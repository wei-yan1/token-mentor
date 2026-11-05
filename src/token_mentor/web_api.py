"""Same-origin browser UI and cookie authentication adapter."""
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

from token_mentor.orchestration.email_auth import EmailAuthentication
from token_mentor.state.billing import BillingError
from token_mentor.state.web_auth import WebAuth
from token_mentor.tools.mail import SmtpMailer


def register_web(app, billing, mailer=None):
    from fastapi import HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, Field
    from starlette.concurrency import run_in_threadpool

    auth = WebAuth(billing)
    email_auth = EmailAuthentication(auth, mailer or SmtpMailer())
    app.state.web_auth = auth
    current = ContextVar("web_principal", default=None)
    assets = Path(__file__).parent / "web"
    app.mount("/assets", StaticFiles(directory=assets), name="web-assets")

    @app.middleware("http")
    async def session_context(request: Request, call_next):
        cookie = request.cookies.get("tm_session", "")
        browser_write = request.method not in {"GET", "HEAD", "OPTIONS"} and not request.headers.get("authorization")
        if browser_write and (cookie or request.url.path.startswith("/v1/auth/")):
            origin = request.headers.get("origin")
            if (request.headers.get("x-requested-with") != "TokenMentor"
                    or (origin and origin != str(request.base_url).rstrip("/"))):
                return JSONResponse({"detail": "请从本站页面发起操作"}, status_code=403)
        principal = await run_in_threadpool(auth.authenticate, cookie) if cookie else None
        context = current.set(principal)
        try:
            response = await call_next(request)
            if request.url.path.startswith(("/v1/", "/login", "/app")):
                response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "font-src 'self'; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
            )
            return response
        finally:
            current.reset(context)

    @app.get("/", include_in_schema=False)
    def home_page():
        return FileResponse(assets / "index.html")

    @app.get("/login", include_in_schema=False)
    def login_page():
        return FileResponse(assets / "login.html")

    @app.get("/app", include_in_schema=False)
    def app_page():
        return FileResponse(assets / "app.html")

    class EmailRequest(BaseModel):
        email: str = Field(max_length=254, pattern=r"^[A-Za-z0-9._%+-]+@[qQ][qQ]\.[cC][oO][mM]$")
        model_config = {"extra": "forbid"}

    class LoginRequest(BaseModel):
        account: str = Field(min_length=1, max_length=254)
        password: str = Field(min_length=1, max_length=128)
        model_config = {"extra": "forbid"}

    class RegisterRequest(EmailRequest):
        name: str = Field(min_length=1, max_length=128, pattern=r".*\S.*")
        username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
        password: str = Field(min_length=10, max_length=128)
        code: str = Field(pattern=r"^\d{6}$")

    class SendCodeRequest(EmailRequest):
        purpose: Literal["register", "login"]

    class EmailLoginRequest(EmailRequest):
        code: str = Field(pattern=r"^\d{6}$")

    @app.post("/v1/auth/email-code")
    def send_code(payload: SendCodeRequest, request: Request):
        throttle(request)
        try:
            email_auth.send_code(payload.email.casefold(), payload.purpose)
        except BillingError:
            raise
        except Exception as exc:
            raise HTTPException(503, "验证码发送失败，请检查 QQ 邮箱 SMTP 配置") from exc
        return {"sent": True, "expires_in": 600, "retry_after": 60}

    def session_response(request, token):
        response = JSONResponse({"authenticated": True})
        secure = request.url.scheme == "https" or os.getenv("TOKEN_MENTOR_COOKIE_SECURE", "false").lower() == "true"
        response.set_cookie("tm_session", token, httponly=True, secure=secure, samesite="lax",
                            max_age=auth.session_ttl, path="/")
        return response

    def throttle(request):
        auth.throttle(request.client.host if request.client else "unknown")

    @app.post("/v1/auth/register", status_code=201)
    def register(payload: RegisterRequest, request: Request):
        throttle(request)
        response = session_response(request, auth.register(payload.name, payload.email, payload.password, payload.username, payload.code))
        response.status_code = 201
        return response

    @app.post("/v1/auth/login")
    def login(payload: LoginRequest, request: Request):
        throttle(request)
        return session_response(request, auth.login(payload.account, payload.password))

    @app.post("/v1/auth/email-login")
    def email_login(payload: EmailLoginRequest, request: Request):
        throttle(request)
        return session_response(request, auth.email_login(payload.email, payload.code))

    @app.get("/v1/auth/me")
    def me():
        principal = current.get()
        if not principal:
            raise HTTPException(401, "请先登录")
        return auth.me(principal)

    @app.post("/v1/auth/logout")
    def logout(request: Request):
        auth.logout(request.cookies.get("tm_session", ""))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie("tm_session", path="/")
        return response

    return current
