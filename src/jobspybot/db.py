"""Хранилище на SQLite (stdlib). Все запросы короткие, поэтому выполняются синхронно."""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobspybot.filters import UserFilters
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

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,          -- Telegram chat id
    username    TEXT NOT NULL DEFAULT '',
    allowed     INTEGER NOT NULL DEFAULT 0,
    paused      INTEGER NOT NULL DEFAULT 0,
    filters     TEXT NOT NULL DEFAULT '{}',
    resume      TEXT NOT NULL DEFAULT '',
    profile     TEXT NOT NULL DEFAULT '',     -- сжатое ИИ резюме
    created_at  TEXT NOT NULL
);

-- что и почему пользователь получил / не получил
CREATE TABLE IF NOT EXISTS deliveries (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vacancy_uid  TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL,               -- sent | filtered | ai_rejected | disliked
    reason       TEXT NOT NULL DEFAULT '',
    score        INTEGER,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, vacancy_uid)
);
CREATE INDEX IF NOT EXISTS ix_deliveries_fp ON deliveries(user_id, fingerprint);

-- оценка вакансии ИИ для конкретного профиля (одна вакансия — один запрос к модели)
CREATE TABLE IF NOT EXISTS ai_cache (
    vacancy_uid   TEXT NOT NULL,
    profile_hash  TEXT NOT NULL,
    result        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (vacancy_uid, profile_hash)
);

CREATE TABLE IF NOT EXISTS ai_usage (
    user_id  INTEGER NOT NULL,
    day      TEXT NOT NULL,
    tokens   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

CREATE TABLE IF NOT EXISTS invites (
    code        TEXT PRIMARY KEY,
    created_by  INTEGER NOT NULL,
    used_by     INTEGER,
    created_at  TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class User:
    id: int
    username: str
    allowed: bool
    paused: bool
    filters: UserFilters
    resume: str
    profile: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> User:
        return cls(
            id=row["id"],
            username=row["username"],
            allowed=bool(row["allowed"]),
            paused=bool(row["paused"]),
            filters=UserFilters.from_json(row["filters"]),
            resume=row["resume"],
            profile=row["profile"],
        )


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
        self.conn.execute("DELETE FROM ai_cache WHERE created_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM ai_usage WHERE day < ?", (cutoff[:10],))
        self.conn.execute("DELETE FROM deliveries WHERE created_at < ?", (cutoff,))
        return self.conn.execute("DELETE FROM vacancies WHERE first_seen < ?", (cutoff,)).rowcount

    # --- пользователи ---------------------------------------------------------

    def get_user(self, user_id: int) -> User | None:
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return User.from_row(row) if row else None

    def ensure_user(self, user_id: int, username: str = "", *, allowed: bool = False) -> User:
        self.conn.execute(
            "INSERT INTO users(id, username, allowed, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET username = excluded.username,"
            " allowed = MAX(users.allowed, excluded.allowed)",
            (user_id, username, int(allowed), utcnow().isoformat()),
        )
        user = self.get_user(user_id)
        assert user is not None
        return user

    def set_allowed(self, user_id: int, allowed: bool) -> None:
        self.conn.execute("UPDATE users SET allowed = ? WHERE id = ?", (int(allowed), user_id))

    def set_paused(self, user_id: int, paused: bool) -> None:
        self.conn.execute("UPDATE users SET paused = ? WHERE id = ?", (int(paused), user_id))

    def save_filters(self, user_id: int, filters: UserFilters) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE users SET filters = ? WHERE id = ?", (filters.to_json(), user_id)
            )
            # фильтры поменялись — отсеянное ранее нужно оценить заново
            self.conn.execute(
                "DELETE FROM deliveries WHERE user_id = ? AND status = 'filtered'", (user_id,)
            )

    def save_resume(self, user_id: int, resume: str, profile: str = "") -> None:
        self.conn.execute(
            "UPDATE users SET resume = ?, profile = ? WHERE id = ?", (resume, profile, user_id)
        )

    def active_users(self) -> list[User]:
        rows = self.conn.execute("SELECT * FROM users WHERE allowed = 1 AND paused = 0")
        return [User.from_row(r) for r in rows]

    def all_users(self) -> list[User]:
        return [User.from_row(r) for r in self.conn.execute("SELECT * FROM users ORDER BY id")]

    # --- инвайты --------------------------------------------------------------

    def create_invite(self, created_by: int) -> str:
        code = secrets.token_urlsafe(9)
        self.conn.execute(
            "INSERT INTO invites(code, created_by, created_at) VALUES (?, ?, ?)",
            (code, created_by, utcnow().isoformat()),
        )
        return code

    def use_invite(self, code: str, user_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE invites SET used_by = ? WHERE code = ? AND used_by IS NULL", (user_id, code)
        )
        if cur.rowcount:
            self.set_allowed(user_id, True)
            return True
        return False

    # --- доставки -------------------------------------------------------------

    def is_known(self, user_id: int, vacancy: Vacancy) -> bool:
        """Вакансия (или её копия с другой площадки) уже обработана для пользователя."""
        row = self.conn.execute(
            "SELECT 1 FROM deliveries WHERE user_id = ? AND (vacancy_uid = ? OR"
            " (fingerprint = ? AND status IN ('sent', 'disliked', 'ai_rejected'))) LIMIT 1",
            (user_id, vacancy.uid, vacancy.fingerprint),
        ).fetchone()
        return row is not None

    def record(
        self,
        user_id: int,
        vacancy: Vacancy,
        status: str,
        reason: str = "",
        score: int | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO deliveries(user_id, vacancy_uid, fingerprint, status, reason, score,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, vacancy_uid) DO UPDATE SET status = excluded.status,"
            " reason = excluded.reason, score = COALESCE(excluded.score, deliveries.score)",
            (
                user_id,
                vacancy.uid,
                vacancy.fingerprint,
                status,
                reason,
                score,
                utcnow().isoformat(),
            ),
        )

    def delivery_id(self, user_id: int, vacancy_uid: str) -> int | None:
        """Короткий числовой id доставки — влезает в callback_data кнопки (64 байта)."""
        row = self.conn.execute(
            "SELECT rowid FROM deliveries WHERE user_id = ? AND vacancy_uid = ?",
            (user_id, vacancy_uid),
        ).fetchone()
        return row[0] if row else None

    def delivery_vacancy(self, user_id: int, delivery_id: int) -> Vacancy | None:
        row = self.conn.execute(
            "SELECT vacancy_uid FROM deliveries WHERE user_id = ? AND rowid = ?",
            (user_id, delivery_id),
        ).fetchone()
        return self.get_vacancy(row[0]) if row else None

    def set_status(self, user_id: int, delivery_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE deliveries SET status = ? WHERE user_id = ? AND rowid = ?",
            (status, user_id, delivery_id),
        )

    def stats(self, user_id: int, days: int = 7) -> dict[str, int]:
        since = (utcnow() - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM deliveries WHERE user_id = ? AND created_at >= ?"
            " GROUP BY status",
            (user_id, since),
        )
        return {r["status"]: r["n"] for r in rows}

    # --- ИИ -------------------------------------------------------------------

    def get_ai_cache(self, vacancy_uid: str, profile_hash: str) -> str | None:
        row = self.conn.execute(
            "SELECT result FROM ai_cache WHERE vacancy_uid = ? AND profile_hash = ?",
            (vacancy_uid, profile_hash),
        ).fetchone()
        return row[0] if row else None

    def set_ai_cache(self, vacancy_uid: str, profile_hash: str, result: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ai_cache(vacancy_uid, profile_hash, result, created_at)"
            " VALUES (?, ?, ?, ?)",
            (vacancy_uid, profile_hash, result, utcnow().isoformat()),
        )

    def add_tokens(self, user_id: int, tokens: int) -> None:
        self.conn.execute(
            "INSERT INTO ai_usage(user_id, day, tokens) VALUES (?, ?, ?)"
            " ON CONFLICT(user_id, day) DO UPDATE SET tokens = tokens + excluded.tokens",
            (user_id, utcnow().date().isoformat(), tokens),
        )

    def tokens_today(self, user_id: int) -> int:
        row = self.conn.execute(
            "SELECT tokens FROM ai_usage WHERE user_id = ? AND day = ?",
            (user_id, utcnow().date().isoformat()),
        ).fetchone()
        return row[0] if row else 0
