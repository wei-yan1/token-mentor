"""Customer ownership and atomic credit ledger in the service database."""
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from token_mentor.models import utc_now
from token_mentor.state.storage import Catalog


class BillingError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Principal:
    account_id: str
    key_id: str
    project_id: str | None


class BillingStore:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        with catalog._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    credits INTEGER NOT NULL DEFAULT 0 CHECK(credits >= 0), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS customer_projects (
                    project_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id),
                    name TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(account_id, name)
                );
                CREATE TABLE IF NOT EXISTS customer_keys (
                    key_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id),
                    project_id TEXT REFERENCES customer_projects(project_id), key_hash TEXT NOT NULL UNIQUE,
                    revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payment_orders (
                    order_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id),
                    amount_fen INTEGER NOT NULL CHECK(amount_fen > 0), credits INTEGER NOT NULL CHECK(credits > 0),
                    status TEXT NOT NULL DEFAULT 'pending', trade_no TEXT UNIQUE,
                    qr_code TEXT, created_at TEXT NOT NULL, paid_at TEXT
                );
                CREATE TABLE IF NOT EXISTS credit_ledger (
                    id INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id),
                    delta INTEGER NOT NULL, kind TEXT NOT NULL, reference TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(kind, reference)
                );
                CREATE TABLE IF NOT EXISTS usage_requests (
                    request_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id),
                    project_id TEXT NOT NULL REFERENCES customer_projects(project_id),
                    idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, operation TEXT NOT NULL,
                    status TEXT NOT NULL, response_json TEXT, attempt INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    response_expires_at TEXT,
                    UNIQUE(account_id, project_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS customer_report_access (
                    account_id TEXT NOT NULL, project_id TEXT NOT NULL, report_id TEXT NOT NULL,
                    PRIMARY KEY(account_id, project_id, report_id)
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(usage_requests)")}
            if "response_expires_at" not in columns:
                db.execute("ALTER TABLE usage_requests ADD COLUMN response_expires_at TEXT")

    @staticmethod
    def _new_key(db, account_id, project_id=None):
        token = "tm_" + secrets.token_urlsafe(32)
        key_id = uuid.uuid4().hex
        db.execute("INSERT INTO customer_keys VALUES(?,?,?,?,0,?)",
                   (key_id, account_id, project_id, sha256(token.encode()).hexdigest(), utc_now()))
        return {"key_id": key_id, "api_key": token, "project_id": project_id}

    def create_account(self, name: str) -> dict:
        account_id = uuid.uuid4().hex
        with self.catalog._connection() as db:
            db.execute("INSERT INTO accounts VALUES(?,?,0,?)", (account_id, name, utc_now()))
            key = self._new_key(db, account_id)
        return {"account_id": account_id, **key}

    def authenticate(self, token: str) -> Principal | None:
        with self.catalog._connection() as db:
            row = db.execute("SELECT * FROM customer_keys WHERE key_hash=? AND revoked=0",
                             (sha256(token.encode()).hexdigest(),)).fetchone()
        return Principal(row["account_id"], row["key_id"], row["project_id"]) if row else None

    @staticmethod
    def require_account_key(principal: Principal):
        if principal.project_id:
            raise BillingError("此操作需要账户级 Key", 403)

    def create_project(self, principal: Principal, name: str) -> dict:
        self.require_account_key(principal)
        project_id = uuid.uuid4().hex
        with self.catalog._connection() as db:
            if db.execute("SELECT 1 FROM customer_projects WHERE account_id=? AND name=?",
                          (principal.account_id, name)).fetchone():
                raise BillingError("项目名称已存在")
            db.execute("INSERT INTO customer_projects VALUES(?,?,?,?)",
                       (project_id, principal.account_id, name, utc_now()))
            key = self._new_key(db, principal.account_id, project_id)
        return {"project_id": project_id, "name": name, **key}

    def project(self, principal: Principal, project_id: str | None) -> str:
        if principal.project_id:
            if project_id and project_id != principal.project_id:
                raise BillingError("Key 不属于该项目", 403)
            project_id = principal.project_id
        if not project_id:
            raise BillingError("账户级 Key 调用时需提供 X-Project-ID", 422)
        with self.catalog._connection() as db:
            exists = db.execute("SELECT 1 FROM customer_projects WHERE project_id=? AND account_id=?",
                                (project_id, principal.account_id)).fetchone()
        if not exists:
            raise BillingError("项目不存在", 404)
        return project_id

    def list_projects(self, principal: Principal) -> list[dict]:
        with self.catalog._connection() as db:
            rows = db.execute("SELECT project_id,name,created_at FROM customer_projects WHERE account_id=?",
                              (principal.account_id,)).fetchall()
        return [dict(row) for row in rows if not principal.project_id or row["project_id"] == principal.project_id]

    def issue_key(self, principal: Principal, project_id: str | None):
        self.require_account_key(principal)
        if project_id:
            self.project(principal, project_id)
        with self.catalog._connection() as db:
            return self._new_key(db, principal.account_id, project_id)

    def revoke_key(self, principal: Principal, key_id: str):
        self.require_account_key(principal)
        with self.catalog._connection() as db:
            row = db.execute("UPDATE customer_keys SET revoked=1 WHERE key_id=? AND account_id=?",
                             (key_id, principal.account_id))
            if not row.rowcount:
                raise BillingError("Key 不存在", 404)

    def list_keys(self, principal: Principal):
        self.require_account_key(principal)
        with self.catalog._connection() as db:
            rows = db.execute("SELECT key_id,project_id,revoked,created_at FROM customer_keys WHERE account_id=?",
                              (principal.account_id,)).fetchall()
        return [dict(row) for row in rows]

    def balance(self, principal: Principal):
        with self.catalog._connection() as db:
            row = db.execute("SELECT credits FROM accounts WHERE account_id=?", (principal.account_id,)).fetchone()
        return {"account_id": principal.account_id, "credits": row["credits"]}

    def create_order(self, principal: Principal, *, amount_fen: int, credits: int):
        self.require_account_key(principal)
        if amount_fen <= 0 or credits <= 0:
            raise ValueError("次数包价格和次数必须大于零")
        order_id = uuid.uuid4().hex
        with self.catalog._connection() as db:
            db.execute("INSERT INTO payment_orders(order_id,account_id,amount_fen,credits,created_at) VALUES(?,?,?,?,?)",
                       (order_id, principal.account_id, amount_fen, credits, utc_now()))
        return self.order(principal, order_id)

    def order(self, principal: Principal, order_id: str):
        with self.catalog._connection() as db:
            row = db.execute("SELECT * FROM payment_orders WHERE order_id=? AND account_id=?",
                             (order_id, principal.account_id)).fetchone()
        if not row:
            raise BillingError("订单不存在", 404)
        return dict(row)

    def list_orders(self, principal: Principal, limit=50):
        self.require_account_key(principal)
        with self.catalog._connection() as db:
            rows = db.execute("""SELECT order_id,amount_fen,credits,status,created_at,paid_at
                FROM payment_orders WHERE account_id=? ORDER BY created_at DESC LIMIT ?""",
                (principal.account_id, max(1, min(limit, 100)))).fetchall()
        return [dict(row) for row in rows]

    def set_qr(self, order_id: str, qr_code: str):
        with self.catalog._connection() as db:
            db.execute("UPDATE payment_orders SET qr_code=? WHERE order_id=?", (qr_code, order_id))

    def settle_order(self, order_id: str, amount_fen: int, trade_no: str) -> None:
        if not trade_no:
            raise BillingError("缺少支付宝交易号", 400)
        with self.catalog._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            order = db.execute("SELECT * FROM payment_orders WHERE order_id=?", (order_id,)).fetchone()
            if not order or order["amount_fen"] != amount_fen:
                raise BillingError("订单或金额不匹配", 400)
            if order["status"] == "paid":
                if order["trade_no"] != trade_no:
                    raise BillingError("交易号不匹配", 400)
                return
            reused = db.execute("SELECT 1 FROM payment_orders WHERE trade_no=?", (trade_no,)).fetchone()
            if reused:
                raise BillingError("交易号已用于其他订单", 400)
            db.execute("UPDATE payment_orders SET status='paid',trade_no=?,paid_at=? WHERE order_id=?",
                       (trade_no, utc_now(), order_id))
            db.execute("UPDATE accounts SET credits=credits+? WHERE account_id=?", (order["credits"], order["account_id"]))
            db.execute("INSERT INTO credit_ledger(account_id,delta,kind,reference,created_at) VALUES(?,?,'payment',?,?)",
                       (order["account_id"], order["credits"], order_id, utc_now()))

    def reserve(self, principal: Principal, project_id: str, idempotency_key: str, request_hash: str, operation: str):
        """Caller holds the request's OS lock across reserve/execute/complete."""
        self.clear_expired_responses()
        with self.catalog._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM usage_requests WHERE account_id=? AND project_id=? AND idempotency_key=?",
                             (principal.account_id, project_id, idempotency_key)).fetchone()
            if row:
                if row["request_hash"] != request_hash:
                    raise BillingError("同一 Idempotency-Key 不可用于不同请求")
                if row["status"] == "completed":
                    if row["response_json"] is None:
                        raise BillingError("幂等响应已过期；此请求不会重复扣费，请使用新幂等键发起新请求", 410)
                    return row["request_id"], json.loads(row["response_json"])
                if row["status"] == "processing":
                    # A former worker died. The OS lock proves it no longer executes.
                    return row["request_id"], None
            if not db.execute("UPDATE accounts SET credits=credits-1 WHERE account_id=? AND credits>=1",
                              (principal.account_id,)).rowcount:
                raise BillingError("可用次数不足，请先充值", 402)
            request_id = row["request_id"] if row else uuid.uuid4().hex
            attempt = row["attempt"] + 1 if row else 1
            now = utc_now()
            if row:
                db.execute("UPDATE usage_requests SET status='processing',attempt=?,updated_at=? WHERE request_id=?",
                           (attempt, now, request_id))
            else:
                db.execute("""INSERT INTO usage_requests(request_id,account_id,project_id,idempotency_key,
                    request_hash,operation,status,response_json,attempt,created_at,updated_at)
                    VALUES(?,?,?,?,?,?, 'processing',NULL,1,?,?)""",
                           (request_id, principal.account_id, project_id, idempotency_key, request_hash, operation, now, now))
            db.execute("INSERT INTO credit_ledger(account_id,delta,kind,reference,created_at) VALUES(?,-1,'usage',?,?)",
                       (principal.account_id, f"{request_id}:{attempt}", now))
        return request_id, None

    def complete(self, request_id: str, response: dict, *, ttl_seconds=86400):
        with self.catalog._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM usage_requests WHERE request_id=?", (request_id,)).fetchone()
            if not row or row["status"] != "processing":
                raise BillingError("请求状态不允许完成")
            expiry = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
            db.execute("UPDATE usage_requests SET status='completed',response_json=?,updated_at=?,response_expires_at=? WHERE request_id=?",
                       (json.dumps(response, ensure_ascii=False), utc_now(), expiry, request_id))
            if response.get("report_id"):
                db.execute("INSERT OR IGNORE INTO customer_report_access VALUES(?,?,?)",
                           (row["account_id"], row["project_id"], response["report_id"]))

    def fail(self, request_id: str):
        with self.catalog._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM usage_requests WHERE request_id=?", (request_id,)).fetchone()
            if not row or row["status"] != "processing":
                return
            db.execute("UPDATE usage_requests SET status='failed',updated_at=? WHERE request_id=?", (utc_now(), request_id))
            db.execute("UPDATE accounts SET credits=credits+1 WHERE account_id=?", (row["account_id"],))
            db.execute("INSERT INTO credit_ledger(account_id,delta,kind,reference,created_at) VALUES(?,1,'refund',?,?)",
                       (row["account_id"], f"{request_id}:{row['attempt']}", utc_now()))

    def can_feedback(self, principal, project_id, report_id):
        with self.catalog._connection() as db:
            return bool(db.execute("SELECT 1 FROM customer_report_access WHERE account_id=? AND project_id=? AND report_id=?",
                                   (principal.account_id, project_id, report_id)).fetchone())

    def request(self, principal, request_id):
        with self.catalog._connection() as db:
            row = db.execute("SELECT * FROM usage_requests WHERE account_id=? AND request_id=?",
                             (principal.account_id, request_id)).fetchone()
        if not row or (principal.project_id and row["project_id"] != principal.project_id):
            raise BillingError("请求不存在", 404)
        return dict(row)

    def usage(self, principal: Principal, limit: int = 100):
        self.clear_expired_responses()
        with self.catalog._connection() as db:
            rows = db.execute("""SELECT request_id,project_id,operation,status,created_at,updated_at
                FROM usage_requests WHERE account_id=? AND (? IS NULL OR project_id=?)
                ORDER BY created_at DESC LIMIT ?""",
                (principal.account_id, principal.project_id, principal.project_id, max(1, min(limit, 1000)))).fetchall()
        return [dict(row) for row in rows]

    def clear_expired_responses(self):
        with self.catalog._connection() as db:
            db.execute("UPDATE usage_requests SET response_json=NULL WHERE response_json IS NOT NULL AND response_expires_at<=?",
                       (utc_now(),))
