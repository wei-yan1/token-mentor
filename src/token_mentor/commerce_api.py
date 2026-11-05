"""HTTP adapters for accounts, project keys and credit-pack orders."""
from urllib.parse import parse_qsl

from token_mentor.orchestration.payments import PaymentService
from token_mentor.state.billing import BillingError
from token_mentor.tools.alipay import PaymentUnavailable


def register_commerce(app, store, metered, require_auth, require_operator, gateway, pack):
    from fastapi import Header, HTTPException, Request
    from fastapi.responses import JSONResponse, PlainTextResponse, Response
    from pydantic import BaseModel, Field
    from starlette.concurrency import run_in_threadpool

    payments = PaymentService(store, gateway)

    @app.exception_handler(BillingError)
    async def billing_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @app.exception_handler(PaymentUnavailable)
    async def payment_error(request, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    class NamedRequest(BaseModel):
        name: str = Field(min_length=1, max_length=128, pattern=r".*\S.*")
        model_config = {"extra": "forbid"}

    class KeyRequest(BaseModel):
        project_id: str | None = Field(default=None, min_length=32, max_length=32)
        model_config = {"extra": "forbid"}

    def customer(authorization):
        principal = require_auth(authorization)
        if principal is None:
            raise HTTPException(403, "此接口需要独立客户 Key")
        return principal

    @app.post("/v1/operator/accounts", status_code=201)
    def create_account(payload: NamedRequest, authorization: str | None = Header(default=None)):
        require_operator(authorization)
        return store.create_account(payload.name.strip())

    @app.post("/v1/projects", status_code=201)
    def create_project(payload: NamedRequest, authorization: str | None = Header(default=None)):
        return store.create_project(customer(authorization), payload.name.strip())

    @app.get("/v1/projects")
    def projects(authorization: str | None = Header(default=None)):
        return store.list_projects(customer(authorization))

    @app.post("/v1/keys", status_code=201)
    def issue_key(payload: KeyRequest, authorization: str | None = Header(default=None)):
        return store.issue_key(customer(authorization), payload.project_id)

    @app.delete("/v1/keys/{key_id}")
    def revoke_key(key_id: str, authorization: str | None = Header(default=None)):
        store.revoke_key(customer(authorization), key_id)
        return {"revoked": True}

    @app.get("/v1/keys")
    def keys(authorization: str | None = Header(default=None)):
        return store.list_keys(customer(authorization))

    @app.get("/v1/billing/balance")
    def balance(authorization: str | None = Header(default=None)):
        return store.balance(customer(authorization))

    @app.get("/v1/billing/usage")
    def usage(limit: int = 100, authorization: str | None = Header(default=None)):
        return store.usage(customer(authorization), limit)

    @app.post("/v1/billing/requests/{request_id}/cancel")
    def cancel_request(request_id: str, authorization: str | None = Header(default=None)):
        try:
            return metered.cancel(customer(authorization), request_id)
        except RuntimeError as exc:
            raise HTTPException(409, "请求仍在执行，请稍后重试") from exc

    @app.get("/v1/billing/pack")
    def credit_pack(authorization: str | None = Header(default=None)):
        customer(authorization)
        return pack

    @app.post("/v1/billing/orders", status_code=201)
    def create_order(authorization: str | None = Header(default=None)):
        # No amount/credits supplied by the customer: snapshot the server's current offer.
        return store.create_order(customer(authorization), **pack)

    @app.get("/v1/billing/orders")
    def orders(authorization: str | None = Header(default=None)):
        return store.list_orders(customer(authorization))

    @app.get("/v1/billing/orders/{order_id}/qr.svg")
    def order_qr(order_id: str, authorization: str | None = Header(default=None)):
        principal = customer(authorization)
        store.require_account_key(principal)
        data = store.order(principal, order_id)
        if not data["qr_code"]:
            raise HTTPException(409, "请先为此订单创建支付二维码")
        import qrcode
        import qrcode.image.svg
        image = qrcode.make(data["qr_code"], image_factory=qrcode.image.svg.SvgPathImage)
        return Response(image.to_string(), media_type="image/svg+xml")

    @app.get("/v1/billing/orders/{order_id}")
    def order(order_id: str, authorization: str | None = Header(default=None)):
        principal = customer(authorization)
        store.require_account_key(principal)
        return store.order(principal, order_id)

    @app.post("/v1/billing/orders/{order_id}/checkout")
    def checkout(order_id: str, authorization: str | None = Header(default=None)):
        return payments.checkout(customer(authorization), order_id)

    @app.post("/v1/billing/orders/{order_id}/reconcile")
    def reconcile(order_id: str, authorization: str | None = Header(default=None)):
        return payments.reconcile(customer(authorization), order_id)

    @app.post("/v1/payments/alipay/notify", response_class=PlainTextResponse)
    async def alipay_notify(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            return PlainTextResponse("failure", status_code=400)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 64_000:
                return PlainTextResponse("failure", status_code=413)
        try:
            pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=100)
            params = dict(pairs)
            if len(params) != len(pairs):
                raise ValueError("重复通知字段")
            await run_in_threadpool(payments.notification, params)
        except (ValueError, PaymentUnavailable):
            return PlainTextResponse("failure", status_code=400)
        return PlainTextResponse("success")
