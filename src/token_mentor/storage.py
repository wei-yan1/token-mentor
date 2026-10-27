from __future__ import annotations

from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from .models import ModelQuote, parse_time, utc_now


class Catalog:
    """SQLite-backed shared quote, recommendation-cache, and feedback catalog."""

    def __init__(self, path: str | Path = ".token-mentor/catalog.sqlite3") -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS quotes (
                    id INTEGER PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(provider, model, source_url, observed_at)
                );
                CREATE INDEX IF NOT EXISTS idx_quotes_model_time
                    ON quotes(provider, model, observed_at DESC);
                CREATE TABLE IF NOT EXISTS recommendation_cache (
                    cache_key TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    report TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY,
                    report_id TEXT NOT NULL,
                    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_report ON feedback(report_id);
                CREATE TABLE IF NOT EXISTS discoveries (
                    id INTEGER PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    snippet TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    UNIQUE(source_name, url)
                );
                CREATE INDEX IF NOT EXISTS idx_discoveries_time ON discoveries(discovered_at DESC);
                """
            )

    def upsert_quotes(self, quotes: Iterable[ModelQuote]) -> int:
        rows = [
            (q.provider, q.model, q.source_url, q.observed_at, json.dumps(q.to_dict(), ensure_ascii=False, sort_keys=True))
            for q in quotes
        ]
        if not rows:
            return 0
        with self._connection() as db:
            before = db.total_changes
            db.executemany(
                """INSERT INTO quotes(provider, model, source_url, observed_at, payload)
                   VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(provider, model, source_url, observed_at)
                   DO UPDATE SET payload=excluded.payload WHERE quotes.payload != excluded.payload""",
                rows,
            )
            return db.total_changes - before

    def latest_quotes(self) -> list[ModelQuote]:
        with self._connection() as db:
            rows = db.execute(
                """SELECT q.payload FROM quotes q
                   JOIN (
                     SELECT provider, model, MAX(observed_at) AS newest
                     FROM quotes GROUP BY provider, model
                   ) latest ON latest.provider=q.provider AND latest.model=q.model AND latest.newest=q.observed_at
                   ORDER BY q.provider, q.model, q.observed_at, q.source_url"""
            ).fetchall()
        quotes: dict[tuple[str, str], ModelQuote] = {}
        for row in rows:
            quote = ModelQuote.from_dict(json.loads(row["payload"]))
            key = quote.provider, quote.model
            current = quotes.get(key)
            if (
                current is None
                or parse_time(quote.observed_at) > parse_time(current.observed_at)
                or (quote.observed_at == current.observed_at and quote.source_url > current.source_url)
            ):
                quotes[key] = quote
        return list(quotes.values())

    def quote_fingerprint(self, quotes: Iterable[ModelQuote]) -> str:
        import hashlib

        payload = [q.to_dict() for q in sorted(quotes, key=lambda x: (x.provider, x.model, x.observed_at))]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def get_cached_report(self, cache_key: str) -> str | None:
        now = utc_now()
        with self._connection() as db:
            row = db.execute(
                "SELECT report, expires_at FROM recommendation_cache WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            if parse_time(row["expires_at"]) <= parse_time(now):
                db.execute("DELETE FROM recommendation_cache WHERE cache_key=?", (cache_key,))
                return None
            return str(row["report"])

    def cache_report(self, cache_key: str, report: str, ttl_seconds: int = 86_400) -> None:
        if ttl_seconds <= 0:
            raise ValueError("缓存 TTL 必须大于 0")
        created = datetime.now(UTC)
        expires = (created + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self._connection() as db:
            db.execute(
                """INSERT INTO recommendation_cache(cache_key, created_at, expires_at, report)
                   VALUES(?, ?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                     created_at=excluded.created_at, expires_at=excluded.expires_at, report=excluded.report""",
                (cache_key, created.isoformat(timespec="seconds").replace("+00:00", "Z"), expires, report),
            )

    def record_feedback(self, report_id: str, rating: int, note: str = "") -> None:
        if not 1 <= rating <= 5:
            raise ValueError("rating 取值范围是 1 到 5")
        with self._connection() as db:
            row = db.execute(
                "SELECT cache_key FROM recommendation_cache WHERE substr(cache_key, 1, 16)=? LIMIT 1",
                (report_id,),
            ).fetchone()
            if row is None:
                raise ValueError("报告 ID 未找到或缓存已过期；反馈需在报告生成后提交")
            db.execute(
                "INSERT INTO feedback(report_id, rating, note, created_at) VALUES(?, ?, ?, ?)",
                (str(row["cache_key"]), rating, note.strip()[:2_000], utc_now()),
            )

    def list_feedback(self, limit: int = 100) -> list[dict[str, str | int]]:
        with self._connection() as db:
            rows = db.execute(
                """SELECT substr(report_id, 1, 16) AS report_id, rating, note, created_at
                   FROM feedback ORDER BY created_at DESC LIMIT ?""",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune(self, *, quote_days: int = 90) -> tuple[int, int]:
        cutoff = (datetime.now(UTC) - timedelta(days=quote_days)).isoformat(timespec="seconds").replace("+00:00", "Z")
        now = utc_now()
        with self._connection() as db:
            quotes = db.execute("DELETE FROM quotes WHERE observed_at < ?", (cutoff,)).rowcount
            cache = db.execute("DELETE FROM recommendation_cache WHERE expires_at <= ?", (now,)).rowcount
        return quotes, cache

    def upsert_discoveries(self, source_name: str, rows: Iterable[dict[str, str]]) -> int:
        discovered_at = utc_now()
        values = [
            (source_name, row["title"], row["url"], row["snippet"], discovered_at)
            for row in rows
        ]
        if not values:
            return 0
        with self._connection() as db:
            before = db.total_changes
            db.executemany(
                """INSERT INTO discoveries(source_name, title, url, snippet, discovered_at)
                   VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(source_name, url) DO UPDATE SET
                     title=excluded.title, snippet=excluded.snippet, discovered_at=excluded.discovered_at""",
                values,
            )
            return db.total_changes - before

    def list_discoveries(self, limit: int = 100) -> list[dict[str, str]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT source_name, title, url, snippet, discovered_at FROM discoveries ORDER BY discovered_at DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

