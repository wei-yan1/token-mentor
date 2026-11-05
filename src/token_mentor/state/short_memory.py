"""Single expiring summary per caller namespace and session."""
import sqlite3
import time
from contextlib import contextmanager


class ShortMemory:
    def __init__(self, path, *, ttl_seconds=1800):
        if ttl_seconds <= 0:
            raise ValueError('记忆有效期必须大于 0')
        self.path = str(path)
        self.ttl_seconds = ttl_seconds
        with self.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS short_memory (
                user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                summary TEXT NOT NULL, expires_at REAL NOT NULL,
                PRIMARY KEY(user_id,session_id))''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                db.execute('''CREATE TABLE IF NOT EXISTS short_memory (
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    summary TEXT NOT NULL, expires_at REAL NOT NULL,
                    PRIMARY KEY(user_id,session_id))''')
                db.execute('DELETE FROM short_memory WHERE expires_at <= ?', (time.time(),))
                yield db
        finally:
            db.close()

    def read(self, user_id, session_id):
        with self.connection() as db:
            row = db.execute('SELECT summary FROM short_memory WHERE user_id=? AND session_id=?',
                             (user_id, session_id)).fetchone()
        return row[0] if row else ''

    def save(self, user_id, session_id, summary):
        with self.connection() as db:
            db.execute('INSERT INTO short_memory VALUES(?,?,?,?) ON CONFLICT(user_id,session_id) '
                       'DO UPDATE SET summary=excluded.summary,expires_at=excluded.expires_at',
                       (user_id, session_id, summary[:2000], time.time() + self.ttl_seconds))

    def clear(self, user_id, session_id):
        with self.connection() as db:
            db.execute('DELETE FROM short_memory WHERE user_id=? AND session_id=?', (user_id, session_id))
