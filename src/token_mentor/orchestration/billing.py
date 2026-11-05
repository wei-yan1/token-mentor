"""Shared charging entry point for HTTP and future MCP adapters."""
import json
import re
from hashlib import sha256

from token_mentor.constraints.recovery import exclusive_run
from token_mentor.state.billing import BillingError, BillingStore, Principal


class MeteredService:
    def __init__(self, store: BillingStore):
        self.store = store

    def execute(self, principal: Principal, project_id: str, idempotency_key: str | None,
                operation: str, payload: dict, action):
        if not idempotency_key or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", idempotency_key):
            raise BillingError("需要 1–128 字符的 Idempotency-Key（字母、数字、下划线或短横线）", 422)
        request_hash = sha256(json.dumps({"operation": operation, "payload": payload},
                                        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        lock_id = sha256(json.dumps([principal.account_id, project_id, idempotency_key]).encode()).hexdigest()
        with exclusive_run(self.store.catalog.path.parent / "billing-locks" / f"{lock_id}.lock"):
            request_id, previous = self.store.reserve(principal, project_id, idempotency_key, request_hash, operation)
            if previous is not None:
                return previous
            try:
                response = {**action(), "request_id": request_id}
                self.store.complete(request_id, response,
                                    ttl_seconds=1800 if payload.get("session_id") else 86400)
            except Exception:
                self.store.fail(request_id)
                raise
            return response

    def cancel(self, principal: Principal, request_id: str):
        row = self.store.request(principal, request_id)
        lock_id = sha256(json.dumps([principal.account_id, row["project_id"], row["idempotency_key"]]).encode()).hexdigest()
        with exclusive_run(self.store.catalog.path.parent / "billing-locks" / f"{lock_id}.lock"):
            row = self.store.request(principal, request_id)
            if row["status"] == "completed":
                raise BillingError("已完成的请求不可取消")
            self.store.fail(request_id)
        return {"request_id": request_id, "status": "failed", "credit_released": True}


def memory_namespace(principal: Principal, project_id: str, user_id: str) -> str:
    return sha256(json.dumps([principal.account_id, project_id, user_id]).encode()).hexdigest()
