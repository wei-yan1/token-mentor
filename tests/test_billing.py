from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from token_mentor.orchestration.billing import MeteredService
from token_mentor.orchestration.payments import PaymentService
from token_mentor.state.billing import BillingError, BillingStore
from token_mentor.state.storage import Catalog


@pytest.fixture
def store(tmp_path):
    return BillingStore(Catalog(tmp_path / "billing.sqlite3"))


def customer(store, name="customer"):
    account = store.create_account(name)
    principal = store.authenticate(account["api_key"])
    project = store.create_project(principal, "project")
    return principal, project


def fund(store, principal, credits):
    order = store.create_order(principal, amount_fen=1000, credits=credits)
    store.settle_order(order["order_id"], 1000, "alipay-" + order["order_id"])
    return order


def test_key_revocation_and_cross_account_project_rejected(store):
    alice, project = customer(store, "alice")
    bob, _ = customer(store, "bob")
    with pytest.raises(BillingError):
        store.issue_key(bob, project["project_id"])
    project_principal = store.authenticate(project["api_key"])
    with pytest.raises(BillingError):
        store.create_project(project_principal, "another")
    store.revoke_key(alice, project["key_id"])
    assert store.authenticate(project["api_key"]) is None
    with store.catalog._connection() as db:
        rows = db.execute("SELECT key_hash FROM customer_keys").fetchall()
        assert all(project["api_key"] not in row["key_hash"] for row in rows)


def test_payment_duplicate_delivery_atomic_and_amount_checked(store):
    principal, _ = customer(store)
    order = store.create_order(principal, amount_fen=1000, credits=10)
    with pytest.raises(BillingError):
        store.settle_order(order["order_id"], 1, "trade-1")
    assert store.balance(principal)["credits"] == 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: store.settle_order(order["order_id"], 1000, "trade-1"), range(12)))
    assert store.balance(principal)["credits"] == 10
    other = store.create_order(principal, amount_fen=1000, credits=10)
    with pytest.raises(BillingError):
        store.settle_order(other["order_id"], 1000, "trade-1")
    with pytest.raises(BillingError):
        store.settle_order(order["order_id"], 1000, "different-trade")


def test_usage_retry_refund_and_crash_recovery(store):
    principal, project = customer(store)
    fund(store, principal, 3)
    metered = MeteredService(store)
    calls = []

    def action():
        calls.append(1)
        return {"report_id": "a" * 16, "markdown": "report"}

    first = metered.execute(principal, project["project_id"], "one", "profile", {}, action)
    assert metered.execute(principal, project["project_id"], "one", "profile", {}, action) == first
    assert len(calls) == 1
    assert store.balance(principal)["credits"] == 2
    with pytest.raises(BillingError):
        metered.execute(principal, project["project_id"], "one", "profile", {"different": True}, action)

    def failure():
        raise RuntimeError("transient failure")

    with pytest.raises(RuntimeError):
        metered.execute(principal, project["project_id"], "two", "profile", {}, failure)
    assert store.balance(principal)["credits"] == 2
    metered.execute(principal, project["project_id"], "two", "profile", {}, action)
    assert store.balance(principal)["credits"] == 1

    # Simulate an earlier process exiting after reserve; a retry reuses that debit.
    import json
    from hashlib import sha256
    digest = sha256(json.dumps({"operation": "profile", "payload": {}}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    store.reserve(principal, project["project_id"], "crashed", digest, "profile")
    assert store.balance(principal)["credits"] == 0
    metered.execute(principal, project["project_id"], "crashed", "profile", {}, action)
    assert store.balance(principal)["credits"] == 0


def test_concurrent_usage_never_overdraws(store):
    principal, project = customer(store)
    fund(store, principal, 1)
    service = MeteredService(store)

    def invoke(index):
        try:
            service.execute(principal, project["project_id"], f"request-{index}", "profile", {}, lambda: {"ok": True})
            return 200
        except BillingError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(invoke, range(16)))
    assert statuses.count(200) == 1
    assert statuses.count(402) == 15
    assert store.balance(principal)["credits"] == 0


def test_reconcile_requires_matching_signed_gateway_result(store):
    principal, _ = customer(store)
    order = store.create_order(principal, amount_fen=1000, credits=10)
    gateway = SimpleNamespace(settings=SimpleNamespace(seller_id="seller"),
        query=lambda _: {"out_trade_no": order["order_id"], "trade_status": "TRADE_SUCCESS",
                         "total_amount": "10.00", "trade_no": "query-trade"})
    payments = PaymentService(store, gateway)
    assert payments.reconcile(principal, order["order_id"])["status"] == "paid"
    assert store.balance(principal)["credits"] == 10


def test_expired_response_not_recharged_and_abandoned_reservation_can_cancel(store):
    principal, project = customer(store)
    fund(store, principal, 2)
    service = MeteredService(store)
    response = service.execute(principal, project["project_id"], "expiring", "profile", {}, lambda: {"ok": True})
    with store.catalog._connection() as db:
        db.execute("UPDATE usage_requests SET response_expires_at='2000-01-01T00:00:00Z' WHERE request_id=?",
                   (response["request_id"],))
    with pytest.raises(BillingError) as exc:
        service.execute(principal, project["project_id"], "expiring", "profile", {}, dict)
    assert exc.value.status == 410
    assert store.balance(principal)["credits"] == 1
    request_id, _ = store.reserve(principal, project["project_id"], "interrupted", "hash", "profile")
    assert store.balance(principal)["credits"] == 0
    assert service.cancel(principal, request_id)["credit_released"]
    service.cancel(principal, request_id)
    assert store.balance(principal)["credits"] == 1
