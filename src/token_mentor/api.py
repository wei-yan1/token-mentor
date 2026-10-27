import hmac
import os
from typing import Any

from .service import RecommendationService, profile_from_text
from .storage import Catalog


def create_app(db_path: str | None = None, api_key: str | None = None) -> Any:
    """Create the optional FastAPI service. Imports stay lazy for CLI-only installs."""
    try:
        from fastapi import FastAPI, Header, HTTPException
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise RuntimeError("HTTP 服务依赖尚未安装，请执行 pip install -e '.[api]'") from exc

    key = api_key or os.environ.get("TOKEN_MENTOR_API_KEY", "")
    if len(key) < 32:
        raise ValueError("TOKEN_MENTOR_API_KEY 必须至少 32 个字符；服务拒绝以无认证模式启动")
    catalog = Catalog(db_path or os.environ.get("TOKEN_MENTOR_DB", ".token-mentor/catalog.sqlite3"))
    service = RecommendationService(catalog)
    app = FastAPI(
        title="Token Mentor API",
        version="0.1.0",
        description="项目画像、模型推荐、共享报价缓存与反馈接口。",
        docs_url=None,
        redoc_url=None,
    )

    class RecommendationRequest(BaseModel):
        intent: str = Field(min_length=1, max_length=8_000)
        tasks: list[str] = Field(default_factory=list, max_length=12)
        technologies: list[str] = Field(default_factory=list, max_length=30)
        input_tokens: int = Field(default=4_000, ge=0, le=10_000_000)
        output_tokens: int = Field(default=1_200, ge=0, le=10_000_000)
        weights: dict[str, float] | None = None

    class FeedbackRequest(BaseModel):
        report_id: str = Field(min_length=16, max_length=16)
        rating: int = Field(ge=1, le=5)
        note: str = Field(default="", max_length=2_000)

    def require_auth(authorization: str | None) -> None:
        expected = f"Bearer {key}"
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/healthz")
    def health() -> dict[str, str]:
        try:
            catalog.latest_quotes()
            return {"status": "ok"}
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Catalog unavailable") from exc

    @app.post("/v1/recommend")
    def recommend(payload: RecommendationRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(authorization)
        try:
            project = profile_from_text(payload.intent, payload.technologies, payload.tasks)
            result = service.recommend_profile(
                project,
                input_tokens=payload.input_tokens,
                output_tokens=payload.output_tokens,
                weights=payload.weights,
            )
            return {"report_id": result.report_id, "cached": result.cached, "markdown": result.markdown}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/feedback", status_code=201)
    def feedback(payload: FeedbackRequest, authorization: str | None = Header(default=None)) -> dict[str, str]:
        require_auth(authorization)
        try:
            catalog.record_feedback(payload.report_id, payload.rating, payload.note)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"status": "recorded"}

    @app.get("/v1/discoveries")
    def discoveries(limit: int = 50, authorization: str | None = Header(default=None)) -> list[dict[str, str]]:
        require_auth(authorization)
        return catalog.list_discoveries(limit)

    @app.get("/v1/catalog")
    def quote_catalog(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(authorization)
        quotes = catalog.latest_quotes()
        return {"count": len(quotes), "quotes": [quote.to_dict() for quote in quotes]}

    return app

