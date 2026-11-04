from __future__ import annotations

from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from token_mentor.models import BenchmarkEvidence, ModelProfile, ModelQuote, ResearchTarget, parse_time, utc_now


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
                CREATE TABLE IF NOT EXISTS research_targets (
                    id INTEGER PRIMARY KEY, provider TEXT NOT NULL, region TEXT NOT NULL,
                    aliases_json TEXT NOT NULL, official_domains_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    last_checked_at TEXT, last_status TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(provider, region)
                );
                CREATE TABLE IF NOT EXISTS model_profiles (
                    id INTEGER PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL, region TEXT NOT NULL,
                    payload TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                    UNIQUE(provider, model)
                );
                CREATE TABLE IF NOT EXISTS market_leads (
                    id INTEGER PRIMARY KEY, source_name TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
                    snippet TEXT NOT NULL, scope TEXT NOT NULL, category TEXT NOT NULL, provider TEXT, model TEXT, published_at TEXT,
                    verification_status TEXT NOT NULL DEFAULT 'needs_review', first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                    UNIQUE(source_name, url)
                );
                CREATE TABLE IF NOT EXISTS market_checks (
                    id INTEGER PRIMARY KEY, source_name TEXT NOT NULL, scope TEXT NOT NULL, target_provider TEXT, target_model TEXT,
                    checked_at TEXT NOT NULL, status TEXT NOT NULL, quote_count INTEGER NOT NULL DEFAULT 0,
                    profile_count INTEGER NOT NULL DEFAULT 0, lead_count INTEGER NOT NULL DEFAULT 0,
                    changed_count INTEGER NOT NULL DEFAULT 0, error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_market_checks_time ON market_checks(checked_at DESC);
                CREATE TABLE IF NOT EXISTS price_changes (
                    id INTEGER PRIMARY KEY, source_name TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                    old_input REAL NOT NULL, new_input REAL NOT NULL, old_output REAL NOT NULL, new_output REAL NOT NULL,
                    source_url TEXT NOT NULL, observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'CNY'
                );
                CREATE INDEX IF NOT EXISTS idx_price_changes_time ON price_changes(recorded_at DESC);
                CREATE TABLE IF NOT EXISTS benchmarks (
                    id INTEGER PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
                    benchmark_id TEXT NOT NULL, benchmark_version TEXT NOT NULL, task TEXT NOT NULL,
                    score REAL NOT NULL, max_score REAL NOT NULL, source_url TEXT NOT NULL,
                    observed_at TEXT NOT NULL, protocol TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(provider, model, benchmark_id, benchmark_version, task, protocol, observed_at)
                );
                CREATE INDEX IF NOT EXISTS idx_benchmarks_cohort
                    ON benchmarks(benchmark_id, benchmark_version, task, protocol, observed_at DESC);
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    run_id TEXT PRIMARY KEY, status TEXT NOT NULL, current_node TEXT NOT NULL,
                    input_json TEXT NOT NULL, snapshot_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}', error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
                    node TEXT NOT NULL, output_json TEXT NOT NULL, completed_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, node)
                );
                CREATE TABLE IF NOT EXISTS workflow_events (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
                    node TEXT NOT NULL, event_type TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_events_run ON workflow_events(run_id, id);
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(market_checks)").fetchall()}
            if "changed_count" not in columns:
                db.execute("ALTER TABLE market_checks ADD COLUMN changed_count INTEGER NOT NULL DEFAULT 0")
            check_columns = {row["name"] for row in db.execute("PRAGMA table_info(market_checks)").fetchall()}
            if "target_model" not in check_columns:
                db.execute("ALTER TABLE market_checks ADD COLUMN target_model TEXT")
            lead_columns = {row["name"] for row in db.execute("PRAGMA table_info(market_leads)").fetchall()}
            if "published_at" not in lead_columns:
                db.execute("ALTER TABLE market_leads ADD COLUMN published_at TEXT")
            price_change_columns = {row["name"] for row in db.execute("PRAGMA table_info(price_changes)").fetchall()}
            if "currency" not in price_change_columns:
                db.execute("ALTER TABLE price_changes ADD COLUMN currency TEXT NOT NULL DEFAULT 'CNY'")

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



    def upsert_research_targets(self, rows: Iterable[ResearchTarget]) -> int:
        now = utc_now()
        values = [(row.provider, row.region, json.dumps(row.aliases, ensure_ascii=False),
                   json.dumps(row.official_domains, ensure_ascii=False), int(row.enabled), now, now) for row in rows]
        if not values:
            return 0
        with self._connection() as db:
            before = db.total_changes
            db.executemany("""INSERT INTO research_targets(provider,region,aliases_json,official_domains_json,enabled,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(provider,region) DO UPDATE SET aliases_json=excluded.aliases_json,
                official_domains_json=excluded.official_domains_json,enabled=excluded.enabled,updated_at=excluded.updated_at""", values)
            return db.total_changes - before

    def list_research_targets(self, region: str | None = None, enabled_only: bool = True) -> list[dict[str, object]]:
        query = "SELECT * FROM research_targets WHERE 1=1"
        args: list[object] = []
        if region in {"domestic", "foreign"}:
            query += " AND region=?"; args.append(region)
        if enabled_only:
            query += " AND enabled=1"
        query += " ORDER BY region, provider"
        with self._connection() as db:
            rows = db.execute(query, args).fetchall()
        return [{**dict(row), "aliases": json.loads(row["aliases_json"]), "official_domains": json.loads(row["official_domains_json"])} for row in rows]

    def upsert_model_profiles(self, rows: Iterable[ModelProfile]) -> int:
        now = utc_now()
        values = [(row.provider, row.model, row.region, json.dumps(row.to_dict(), ensure_ascii=False, sort_keys=True), now, now) for row in rows]
        if not values:
            return 0
        with self._connection() as db:
            before = db.total_changes
            db.executemany("""INSERT INTO model_profiles(provider,model,region,payload,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(provider,model) DO UPDATE SET region=excluded.region,payload=excluded.payload,last_seen_at=excluded.last_seen_at""", values)
            return db.total_changes - before

    def list_model_profiles(self, limit: int = 200, region: str | None = None) -> list[dict[str, object]]:
        query = "SELECT payload,first_seen_at,last_seen_at FROM model_profiles"; args: list[object] = []
        if region in {"domestic", "foreign"}:
            query += " WHERE region=?"; args.append(region)
        query += " ORDER BY last_seen_at DESC LIMIT ?"; args.append(max(1, min(limit, 1000)))
        with self._connection() as db:
            rows = db.execute(query, args).fetchall()
        return [{**json.loads(row["payload"]), "first_seen_at": row["first_seen_at"], "last_seen_at": row["last_seen_at"]} for row in rows]

    def upsert_market_leads(self, source_name: str, rows: Iterable[dict[str, object]], *, scope: str = "all", category: str = "news", provider: str | None = None) -> int:
        now = utc_now()
        values = [(source_name, str(row.get("title", ""))[:500], str(row.get("url", "")), str(row.get("snippet", ""))[:5000],
                   scope, category, row.get("provider") or provider, row.get("model"), row.get("published_at"), row.get("verification_status", "needs_review"), now, now) for row in rows if row.get("url")]
        if not values:
            return 0
        with self._connection() as db:
            before = db.total_changes
            db.executemany("""INSERT INTO market_leads(source_name,title,url,snippet,scope,category,provider,model,published_at,verification_status,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_name,url) DO UPDATE SET title=excluded.title,snippet=excluded.snippet,
                scope=excluded.scope,category=excluded.category,provider=COALESCE(excluded.provider,market_leads.provider),model=COALESCE(excluded.model,market_leads.model),
                published_at=COALESCE(excluded.published_at,market_leads.published_at),last_seen_at=excluded.last_seen_at""", values)
            return db.total_changes - before

    def list_market_leads(self, limit: int = 100, scope: str | None = None) -> list[dict[str, object]]:
        query = "SELECT * FROM market_leads"; args: list[object] = []
        if scope in {"domestic", "foreign"}:
            query += " WHERE scope=?"; args.append(scope)
        query += " ORDER BY last_seen_at DESC LIMIT ?"; args.append(max(1, min(limit, 1000)))
        with self._connection() as db:
            return [dict(row) for row in db.execute(query, args).fetchall()]

    def record_market_check(self, source_name: str, scope: str, *, target_provider: str | None = None, target_model: str | None = None, status: str, quote_count: int = 0, profile_count: int = 0, lead_count: int = 0, changed_count: int = 0, error: str | None = None) -> None:
        checked = utc_now()
        with self._connection() as db:
            db.execute("INSERT INTO market_checks(source_name,scope,target_provider,target_model,checked_at,status,quote_count,profile_count,lead_count,changed_count,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (source_name, scope, target_provider, target_model, checked, status, quote_count, profile_count, lead_count, changed_count, error[:2000] if error else None))
            if target_provider:
                db.execute("UPDATE research_targets SET last_checked_at=?,last_status=?,last_error=?,updated_at=? WHERE provider=? AND region IN ('domestic','foreign')",
                           (checked, status, error[:2000] if error else None, checked, target_provider))

    def model_verification_times(self, source_name: str) -> dict[tuple[str, str], str]:
        with self._connection() as db:
            rows = db.execute("""SELECT target_provider,target_model,MAX(checked_at) AS checked_at FROM market_checks
                WHERE source_name=? AND target_model IS NOT NULL GROUP BY target_provider,target_model""", (source_name,)).fetchall()
        return {(row["target_provider"] or "", row["target_model"]): row["checked_at"] for row in rows}

    def record_price_change(self, source_name: str, change: dict[str, object]) -> None:
        with self._connection() as db:
            db.execute("INSERT INTO price_changes(source_name,provider,model,old_input,new_input,old_output,new_output,source_url,observed_at,recorded_at,currency) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (source_name, change["provider"], change["model"], change["old_input"], change["new_input"],
                        change["old_output"], change["new_output"], change["source_url"], change["observed_at"], utc_now(),
                        change.get("currency", "CNY")))

    def list_price_changes(self, limit: int = 100) -> list[dict[str, object]]:
        with self._connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM price_changes ORDER BY recorded_at DESC LIMIT ?",
                    (max(1, min(limit, 1000)),)).fetchall()]

    def list_market_checks(self, limit: int = 100) -> list[dict[str, object]]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM market_checks ORDER BY checked_at DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
        return [dict(row) for row in rows]


    def upsert_benchmarks(self, rows: Iterable[BenchmarkEvidence]) -> int:
        values = []
        for row in rows:
            if not row.source_url or not row.observed_at:
                raise ValueError("benchmark evidence 必须带可溯源 URL 和 observed_at")
            payload = json.dumps(row.to_dict(), sort_keys=True, ensure_ascii=False)
            values.append((row.provider, row.model, row.benchmark_id, row.benchmark_version,
                           row.task, row.score, row.max_score, row.source_url, row.observed_at, row.protocol, payload))
        if not values:
            return 0
        with self._connection() as db:
            before = db.total_changes
            db.executemany(
                """INSERT INTO benchmarks(provider,model,benchmark_id,benchmark_version,task,score,max_score,source_url,observed_at,protocol,payload)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(provider,model,benchmark_id,benchmark_version,task,protocol,observed_at)
                   DO UPDATE SET payload=excluded.payload WHERE benchmarks.payload != excluded.payload""", values)
            return db.total_changes - before

    def benchmark_snapshot(self) -> list[BenchmarkEvidence]:
        with self._connection() as db:
            rows = db.execute("SELECT payload FROM benchmarks ORDER BY provider,model,benchmark_id,benchmark_version,task,protocol,observed_at").fetchall()
        return [BenchmarkEvidence.from_dict(json.loads(row["payload"])) for row in rows]

    def create_workflow_run(self, run_id: str, input_data: dict) -> None:
        now = utc_now()
        with self._connection() as db:
            db.execute("INSERT INTO workflow_runs(run_id,status,current_node,input_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       (run_id, "pending", "collect", json.dumps(input_data, sort_keys=True, ensure_ascii=False), now, now))
            db.execute("INSERT INTO workflow_events(run_id,node,event_type,detail_json,created_at) VALUES(?,?,?,?,?)",
                       (run_id, "collect", "created", "{}", now))

    def get_workflow_run(self, run_id: str) -> dict | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None: return None
            checkpoints = db.execute("SELECT node,output_json,completed_at FROM workflow_checkpoints WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
            events = db.execute("SELECT node,event_type,detail_json,created_at FROM workflow_events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        result = dict(row)
        for key in ("input_json", "snapshot_json", "result_json"):
            result[key] = json.loads(result[key])
        result["checkpoints"] = [{"node":x["node"],"output":json.loads(x["output_json"]),"completed_at":x["completed_at"]} for x in checkpoints]
        result["events"] = [{"node":x["node"],"type":x["event_type"],"detail":json.loads(x["detail_json"]),"created_at":x["created_at"]} for x in events]
        return result

    def checkpoint_workflow_node(self, run_id: str, node: str, output: dict, next_node: str, *, final: bool = False) -> None:
        now = utc_now()
        with self._connection() as db:
            run = db.execute("SELECT status FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None: raise ValueError("workflow run 不存在")
            if run["status"] == "completed": raise ValueError("workflow run 已完成")
            db.execute("INSERT OR IGNORE INTO workflow_checkpoints(run_id,node,output_json,completed_at) VALUES(?,?,?,?)",
                       (run_id,node,json.dumps(output,sort_keys=True,ensure_ascii=False),now))
            db.execute("UPDATE workflow_runs SET status=?,current_node=?,error=NULL,result_json=?,updated_at=? WHERE run_id=?",
                       ("completed" if final else "running",next_node,json.dumps(output if final else {},sort_keys=True,ensure_ascii=False),now,run_id))
            db.execute("INSERT INTO workflow_events(run_id,node,event_type,detail_json,created_at) VALUES(?,?,?,?,?)",
                       (run_id,node,"completed",json.dumps({"next_node":next_node},ensure_ascii=False),now))

    def set_workflow_snapshot(self, run_id: str, snapshot: dict) -> None:
        with self._connection() as db:
            db.execute("UPDATE workflow_runs SET snapshot_json=?,updated_at=? WHERE run_id=? AND snapshot_json='{}'",
                       (json.dumps(snapshot,sort_keys=True,ensure_ascii=False),utc_now(),run_id))

    def fail_workflow_node(self, run_id: str, node: str, error: str) -> None:
        now = utc_now()
        with self._connection() as db:
            db.execute("UPDATE workflow_runs SET status='failed',current_node=?,error=?,updated_at=? WHERE run_id=?",
                       (node,error[:2000],now,run_id))
            db.execute("INSERT INTO workflow_events(run_id,node,event_type,detail_json,created_at) VALUES(?,?,?,?,?)",
                       (run_id,node,"failed",json.dumps({"error":error[:2000]},ensure_ascii=False),now))

    def start_workflow_node(self, run_id: str, node: str) -> None:
        now = utc_now()
        with self._connection() as db:
            run = db.execute("SELECT status FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None: raise ValueError("workflow run 不存在")
            if run["status"] == "completed": raise ValueError("workflow run 已完成")
            db.execute("UPDATE workflow_runs SET status='running',current_node=?,error=NULL,updated_at=? WHERE run_id=?", (node,now,run_id))
            db.execute("INSERT INTO workflow_events(run_id,node,event_type,detail_json,created_at) VALUES(?,?,?,?,?)", (run_id,node,"started","{}",now))
