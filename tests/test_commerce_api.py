from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from token_mentor.api import create_app
from token_mentor.orchestration.billing import memory_namespace
from token_mentor.state.billing import BillingStore
from token_mentor.state.short_memory import ShortMemory
from token_mentor.state.storage import Catalog

OPERATOR = "operator-key-at-least-thirty-two-characters-long"
LEGACY = "legacy-key-at-least-thirty-two-characters-long"


@pytest.fixture
def api(tmp_path):
    gateway = SimpleNamespace(settings=SimpleNamespace(seller_id="seller"),
        precreate=lambda order: "https://qr.alipay.com/test-" + order["order_id"],
        verify_notification=lambda params: params.get("sign") == "test-verified")
    db = tmp_path / "billing.sqlite3"
    client = TestClient(create_app(str(db), LEGACY, OPERATOR, payment_gateway=gateway))
    return client, BillingStore(Catalog(db))


def auth(token, **extras):
    return {"Authorization": f"Bearer {token}", **extras}


def onboard(client, name):
    response = client.post("/v1/operator/accounts", headers=auth(OPERATOR), json={"name": name})
    assert response.status_code == 201, response.text
    account = response.json()
    response = client.post("/v1/projects", headers=auth(account["api_key"]), json={"name": name + "-project"})
    assert response.status_code == 201, response.text
    return account, response.json()


def paid(client, account):
    headers = auth(account["api_key"])
    order = client.post("/v1/billing/orders", headers=headers).json()
    checkout = client.post(f"/v1/billing/orders/{order['order_id']}/checkout", headers=headers)
    assert checkout.status_code == 200, checkout.text
    assert checkout.json()["qr_code"].startswith("https://qr.alipay.com/")
    params = {"sign": "test-verified", "out_trade_no": order["order_id"], "trade_no": "trade-" + order["order_id"],
              "trade_status": "TRADE_SUCCESS", "total_amount": f"{order['amount_fen'] / 100:.2f}"}
    for _ in range(2):
        response = client.post("/v1/payments/alipay/notify", data=params)
        assert response.text == "success", response.text
    assert client.get("/v1/billing/balance", headers=headers).json()["credits"] == order["credits"]
    return order


def test_paid_end_to_end_and_project_ownership(api):
    client, _store = api
    alice, project = onboard(client, "alice")
    bob, other = onboard(client, "bob")
    payload = {"tasks": ["coding"]}
    headers = auth(project["api_key"], **{"Idempotency-Key": "request-1"})
    assert client.post("/v1/recommend/profile", headers=headers, json=payload).status_code == 402
    order = paid(client, alice)
    first = client.post("/v1/recommend/profile", headers=headers, json=payload)
    assert first.status_code == 200, first.text
    assert client.post("/v1/recommend/profile", headers=headers, json=payload).json() == first.json()
    assert client.get("/v1/billing/balance", headers=auth(alice["api_key"])).json()["credits"] == order["credits"] - 1
    assert client.post("/v1/recommend/profile", headers=headers, json={"tasks": ["translation"]}).status_code == 409
    assert client.post("/v1/recommend/profile", headers=auth(project["api_key"]), json=payload).status_code == 422
    assert client.post("/v1/recommend/profile", headers=auth(project["api_key"], **{"X-Project-ID": other["project_id"],
        "Idempotency-Key": "bad-project"}), json=payload).status_code == 403
    assert client.post("/v1/keys", headers=auth(bob["api_key"]), json={"project_id": project["project_id"]}).status_code == 404
    assert client.get(f"/v1/billing/orders/{order['order_id']}", headers=auth(bob["api_key"])).status_code == 404
    assert client.post("/v1/projects", headers=auth(project["api_key"]), json={"name": "escape"}).status_code == 403
    assert client.get("/v1/catalog", headers=headers).status_code == 403
    assert client.post("/v1/recommend/profile", headers=auth(LEGACY), json=payload).status_code == 401
    assert client.get("/v1/billing/usage", headers=auth(other["api_key"])).json() == []
    report = first.json()["report_id"]
    assert client.post("/v1/feedback", headers=auth(other["api_key"]), json={"report_id": report, "rating": 5}).status_code == 404
    assert client.post("/v1/feedback", headers=headers, json={"report_id": report, "rating": 5}).status_code == 201
    assert client.delete(f"/v1/keys/{project['key_id']}", headers=auth(alice["api_key"])).status_code == 200
    assert client.get("/v1/billing/balance", headers=headers).status_code == 401


def test_account_key_requires_project_and_failed_report_refunds(api):
    client, _ = api
    account, project = onboard(client, "alice")
    order = paid(client, account)
    headers = auth(account["api_key"], **{"Idempotency-Key": "invalid"})
    assert client.post("/v1/recommend/profile", headers=headers, json={"tasks": ["coding"]}).status_code == 422
    headers["X-Project-ID"] = project["project_id"]
    assert client.post("/v1/recommend/profile", headers=headers, json={"tasks": ["unknown"]}).status_code == 422
    assert client.get("/v1/billing/balance", headers=headers).json()["credits"] == order["credits"]


def test_memory_is_namespaced_by_authenticated_account_and_project(api):
    client, store = api
    alice, project = onboard(client, "alice")
    bob, other = onboard(client, "bob")
    paid(client, alice)
    paid(client, bob)
    headers = auth(project["api_key"], **{"Idempotency-Key": "chat-1"})
    payload = {"allow_project_content": True, "intent": "做文本向量化", "user_id": "same", "session_id": "same"}
    result = client.post("/v1/recommend", headers=headers, json=payload)
    assert result.status_code == 200, result.text
    principal = store.authenticate(alice["api_key"])
    namespace = memory_namespace(principal, project["project_id"], "same")
    memory = ShortMemory(store.catalog.path)
    summary = memory.read(namespace, "same")
    assert summary
    assert client.delete("/v1/conversations/same/same", headers=auth(other["api_key"])).status_code == 200
    assert memory.read(namespace, "same") == summary
    assert client.delete("/v1/conversations/same/same", headers=headers).status_code == 200
    assert memory.read(namespace, "same") == ""


def test_bad_or_duplicate_callback_never_credits(api):
    client, _ = api
    account, _ = onboard(client, "alice")
    order = client.post("/v1/billing/orders", headers=auth(account["api_key"])).json()
    bad = {"sign": "invalid", "out_trade_no": order["order_id"], "trade_status": "TRADE_SUCCESS",
           "trade_no": "bad", "total_amount": "10.00"}
    assert client.post("/v1/payments/alipay/notify", data=bad).status_code == 400
    bad["sign"] = "test-verified"
    bad["total_amount"] = "0.01"
    assert client.post("/v1/payments/alipay/notify", data=bad).status_code == 400
    assert client.post("/v1/payments/alipay/notify", content="sign=x&sign=y", headers={
        "Content-Type": "application/x-www-form-urlencoded"}).status_code == 400
    assert client.get("/v1/billing/balance", headers=auth(account["api_key"])).json()["credits"] == 0
