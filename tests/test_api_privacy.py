import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from token_mentor.api import create_app
from token_mentor.state.storage import Catalog


KEY = "customer-test-key-with-at-least-32-characters"
OPERATOR = "operator-test-key-with-at-least-32-characters"


def test_internal_routes_require_separate_operator_key():
    with tempfile.TemporaryDirectory() as tmp:
        client = TestClient(create_app(str(Path(tmp) / "db.sqlite3"), KEY, OPERATOR, allow_legacy_key=True))
        for path in ("/v1/catalog", "/v1/discoveries", "/v1/market/models",
                     "/v1/market/targets", "/v1/market/leads", "/v1/market/checks",
                     "/v1/market/price-changes", "/v1/workflows/missing"):
            assert client.get(path, headers={"Authorization": f"Bearer {KEY}"}).status_code == 403
        assert client.post("/v1/workflows/missing/resume", headers={"Authorization": f"Bearer {KEY}"}).status_code == 403
        assert client.get("/v1/catalog", headers={"Authorization": f"Bearer {OPERATOR}"}).status_code == 200
        disabled = TestClient(create_app(str(Path(tmp) / "other.sqlite3"), KEY, "", allow_legacy_key=True))
        assert disabled.get("/v1/catalog", headers={"Authorization": f"Bearer {KEY}"}).status_code == 403


def test_private_profile_rejects_text_and_cloud_requires_consent():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "db.sqlite3"
        client = TestClient(create_app(str(db), KEY, OPERATOR, allow_legacy_key=True))
        headers = {"Authorization": f"Bearer {KEY}"}
        secret = "confidential-repository-business-text"
        assert client.post("/v1/recommend", headers=headers, json={"intent": secret}).status_code == 422
        assert client.post("/v1/recommend/profile", headers=headers,
                           json={"tasks": ["coding"], "intent": secret}).status_code == 422
        result = client.post("/v1/recommend/profile", headers=headers,
                             json={"tasks": ["coding"], "technologies": ["FastAPI"]})
        assert result.status_code == 200, result.text
        repeated = client.post("/v1/recommend/profile", headers=headers,
                               json={"tasks": ["coding"], "technologies": ["FastAPI"]})
        assert repeated.json()["cached"] is True
        with Catalog(db)._connection() as connection:
            rows = connection.execute("SELECT report FROM recommendation_cache").fetchall()
            assert len(rows) == 1
            assert secret not in rows[0]["report"]
        assert client.post("/v1/recommend/profile", headers=headers,
                           json={"tasks": ["unknown"]}).status_code == 422
