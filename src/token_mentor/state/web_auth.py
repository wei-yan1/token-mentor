"""Password identities and short-lived browser sessions in the service database."""
import hashlib
import hmac
import secrets
import sqlite3
import time
import uuid

from token_mentor.models import utc_now
from token_mentor.state.billing import BillingError, Principal


class WebAuth:
    session_ttl = 8 * 60 * 60

    def __init__(self, billing):
        self.billing = billing
        with billing.catalog._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS web_users (
                    account_id TEXT PRIMARY KEY REFERENCES accounts(account_id), email TEXT NOT NULL UNIQUE,
                    password_salt TEXT NOT NULL, password_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id),
                    key_id TEXT REFERENCES customer_keys(key_id), expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_auth_attempts (
                    identity TEXT PRIMARY KEY, attempts INTEGER NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS email_verification_codes (
                    email TEXT NOT NULL, purpose TEXT NOT NULL, salt TEXT NOT NULL, code_hash TEXT NOT NULL,
                    expires_at REAL NOT NULL, sent_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(email,purpose)
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(web_users)")}
            if "username" not in columns:
                db.execute("ALTER TABLE web_users ADD COLUMN username TEXT")
            if "email_verified" not in columns:
                db.execute("ALTER TABLE web_users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_web_username ON web_users(username) WHERE username IS NOT NULL")
            # API keys are service credentials, never a browser-login mechanism.
            db.execute("DELETE FROM web_sessions WHERE key_id IS NOT NULL")

    def throttle(self, remote: str):
        identity = hashlib.sha256(remote.encode()).hexdigest()
        with self.billing.catalog._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM web_auth_attempts WHERE expires_at<=?", (time.time(),))
            row = db.execute("SELECT attempts FROM web_auth_attempts WHERE identity=?", (identity,)).fetchone()
            if row and row["attempts"] >= 30:
                raise BillingError("尝试次数过多，请在 15 分钟后重试", 429)
            db.execute("""INSERT INTO web_auth_attempts VALUES(?,1,?) ON CONFLICT(identity)
                DO UPDATE SET attempts=attempts+1""", (identity, time.time() + 900))

    @staticmethod
    def password_hash(password, salt):
        return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32).hex()

    def issue_code(self, email, purpose, code):
        email = email.casefold().strip()
        now = time.time()
        salt = secrets.token_hex(16)
        digest = hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()
        with self.billing.catalog._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute("SELECT 1 FROM web_users WHERE email=?", (email,)).fetchone()
            if purpose == "register" and exists:
                raise BillingError("此邮箱已注册，请使用账号密码或邮箱验证码登录", 409)
            if purpose == "login" and not exists:
                raise BillingError("此邮箱尚未绑定账户，请先创建账户", 404)
            row = db.execute("SELECT sent_at FROM email_verification_codes WHERE email=? AND purpose=?",
                             (email, purpose)).fetchone()
            if row and now - row["sent_at"] < 60:
                raise BillingError("验证码已发送，请 60 秒后再试", 429)
            db.execute("""INSERT INTO email_verification_codes VALUES(?,?,?,?,?,?,0)
                ON CONFLICT(email,purpose) DO UPDATE SET salt=excluded.salt,code_hash=excluded.code_hash,
                expires_at=excluded.expires_at,sent_at=excluded.sent_at,attempts=0""",
                (email, purpose, salt, digest, now + 600, now))
        return salt

    def cancel_code(self, email, purpose, challenge):
        with self.billing.catalog._connection() as db:
            db.execute("DELETE FROM email_verification_codes WHERE email=? AND purpose=? AND salt=?",
                       (email.casefold().strip(), purpose, challenge))

    def verify_code(self, email, purpose, code):
        valid = False
        with self.billing.catalog._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM email_verification_codes WHERE email=? AND purpose=?",
                             (email.casefold().strip(), purpose)).fetchone()
            if row and row["expires_at"] > time.time() and row["attempts"] < 5:
                digest = hashlib.sha256(f"{row['salt']}:{code}".encode()).hexdigest()
                valid = hmac.compare_digest(digest, row["code_hash"])
                if valid:
                    db.execute("DELETE FROM email_verification_codes WHERE email=? AND purpose=?", (email.casefold().strip(), purpose))
                else:
                    db.execute("UPDATE email_verification_codes SET attempts=attempts+1 WHERE email=? AND purpose=?",
                               (email.casefold().strip(), purpose))
        if not valid:
            raise BillingError("验证码不正确、已过期或尝试次数过多，请重新获取", 401)

    def register(self, name, email, password, username, code):
        self.verify_code(email, "register", code)
        account_id = uuid.uuid4().hex
        salt = secrets.token_hex(16)
        digest = self.password_hash(password, salt)
        try:
            with self.billing.catalog._connection() as db:
                db.execute("INSERT INTO accounts VALUES(?,?,0,?)", (account_id, name.strip(), utc_now()))
                db.execute("""INSERT INTO web_users(account_id,email,password_salt,password_hash,username,email_verified)
                    VALUES(?,?,?,?,?,1)""", (account_id, email.casefold().strip(), salt, digest, username.casefold()))
        except sqlite3.IntegrityError as exc:
            raise BillingError("该账号或邮箱已注册，请直接登录", 409) from exc
        return self.create_session(Principal(account_id, "web", None))

    def login(self, account, password):
        with self.billing.catalog._connection() as db:
            row = db.execute("SELECT * FROM web_users WHERE email=? OR username=?",
                             (account.casefold().strip(), account.casefold().strip())).fetchone()
        salt = row["password_salt"] if row else "00" * 16
        digest = self.password_hash(password, salt)
        if not row or not hmac.compare_digest(digest, row["password_hash"]):
            raise BillingError("账号或密码不正确", 401)
        return self.create_session(Principal(row["account_id"], "web", None))

    def email_login(self, email, code):
        self.verify_code(email, "login", code)
        with self.billing.catalog._connection() as db:
            row = db.execute("SELECT account_id FROM web_users WHERE email=?", (email.casefold().strip(),)).fetchone()
            if not row:
                raise BillingError("此邮箱尚未绑定账户", 404)
            db.execute("UPDATE web_users SET email_verified=1 WHERE account_id=?", (row["account_id"],))
        return self.create_session(Principal(row["account_id"], "web", None))

    def create_session(self, principal):
        if principal.key_id != "web":
            raise BillingError("API Key 只能调用服务，不能用于网页登录", 403)
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.billing.catalog._connection() as db:
            db.execute("DELETE FROM web_sessions WHERE expires_at<=?", (time.time(),))
            db.execute("INSERT INTO web_sessions VALUES(?,?,?,?)",
                       (digest, principal.account_id, None, time.time() + self.session_ttl))
        return token

    def authenticate(self, token):
        if not token or len(token) > 128:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.billing.catalog._connection() as db:
            db.execute("DELETE FROM web_sessions WHERE expires_at<=?", (time.time(),))
            row = db.execute("SELECT * FROM web_sessions WHERE token_hash=? AND key_id IS NULL", (digest,)).fetchone()
        if not row:
            return None
        return Principal(row["account_id"], "web", None)

    def logout(self, token):
        with self.billing.catalog._connection() as db:
            db.execute("DELETE FROM web_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def me(self, principal):
        with self.billing.catalog._connection() as db:
            row = db.execute("""SELECT a.name,u.email,u.username,u.email_verified FROM accounts a LEFT JOIN web_users u
                ON u.account_id=a.account_id WHERE a.account_id=?""", (principal.account_id,)).fetchone()
        return {"account_id": principal.account_id, "name": row["name"], "email": row["email"],
                "username": row["username"], "email_verified": bool(row["email_verified"]),
                "project_id": principal.project_id, "can_manage": principal.project_id is None}
