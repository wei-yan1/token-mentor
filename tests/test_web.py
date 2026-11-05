from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from token_mentor.api import create_app
from token_mentor.state.billing import BillingStore
from token_mentor.state.storage import Catalog

WEB = {"X-Requested-With": "TokenMentor"}


@pytest.fixture
def web(tmp_path):
    db = tmp_path / "web.sqlite3"
    gateway = SimpleNamespace(precreate=lambda order: "https://qr.alipay.com/test-" + order["order_id"])
    codes = {}
    def send_code(email, code, purpose):
        if email == "fail@qq.com":
            raise RuntimeError("mail transport unavailable")
        codes[(email, purpose)] = code

    app = create_app(str(db), payment_gateway=gateway, mailer=SimpleNamespace(send_code=send_code))
    app.state.test_codes = codes
    return TestClient(app), BillingStore(Catalog(db)), app


def register(client, email="owner@qq.com"):
    response = client.post("/v1/auth/email-code", headers=WEB, json={"email":email,"purpose":"register"})
    assert response.status_code == 200, response.text
    code = client.app.state.test_codes[(email,"register")]
    return client.post("/v1/auth/register", headers=WEB,
                       json={"name":"测试团队","username":email.split("@")[0],"email":email,
                             "password":"correct-password-123","code":code})


def test_pages_and_packaged_assets_are_available(web):
    client, _, _ = web
    for path in ("/", "/login", "/app", "/assets/styles.css", "/assets/app.js", "/assets/tokens.css"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "#667eea" in client.get("/assets/tokens.css").text
    assert client.get("/v1/auth/me").status_code == 401


def test_registration_login_and_existing_project_apis_share_cookie_auth(web):
    client, store, _ = web
    result = register(client)
    assert result.status_code == 201, result.text
    assert "httponly" in result.headers["set-cookie"].lower()
    assert "api_key" not in result.json()
    me = client.get("/v1/auth/me").json()
    assert me["name"] == "测试团队" and me["can_manage"]
    assert client.get("/v1/billing/balance").json()["credits"] == 0
    project = client.post("/v1/projects", headers=WEB, json={"name": "知识库"})
    assert project.status_code == 201, project.text
    assert len(client.get("/v1/projects").json()) == 1
    assert client.get("/v1/catalog").status_code == 403
    assert client.post("/v1/projects", json={"name": "跨站操作"}).status_code == 403
    assert client.post("/v1/projects", headers={**WEB, "Origin": "https://another.example"}, json={"name": "跨站操作"}).status_code == 403
    cookie = client.cookies.get("tm_session")
    with store.catalog._connection() as db:
        assert db.execute("SELECT password_hash FROM web_users").fetchone()[0] != "correct-password-123"
        assert db.execute("SELECT token_hash FROM web_sessions").fetchone()[0] != cookie
    assert client.post("/v1/auth/logout", headers=WEB).status_code == 200
    assert client.get("/v1/projects").status_code == 401
    wrong = client.post("/v1/auth/login", headers=WEB, json={"account":"owner@qq.com","password":"wrong"})
    assert wrong.status_code == 401
    login = client.post("/v1/auth/login", headers=WEB, json={"account":"OWNER@qq.com","password":"correct-password-123"})
    assert login.status_code == 200
    assert client.get("/v1/auth/me").json()["account_id"] == me["account_id"]


def test_browser_tenant_isolation_orders_qr_and_key_revocation(web):
    alice, _store, app = web
    assert register(alice).status_code == 201
    bob = TestClient(app)
    assert register(bob, "other@qq.com").status_code == 201
    project = alice.post("/v1/projects", headers=WEB, json={"name":"客户一项目"}).json()
    assert bob.get("/v1/projects").json() == []
    order = alice.post("/v1/billing/orders", headers=WEB).json()
    checkout = alice.post(f"/v1/billing/orders/{order['order_id']}/checkout", headers=WEB)
    assert checkout.status_code == 200, checkout.text
    qr = alice.get(f"/v1/billing/orders/{order['order_id']}/qr.svg")
    assert qr.status_code == 200 and b"<svg" in qr.content
    assert bob.get(f"/v1/billing/orders/{order['order_id']}/qr.svg").status_code == 404
    assert bob.get("/v1/billing/orders").json() == []
    assert alice.get("/v1/billing/orders").json()[0]["order_id"] == order["order_id"]
    assert alice.post("/v1/auth/key-login", headers=WEB, json={"api_key":project["api_key"]}).status_code == 404
    assert alice.get("/v1/auth/me").json()["can_manage"] is True
    assert alice.delete(f"/v1/keys/{project['key_id']}", headers=WEB).status_code == 200


def test_qq_code_required_single_use_and_password_username_login(web):
    client, _, _ = web
    assert client.post("/v1/auth/email-code",headers=WEB,json={"email":"test@gmail.com","purpose":"register"}).status_code == 422
    assert register(client).status_code == 201
    client.post("/v1/auth/logout",headers=WEB)
    assert client.post("/v1/auth/login",headers=WEB,json={"account":"owner","password":"correct-password-123"}).status_code == 200
    client.post("/v1/auth/logout",headers=WEB)
    assert client.post("/v1/auth/email-code",headers=WEB,json={"email":"owner@qq.com","purpose":"login"}).status_code == 200
    assert client.post("/v1/auth/email-code",headers=WEB,json={"email":"owner@qq.com","purpose":"login"}).status_code == 429
    code=client.app.state.test_codes[("owner@qq.com","login")]
    assert client.post("/v1/auth/email-login",headers=WEB,json={"email":"owner@qq.com","code":"bad"}).status_code == 422
    assert client.post("/v1/auth/email-login",headers=WEB,json={"email":"owner@qq.com","code":code}).status_code == 200
    client.post("/v1/auth/logout",headers=WEB)
    assert client.post("/v1/auth/email-login",headers=WEB,json={"email":"owner@qq.com","code":code}).status_code == 401


def test_failed_mail_delivery_removes_challenge_and_signup_code_is_one_time(web):
    client, _, _ = web
    response = client.post("/v1/auth/email-code",headers=WEB,json={"email":"fail@qq.com","purpose":"register"})
    assert response.status_code == 503
    with client.app.state.web_auth.billing.catalog._connection() as db:
        assert db.execute("SELECT 1 FROM email_verification_codes WHERE email=?",
                          ("fail@qq.com",)).fetchone() is None
    result=register(client)
    assert result.status_code==201
    code=client.app.state.test_codes[("owner@qq.com","register")]
    assert client.post("/v1/auth/register",headers=WEB,json={"name":"Again","username":"different","email":"owner@qq.com",
        "password":"another-password-123","code":code}).status_code==401


def test_signup_rejects_duplicate_username_and_unknown_login_email(web):
    client, _, _=web
    assert register(client,"owner@qq.com").status_code==201
    client.post("/v1/auth/logout",headers=WEB)
    assert client.post("/v1/auth/email-code",headers=WEB,json={"email":"different@qq.com","purpose":"register"}).status_code==200
    other_code=client.app.state.test_codes[("different@qq.com","register")]
    response=client.post("/v1/auth/register",headers=WEB,json={"name":"Duplicate","username":"owner","email":"different@qq.com",
        "password":"password-at-least-ten","code":other_code})
    assert response.status_code==409
    assert client.post("/v1/auth/email-code",headers=WEB,json={"email":"unknown@qq.com","purpose":"login"}).status_code==404
