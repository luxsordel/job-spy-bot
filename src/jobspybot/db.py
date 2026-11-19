"""Хранилище на SQLite (stdlib). Все запросы короткие, поэтому выполняются синхронно."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobspybot.models import Vacancy

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    uid          TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    data         TEXT NOT NULL,
    description  TEXT,
    first_seen   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_vacancies_fp ON vacancies(fingerprint);
CREATE INDEX IF NOT EXISTS ix_vacancies_seen ON vacancies(first_seen);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


class Database:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # маленький кэш страниц — экономим RAM
        self.conn.execute("PRAGMA cache_size=-2000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- вакансии ------------------------------------------------------------

    def save_vacancies(self, vacancies: Iterable[Vacancy]) -> set[str]:
        """Сохраняет вакансии, возвращает uid тех, что встретились впервые."""
        now = utcnow().isoformat()
        new: set[str] = set()
        with self.conn:
            for v in vacancies:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO vacancies(uid, source, fingerprint, data, first_seen)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        v.uid,
                        v.source,
                        v.fingerprint,
                        json.dumps(v.to_dict(), ensure_ascii=False),
                        now,
                    ),
                )
                if cur.rowcount:
                    new.add(v.uid)
        return new

    def get_vacancy(self, uid: str) -> Vacancy | None:
        row = self.conn.execute(
            "SELECT data, description FROM vacancies WHERE uid = ?", (uid,)
        ).fetchone()
        if not row:
            return None
        v = Vacancy.from_dict(json.loads(row["data"]))
        v.description = row["description"]
        return v

    def set_description(self, uid: str, text: str) -> None:
        self.conn.execute("UPDATE vacancies SET description = ? WHERE uid = ?", (text, uid))

    def purge_old(self, days: int = 30) -> int:
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        return self.conn.execute("DELETE FROM vacancies WHERE first_seen < ?", (cutoff,)).rowcount
