import hmac
import os
from typing import Any

from token_mentor.commerce_api import register_commerce
from token_mentor.orchestration.agents import ProjectAdvisorAgent
from token_mentor.orchestration.billing import MeteredService, memory_namespace
from token_mentor.orchestration.comparison import (
    ComparisonRequest as WorkflowComparisonRequest,
)
from token_mentor.orchestration.comparison import ComparisonWorkflow
from token_mentor.orchestration.conversation import ConversationAdvisor
from token_mentor.orchestration.service import RecommendationService
from token_mentor.state.billing import BillingError, BillingStore
from token_mentor.state.storage import Catalog
from token_mentor.tools.alipay import AlipayGateway, AlipaySettings
from token_mentor.web_api import register_web


def create_app(db_path: str | None = None, api_key: str | None = None, operator_key: str | None = None,
               *, allow_legacy_key: bool | None = None, payment_gateway=None, mailer=None) -> Any:
    """Create the optional FastAPI service. Imports stay lazy for CLI-only installs."""
    try:
        from fastapi import FastAPI, Header, HTTPException
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise RuntimeError("HTTP 服务依赖尚未安装，请执行 pip install -e '.[api]'") from exc

    key = api_key or os.environ.get("TOKEN_MENTOR_API_KEY", "")
    legacy = allow_legacy_key if allow_legacy_key is not None else os.getenv("TOKEN_MENTOR_ALLOW_LEGACY_KEY", "false").lower() == "true"
    if (key and len(key) < 32) or (legacy and not key):
        raise ValueError("兼容密钥必须至少 32 个字符")
    operator = operator_key or os.environ.get("TOKEN_MENTOR_OPERATOR_KEY", "")
    if operator and (len(operator) < 32 or hmac.compare_digest(operator, key)):
        raise ValueError("运营密钥必须至少 32 字符且与客户密钥不同")
    catalog = Catalog(db_path or os.environ.get("TOKEN_MENTOR_DB", ".token-mentor/catalog.sqlite3"))
    billing = BillingStore(catalog)
    metered = MeteredService(billing)
    pack = {"amount_fen": int(os.getenv("TOKEN_MENTOR_PACK_AMOUNT_FEN", "1000")),
            "credits": int(os.getenv("TOKEN_MENTOR_PACK_CREDITS", "10"))}
    if not 1 <= pack["amount_fen"] <= 100_000_000 or not 1 <= pack["credits"] <= 1_000_000:
        raise ValueError("次数包价格或次数超出范围")
    service = RecommendationService(catalog)
    conversation = ConversationAdvisor(service)
    app = FastAPI(
        title="Token Mentor API",
        version="0.1.0",
        description="项目画像、模型推荐、共享报价缓存与反馈接口。",
        docs_url=None,
        redoc_url=None,
    )
    web_principal = register_web(app, billing, mailer)

    class RecommendationRequest(BaseModel):
        user_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
        session_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
        allow_project_content: bool = False
        intent: str = Field(min_length=1, max_length=8_000)
        tasks: list[str] = Field(default_factory=list, max_length=12)
        technologies: list[str] = Field(default_factory=list, max_length=30)
        input_tokens: int = Field(default=4_000, ge=0, le=10_000_000)
        output_tokens: int = Field(default=1_200, ge=0, le=10_000_000)
        weights: dict[str, float] | None = None

    class ProfileRequest(BaseModel):
        tasks: list[str] = Field(min_length=1, max_length=12)
        technologies: list[str] = Field(default_factory=list, max_length=30)
        input_tokens: int = Field(default=4_000, ge=0, le=10_000_000)
        output_tokens: int = Field(default=1_200, ge=0, le=10_000_000)
        weights: dict[str, float] | None = None

        model_config = {"extra": "forbid"}

    class ComparisonRequestBody(BaseModel):
        task: str = Field(min_length=1, max_length=200)
        input_tokens: int = Field(default=4_000, ge=0, le=10_000_000)
        output_tokens: int = Field(default=1_200, ge=0, le=10_000_000)
        benchmark_id: str | None = Field(default=None, max_length=200)
        benchmark_version: str | None = Field(default=None, max_length=100)
        protocol: str = Field(default="", max_length=300)

    class FeedbackRequest(BaseModel):
        report_id: str = Field(min_length=16, max_length=16)
        rating: int = Field(ge=1, le=5)
        note: str = Field(default="", max_length=2_000)

    def require_auth(authorization: str | None):
        if not authorization and web_principal.get():
            return web_principal.get()
        if authorization and authorization.startswith("Bearer "):
            token = authorization[7:]
            if legacy and hmac.compare_digest(token, key):
                return None
            principal = billing.authenticate(token)
            if principal:
                return principal
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    def require_operator(authorization: str | None) -> None:
        if not operator or not authorization or not hmac.compare_digest(authorization, f"Bearer {operator}"):
            raise HTTPException(status_code=403, detail="Operator access required")

    register_commerce(app, billing, metered, require_auth, require_operator,
                      payment_gateway or AlipayGateway(AlipaySettings.from_env()), pack)

    def charge(principal, project_id, idempotency_key, operation, payload, action):
        if principal is None:  # Explicit opt-in compatibility mode; never sold to customers.
            return action()
        project = billing.project(principal, project_id)
        try:
            return metered.execute(principal, project, idempotency_key, operation, payload, action)
        except BillingError:
            raise
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/recommend/profile")
    def recommend_profile(payload: ProfileRequest, authorization: str | None = Header(default=None),
                          x_project_id: str | None = Header(default=None),
                          idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        principal = require_auth(authorization)
        def action():
            result = ProjectAdvisorAgent(service).from_intent(
                "客户端已完成本地任务分析", tasks=payload.tasks,
                technologies=payload.technologies, input_tokens=payload.input_tokens,
                output_tokens=payload.output_tokens, weights=payload.weights)
            return {"report_id": result.report_id, "cached": result.cached, "markdown": result.markdown}
        try:
            return charge(principal, x_project_id, idempotency_key, "profile", payload.model_dump(), action)
        except BillingError:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/healthz")
    def health() -> dict[str, str]:
        try:
            catalog.latest_quotes()
            return {"status": "ok"}
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Catalog unavailable") from exc

    @app.post("/v1/recommend")
    def recommend(payload: RecommendationRequest, authorization: str | None = Header(default=None),
                  x_project_id: str | None = Header(default=None),
                  idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        principal = require_auth(authorization)
        project = billing.project(principal, x_project_id) if principal else None
        if not payload.allow_project_content:
            raise HTTPException(422, "云端分析需要 allow_project_content=true；敏感项目请使用 /v1/recommend/profile")
        if bool(payload.user_id) != bool(payload.session_id):
            raise HTTPException(422, "短期记忆需要同时提供 user_id 和 session_id")
        namespace = (memory_namespace(principal, project, payload.user_id) if principal and payload.user_id
                     else payload.user_id)
        def action():
            recommender = (lambda message, **kwargs: conversation.recommend(
                namespace, payload.session_id, message, **kwargs)) if payload.session_id else ProjectAdvisorAgent(service).from_intent
            result = recommender(payload.intent, technologies=payload.technologies,
                                 tasks=payload.tasks or None, input_tokens=payload.input_tokens,
                                 output_tokens=payload.output_tokens, weights=payload.weights)
            return {"report_id": result.report_id, "cached": result.cached, "markdown": result.markdown}
        try:
            return charge(principal, x_project_id, idempotency_key, "recommend", payload.model_dump(), action)
        except BillingError:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete('/v1/conversations/{user_id}/{session_id}')
    def clear_conversation(user_id: str, session_id: str, authorization: str | None = Header(default=None),
                           x_project_id: str | None = Header(default=None)):
        principal = require_auth(authorization)
        namespace = memory_namespace(principal, billing.project(principal, x_project_id), user_id) if principal else user_id
        try:
            conversation.clear(namespace, session_id)
            return {'cleared': True}
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/comparisons")
    def create_comparison(payload: ComparisonRequestBody, authorization: str | None = Header(default=None),
                          x_project_id: str | None = Header(default=None),
                          idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        principal = require_auth(authorization)
        def action():
            import uuid
            quotes = catalog.latest_quotes()
            if not quotes:
                raise ValueError("报价库为空")
            request = WorkflowComparisonRequest(payload.task, payload.input_tokens, payload.output_tokens,
                                                payload.benchmark_id, payload.benchmark_version, payload.protocol)
            run_id = uuid.uuid4().hex
            workflow = ComparisonWorkflow(catalog)
            workflow.create(run_id, request, quotes, catalog.benchmark_snapshot())
            result = workflow.run(run_id)
            return {"run_id": run_id, "status": result["status"], "markdown": result["result_json"].get("markdown", "")}
        try:
            return charge(principal, x_project_id, idempotency_key, "comparison", payload.model_dump(), action)
        except BillingError:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/workflows/{run_id}")
    def workflow_status(run_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_operator(authorization)
        try:
            return ComparisonWorkflow(catalog).status(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/workflows/{run_id}/resume")
    def workflow_resume(run_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_operator(authorization)
        try:
            return ComparisonWorkflow(catalog).resume(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/feedback", status_code=201)
    def feedback(payload: FeedbackRequest, authorization: str | None = Header(default=None),
                 x_project_id: str | None = Header(default=None)) -> dict[str, str]:
        principal = require_auth(authorization)
        if principal and not billing.can_feedback(principal, billing.project(principal, x_project_id), payload.report_id):
            raise HTTPException(404, "报告不存在")
        try:
            catalog.record_feedback(payload.report_id, payload.rating, payload.note)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"status": "recorded"}

    @app.get("/v1/discoveries")
    def discoveries(limit: int = 50, authorization: str | None = Header(default=None)) -> list[dict[str, str]]:
        require_operator(authorization)
        return catalog.list_discoveries(limit)

    @app.get("/v1/market/targets")
    def market_targets(scope: str = "all", authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        require_operator(authorization)
        if scope not in {"domestic", "foreign", "all"}:
            raise HTTPException(status_code=422, detail="scope must be domestic, foreign or all")
        return catalog.list_research_targets(None if scope == "all" else scope)

    @app.get("/v1/market/models")
    def market_models(scope: str = "all", limit: int = 200, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        require_operator(authorization)
        if scope not in {"domestic", "foreign", "all"}:
            raise HTTPException(status_code=422, detail="scope must be domestic, foreign or all")
        return catalog.list_model_profiles(limit, None if scope == "all" else scope)

    @app.get("/v1/market/leads")
    def market_leads(scope: str = "all", limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        require_operator(authorization)
        if scope not in {"domestic", "foreign", "all"}:
            raise HTTPException(status_code=422, detail="scope must be domestic, foreign or all")
        return catalog.list_market_leads(limit, scope)

    @app.get("/v1/market/price-changes")
    def market_price_changes(limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        require_operator(authorization)
        return catalog.list_price_changes(limit)

    @app.get("/v1/market/checks")
    def market_checks(limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        require_operator(authorization)
        return catalog.list_market_checks(limit)

    @app.get("/v1/catalog")
    def quote_catalog(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_operator(authorization)
        quotes = catalog.latest_quotes()
        return {"count": len(quotes), "quotes": [quote.to_dict() for quote in quotes]}

    return app

