"""Durable local project registration and configuration version journal."""
from contextlib import contextmanager
import json
import sqlite3
from pathlib import Path


class ConfigVersions:
    def __init__(self, db_path: str | Path) -> None:
        self.path = str(db_path)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS managed_projects (
                    project_id TEXT PRIMARY KEY, config_path TEXT NOT NULL, task TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS config_versions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, payload TEXT NOT NULL);
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, project_id: str, path: str, task: str) -> None:
        with self.connection() as db:
            existing = db.execute('SELECT config_path,task FROM managed_projects WHERE project_id=?', (project_id,)).fetchone()
            if existing and existing != (path, task):
                raise ValueError('项目 ID 已登记到其他配置或用途')
            db.execute('INSERT OR IGNORE INTO managed_projects VALUES(?,?,?)', (project_id, path, task))

    def project(self, project_id: str) -> tuple[str, str]:
        with self.connection() as db:
            row = db.execute('SELECT config_path,task FROM managed_projects WHERE project_id=?', (project_id,)).fetchone()
        if row is None:
            raise ValueError('项目尚未登记')
        return row

    def save(self, version: dict) -> None:
        with self.connection() as db:
            db.execute('INSERT INTO config_versions VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                       (version['id'], version['project_id'], json.dumps(version, ensure_ascii=False)))

    def get(self, version_id: str) -> dict:
        with self.connection() as db:
            row = db.execute('SELECT payload FROM config_versions WHERE id=?', (version_id,)).fetchone()
        if row is None:
            raise ValueError('配置版本不存在')
        return json.loads(row[0])
