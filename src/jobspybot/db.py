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
from jobspybot.profile import UserProfile

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
    resume      TEXT NOT NULL DEFAULT '',     -- устарело: см. таблицу resumes
    profile     TEXT NOT NULL DEFAULT '',     -- устарело: см. таблицу resumes
    created_at  TEXT NOT NULL
);

-- у пользователя может быть несколько резюме; ИИ выбирает лучшее под вакансию
CREATE TABLE IF NOT EXISTS resumes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    text        TEXT NOT NULL,
    profile     TEXT NOT NULL DEFAULT '',     -- сжатый ИИ профиль (JSON)
    file_id     TEXT,                         -- Telegram file_id: файл не храним, пересылаем
    file_name   TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_resumes_user ON resumes(user_id);

-- что и почему пользователь получил / не получил
CREATE TABLE IF NOT EXISTS deliveries (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vacancy_uid  TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL,  -- sent | queued | skipped | filtered | ai_rejected
                                 -- | liked | disliked
    reason       TEXT NOT NULL DEFAULT '',
    score        INTEGER,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, vacancy_uid)
);
CREATE INDEX IF NOT EXISTS ix_deliveries_fp ON deliveries(user_id, fingerprint);

-- трекер откликов: название и ссылка копируются — старые вакансии из базы удаляются
CREATE TABLE IF NOT EXISTS applications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vacancy_uid  TEXT NOT NULL,
    title        TEXT NOT NULL,
    company      TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,  -- applied | test | interview | offer | rejected
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    reminded     INTEGER NOT NULL DEFAULT 0,  -- напомнили «нет ответа неделю»
    UNIQUE (user_id, vacancy_uid)
);

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

-- профиль соискателя (JSON, см. profile.py)
CREATE TABLE IF NOT EXISTS profiles (
    user_id  INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data     TEXT NOT NULL
);

-- оплаты Telegram Stars (первые и автопродления)
CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    charge_id   TEXT NOT NULL UNIQUE,
    amount      INTEGER NOT NULL,
    currency    TEXT NOT NULL,
    recurring   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    code        TEXT PRIMARY KEY,
    created_by  INTEGER NOT NULL,
    used_by     INTEGER,
    created_at  TEXT NOT NULL
);

-- суточные счётчики действий (загрузки файлов и т. п.) — защита от злоупотреблений
CREATE TABLE IF NOT EXISTS counters (
    user_id  INTEGER NOT NULL,
    day      TEXT NOT NULL,
    key      TEXT NOT NULL,
    n        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day, key)
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
    plan_until: datetime | None = None  # пока в будущем — Pro
    trial_used: bool = False
    sub_charge_id: str | None = None  # активная Stars-подписка (для отмены автопродления)
    last_check: datetime | None = None
    onboarded: bool = False
    notified: str = ""  # последнее отправленное уведомление о подписке
    banned: bool = False
    last_seen: datetime | None = None
    created_at: datetime | None = None
    trial_until: datetime | None = None
    stopped_at: datetime | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> User:
        return cls(
            banned=bool(row["banned"]),
            last_seen=_dt(row["last_seen"]),
            created_at=_dt(row["created_at"]),
            trial_until=_dt(row["trial_until"]),
            stopped_at=_dt(row["stopped_at"]),
            id=row["id"],
            username=row["username"],
            allowed=bool(row["allowed"]),
            paused=bool(row["paused"]),
            filters=UserFilters.from_json(row["filters"]),
            plan_until=_dt(row["plan_until"]),
            trial_used=bool(row["trial_used"]),
            sub_charge_id=row["sub_charge_id"],
            last_check=_dt(row["last_check"]),
            onboarded=bool(row["onboarded"]),
            notified=row["notified"] or "",
        )


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# колонки, добавленные после первой версии схемы: (имя, определение)
_USER_COLUMNS = [
    ("plan_until", "TEXT"),
    ("trial_used", "INTEGER NOT NULL DEFAULT 0"),
    ("sub_charge_id", "TEXT"),
    ("last_check", "TEXT"),
    ("onboarded", "INTEGER NOT NULL DEFAULT 0"),
    ("notified", "TEXT NOT NULL DEFAULT ''"),
    ("banned", "INTEGER NOT NULL DEFAULT 0"),
    ("last_seen", "TEXT"),
    #: конец пробного периода: Pro дольше — значит, оплачен или выдан админом
    ("trial_until", "TEXT"),
    #: когда человек остановил или заблокировал бота (None — бот доступен)
    ("stopped_at", "TEXT"),
]


@dataclass(slots=True)
class Resume:
    id: int
    user_id: int
    title: str
    text: str
    profile: str
    file_id: str | None
    file_name: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Resume:
        return cls(**{k: row[k] for k in cls.__slots__})

    @property
    def skills(self) -> list[str]:
        if not self.profile:
            return []
        try:
            return [str(s) for s in json.loads(self.profile).get("skills") or []]
        except (ValueError, AttributeError):
            return []


#: статусы отклика по порядку воронки
APPLICATION_STATUSES = ("applied", "test", "interview", "offer", "rejected")


@dataclass(slots=True)
class Application:
    id: int
    user_id: int
    vacancy_uid: str
    title: str
    company: str
    url: str
    status: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Application:
        return cls(**{k: row[k] for k in cls.__slots__})

    @property
    def days(self) -> int:
        """Сколько дней без изменений статуса."""
        return max(0, (utcnow() - datetime.fromisoformat(self.updated_at)).days)


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
        self._migrate_columns()
        self._migrate_legacy_resumes()

    def _migrate_columns(self) -> None:
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        for name, definition in _USER_COLUMNS:
            if name not in existing:
                self.conn.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        # существующие пользователи уже настроены — онбординг им не показываем
        if "onboarded" not in existing:
            self.conn.execute("UPDATE users SET onboarded = 1")

    def _migrate_legacy_resumes(self) -> None:
        """Резюме из старой колонки users.resume переносятся в таблицу resumes."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO resumes(user_id, title, text, profile, created_at)"
                " SELECT id, 'Резюме', resume, profile, created_at FROM users"
                " WHERE resume != '' AND id NOT IN (SELECT user_id FROM resumes)"
            )
            self.conn.execute("UPDATE users SET resume = '', profile = '' WHERE resume != ''")

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
        self.conn.execute("DELETE FROM counters WHERE day < ?", (cutoff[:10],))
        self.conn.execute("DELETE FROM deliveries WHERE created_at < ?", (cutoff,))
        return self.conn.execute("DELETE FROM vacancies WHERE first_seen < ?", (cutoff,)).rowcount

    # --- пользователи ---------------------------------------------------------

    def get_user(self, user_id: int) -> User | None:
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return User.from_row(row) if row else None

    def ensure_user(self, user_id: int, username: str = "", *, allowed: bool = False) -> User:
        """Создаёт или обновляет пользователя и отмечает его активность."""
        now = utcnow().isoformat()
        self.conn.execute(
            "INSERT INTO users(id, username, allowed, created_at, last_seen)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET username = excluded.username,"
            " allowed = MAX(users.allowed, excluded.allowed), last_seen = excluded.last_seen",
            (user_id, username, int(allowed), now, now),
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

    def set_stopped(self, user_id: int, stopped: bool) -> None:
        """Человек остановил бота (вместе с паузой рассылки) или вернулся."""
        if stopped:
            self.conn.execute(
                "UPDATE users SET stopped_at = ?, paused = 1 WHERE id = ?",
                (utcnow().isoformat(), user_id),
            )
        else:
            self.conn.execute("UPDATE users SET stopped_at = NULL WHERE id = ?", (user_id,))

    def reset_setup(self, user_id: int) -> None:
        """«Начать заново»: стирает резюме, профиль и фильтры, онбординг — с начала.

        Остаются доступ, подписка, история вакансий (чтобы не прислать их повторно)
        и счётчики (лимит загрузок нельзя обнулить перезапуском).
        """
        with self.conn:
            for table in ("resumes", "profiles"):
                self.conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            self.conn.execute(
                "UPDATE users SET filters = '{}', resume = '', profile = '', paused = 0,"
                " onboarded = 0, stopped_at = NULL, last_check = NULL WHERE id = ?",
                (user_id,),
            )

    def set_banned(self, user_id: int, banned: bool) -> None:
        self.conn.execute("UPDATE users SET banned = ? WHERE id = ?", (int(banned), user_id))

    def active_users(self) -> list[User]:
        rows = self.conn.execute(
            "SELECT * FROM users WHERE allowed = 1 AND paused = 0 AND banned = 0"
        )
        return [User.from_row(r) for r in rows]

    # --- счётчики и удаление данных -------------------------------------------

    def bump(self, user_id: int, key: str) -> int:
        """+1 к суточному счётчику, возвращает новое значение."""
        day = utcnow().date().isoformat()
        self.conn.execute(
            "INSERT INTO counters(user_id, day, key, n) VALUES (?, ?, ?, 1)"
            " ON CONFLICT(user_id, day, key) DO UPDATE SET n = n + 1",
            (user_id, day, key),
        )
        return self.count_today(user_id, key)

    def ever(self, user_id: int, key: str) -> bool:
        """Был ли счётчик хоть раз (например, подсказка уже показывалась)."""
        row = self.conn.execute(
            "SELECT 1 FROM counters WHERE user_id = ? AND key = ? LIMIT 1", (user_id, key)
        ).fetchone()
        return row is not None

    def keys_like(self, user_id: int, prefix: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT key FROM counters WHERE user_id = ? AND key LIKE ?",
            (user_id, prefix.replace("%", "") + "%"),
        )
        return [r[0] for r in rows]

    def reaction_titles(self, user_id: int, status: str, limit: int = 50) -> list[str]:
        """Названия вакансий с реакцией 👍 (liked) или 👎 (disliked), свежие первыми."""
        rows = self.conn.execute(
            "SELECT v.data FROM deliveries d JOIN vacancies v ON v.uid = d.vacancy_uid"
            " WHERE d.user_id = ? AND d.status = ? ORDER BY d.created_at DESC LIMIT ?",
            (user_id, status, limit),
        )
        return [str(json.loads(r[0]).get("title") or "") for r in rows]

    def count_today(self, user_id: int, key: str) -> int:
        row = self.conn.execute(
            "SELECT n FROM counters WHERE user_id = ? AND day = ? AND key = ?",
            (user_id, utcnow().date().isoformat(), key),
        ).fetchone()
        return row[0] if row else 0

    def delete_user_data(self, user_id: int) -> None:
        """Удаляет резюме, профиль, настройки и историю.

        Остаются только id, доступ, срок Pro, факт пробного периода, бан и платежи:
        без них человек мог бы бесконечно брать пробный период, а платежи нужны для
        возвратов и учёта.
        """
        with self.conn:
            for table in (
                "resumes",
                "profiles",
                "deliveries",
                "applications",
                "ai_usage",
                "counters",
            ):
                self.conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            self.conn.execute(
                "UPDATE users SET username = '', filters = '{}', resume = '', profile = '',"
                " paused = 0, onboarded = 0, notified = '', last_check = NULL WHERE id = ?",
                (user_id,),
            )

    def inactive_users(self, days: int) -> list[int]:
        """Кто не писал боту `days` дней, не на Pro и ещё хранит данные."""
        now = utcnow()
        cutoff = (now - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT id FROM users WHERE COALESCE(last_seen, created_at) < ?"
            " AND (plan_until IS NULL OR plan_until < ?)"
            " AND (onboarded = 1 OR filters != '{}'"
            "      OR id IN (SELECT user_id FROM resumes)"
            "      OR id IN (SELECT user_id FROM profiles))",
            (cutoff, now.isoformat()),
        )
        return [r[0] for r in rows]

    def purge_inactive(self, days: int) -> list[int]:
        ids = self.inactive_users(days)
        for uid in ids:
            self.delete_user_data(uid)
        return ids

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
            " (fingerprint = ? AND status IN ('sent', 'liked', 'disliked', 'ai_rejected')))"
            " LIMIT 1",
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

    def queued(self, user_id: int) -> list[tuple[int, Vacancy, str, int | None]]:
        """Отложенные вакансии (тихие часы, дайджест): rowid, вакансия, оценка ИИ, балл.

        Вакансии, удалённые из базы за старостью, сразу помечаются пропущенными.
        """
        rows = self.conn.execute(
            "SELECT d.rowid AS rowid, d.reason, d.score, v.data FROM deliveries d"
            " LEFT JOIN vacancies v ON v.uid = d.vacancy_uid"
            " WHERE d.user_id = ? AND d.status = 'queued' ORDER BY d.created_at",
            (user_id,),
        ).fetchall()
        out = []
        for row in rows:
            if row["data"] is None:
                self.set_status(user_id, row["rowid"], "skipped")
                continue
            vacancy = Vacancy.from_dict(json.loads(row["data"]))
            out.append((row["rowid"], vacancy, row["reason"], row["score"]))
        return out

    def users_with_queue(self) -> list[int]:
        rows = self.conn.execute("SELECT DISTINCT user_id FROM deliveries WHERE status = 'queued'")
        return [r[0] for r in rows]

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

    # --- отклики --------------------------------------------------------------

    def add_application(self, user_id: int, vacancy: Vacancy) -> int:
        """Отметить отклик (повторная отметка не сбрасывает статус). Возвращает id."""
        now = utcnow().isoformat()
        self.conn.execute(
            "INSERT INTO applications(user_id, vacancy_uid, title, company, url, status,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'applied', ?, ?)"
            " ON CONFLICT(user_id, vacancy_uid) DO NOTHING",
            (user_id, vacancy.uid, vacancy.title, vacancy.company, vacancy.url, now, now),
        )
        row = self.conn.execute(
            "SELECT id FROM applications WHERE user_id = ? AND vacancy_uid = ?",
            (user_id, vacancy.uid),
        ).fetchone()
        return int(row[0])

    def applications(self, user_id: int) -> list[Application]:
        """Отклики: сначала те, что в работе (свежие изменения первыми), потом закрытые."""
        rows = self.conn.execute(
            "SELECT * FROM applications WHERE user_id = ?"
            " ORDER BY status IN ('offer', 'rejected'), updated_at DESC",
            (user_id,),
        )
        return [Application.from_row(r) for r in rows]

    def get_application(self, user_id: int, app_id: int) -> Application | None:
        row = self.conn.execute(
            "SELECT * FROM applications WHERE user_id = ? AND id = ?", (user_id, app_id)
        ).fetchone()
        return Application.from_row(row) if row else None

    def set_application_status(self, user_id: int, app_id: int, status: str) -> bool:
        cur = self.conn.execute(
            "UPDATE applications SET status = ?, updated_at = ?, reminded = 0"
            " WHERE user_id = ? AND id = ?",
            (status, utcnow().isoformat(), user_id, app_id),
        )
        return cur.rowcount > 0

    def delete_application(self, user_id: int, app_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM applications WHERE user_id = ? AND id = ?", (user_id, app_id)
        )
        return cur.rowcount > 0

    def stale_applications(self, days: int) -> list[Application]:
        """Отклики без движения `days` дней, о которых ещё не напоминали."""
        before = (utcnow() - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM applications WHERE status = 'applied' AND reminded = 0"
            " AND updated_at < ? ORDER BY user_id, updated_at",
            (before,),
        )
        return [Application.from_row(r) for r in rows]

    def mark_reminded(self, app_id: int) -> None:
        self.conn.execute("UPDATE applications SET reminded = 1 WHERE id = ?", (app_id,))

    def application_funnel(self, user_id: int) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM applications WHERE user_id = ? GROUP BY status",
            (user_id,),
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

    # --- резюме ---------------------------------------------------------------

    def add_resume(
        self,
        user_id: int,
        title: str,
        text: str,
        *,
        file_id: str | None = None,
        file_name: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO resumes(user_id, title, text, file_id, file_name, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, title, text, file_id, file_name, utcnow().isoformat()),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def find_resume(self, user_id: int, text: str) -> Resume | None:
        """Уже загруженное резюме с тем же текстом (повторная отправка того же файла)."""
        row = self.conn.execute(
            "SELECT * FROM resumes WHERE user_id = ? AND text = ? ORDER BY id LIMIT 1",
            (user_id, text),
        ).fetchone()
        return Resume.from_row(row) if row else None

    def set_resume_profile(self, resume_id: int, profile: str) -> None:
        self.conn.execute("UPDATE resumes SET profile = ? WHERE id = ?", (profile, resume_id))

    def get_resumes(self, user_id: int) -> list[Resume]:
        rows = self.conn.execute("SELECT * FROM resumes WHERE user_id = ? ORDER BY id", (user_id,))
        return [Resume.from_row(r) for r in rows]

    def get_resume(self, user_id: int, resume_id: int) -> Resume | None:
        row = self.conn.execute(
            "SELECT * FROM resumes WHERE user_id = ? AND id = ?", (user_id, resume_id)
        ).fetchone()
        return Resume.from_row(row) if row else None

    def delete_resume(self, user_id: int, resume_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM resumes WHERE user_id = ? AND id = ?", (user_id, resume_id)
        )
        return cur.rowcount > 0

    # --- подписка -------------------------------------------------------------

    def set_plan(self, user_id: int, until: datetime, *, trial_used: bool | None = None) -> None:
        self.conn.execute(
            "UPDATE users SET plan_until = ?, notified = '',"
            " trial_used = COALESCE(?, trial_used) WHERE id = ?",
            (until.isoformat(), None if trial_used is None else int(trial_used), user_id),
        )

    def set_trial_until(self, user_id: int, until: datetime) -> None:
        self.conn.execute(
            "UPDATE users SET trial_until = ? WHERE id = ?", (until.isoformat(), user_id)
        )

    def set_subscription(self, user_id: int, charge_id: str | None) -> None:
        self.conn.execute("UPDATE users SET sub_charge_id = ? WHERE id = ?", (charge_id, user_id))

    def set_notified(self, user_id: int, key: str) -> None:
        self.conn.execute("UPDATE users SET notified = ? WHERE id = ?", (key, user_id))

    def set_last_check(self, user_id: int, when: datetime | None = None) -> None:
        self.conn.execute(
            "UPDATE users SET last_check = ? WHERE id = ?",
            ((when or utcnow()).isoformat(), user_id),
        )

    def set_onboarded(self, user_id: int) -> None:
        self.conn.execute("UPDATE users SET onboarded = 1 WHERE id = ?", (user_id,))

    def record_payment(
        self, user_id: int, *, charge_id: str, amount: int, currency: str, recurring: bool
    ) -> bool:
        """Сохраняет платёж; False, если такой charge_id уже был (повтор апдейта)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO payments(user_id, charge_id, amount, currency, recurring,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, charge_id, amount, currency, int(recurring), utcnow().isoformat()),
        )
        return cur.rowcount > 0

    def last_payment(self, user_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT charge_id FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
        return row[0] if row else None

    def has_payments(self, user_id: int) -> bool:
        row = self.conn.execute("SELECT 1 FROM payments WHERE user_id = ? LIMIT 1", (user_id,))
        return row.fetchone() is not None

    def sent_today(self, user_id: int) -> int:
        day = utcnow().date().isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) FROM deliveries WHERE user_id = ?"
            " AND status IN ('sent', 'liked', 'disliked', 'queued') AND created_at >= ?",
            (user_id, day),
        ).fetchone()
        return row[0]

    def business_stats(self) -> dict[str, int]:
        now = utcnow()
        month = now.replace(day=1).date().isoformat()
        week = (now - timedelta(days=7)).isoformat()
        q = self.conn.execute
        return {
            "users": q("SELECT COUNT(*) FROM users WHERE allowed = 1").fetchone()[0],
            "active_week": q(
                "SELECT COUNT(DISTINCT user_id) FROM deliveries WHERE created_at >= ?", (week,)
            ).fetchone()[0],
            "pro": q(
                "SELECT COUNT(*) FROM users WHERE plan_until > ?", (now.isoformat(),)
            ).fetchone()[0],
            "paying": q(
                "SELECT COUNT(*) FROM users WHERE plan_until > ? AND id IN"
                " (SELECT user_id FROM payments)",
                (now.isoformat(),),
            ).fetchone()[0],
            "subscriptions": q(
                "SELECT COUNT(*) FROM users WHERE sub_charge_id IS NOT NULL"
            ).fetchone()[0],
            "stars_month": q(
                "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE currency = 'XTR'"
                " AND created_at >= ?",
                (month,),
            ).fetchone()[0],
            "stars_total": q(
                "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE currency = 'XTR'"
            ).fetchone()[0],
        }

    # --- веб-админка -----------------------------------------------------------

    def funnel(self) -> dict[str, int]:
        """Воронка: зашли → получили доступ → настроили → загрузили резюме → платят."""
        q = self.conn.execute
        return {
            "started": q("SELECT COUNT(*) FROM users").fetchone()[0],
            "allowed": q("SELECT COUNT(*) FROM users WHERE allowed = 1").fetchone()[0],
            "onboarded": q("SELECT COUNT(*) FROM users WHERE onboarded = 1").fetchone()[0],
            "with_resume": q("SELECT COUNT(DISTINCT user_id) FROM resumes").fetchone()[0],
            "paying": q("SELECT COUNT(DISTINCT user_id) FROM payments").fetchone()[0],
            "banned": q("SELECT COUNT(*) FROM users WHERE banned = 1").fetchone()[0],
        }

    def tokens_since(self, day: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM ai_usage WHERE day >= ?", (day,)
        ).fetchone()
        return row[0]

    def search_users(self, query: str = "", *, limit: int = 50, offset: int = 0) -> list[User]:
        """Поиск по id или имени; свежие активные — первыми."""
        sql = "SELECT * FROM users"
        args: list[object] = []
        if query:
            sql += " WHERE CAST(id AS TEXT) LIKE ? ESCAPE '!' OR username LIKE ? ESCAPE '!'"
            escaped = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            like = f"%{escaped}%"
            args += [like, like]
        sql += " ORDER BY COALESCE(last_seen, created_at) DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        return [User.from_row(r) for r in self.conn.execute(sql, args)]

    def resume_counts(self) -> dict[int, int]:
        rows = self.conn.execute("SELECT user_id, COUNT(*) FROM resumes GROUP BY user_id")
        return {r[0]: r[1] for r in rows}

    def list_invites(self, limit: int = 30) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT code, created_by, used_by, created_at FROM invites"
                " ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        )

    def recent_deliveries(self, user_id: int, limit: int = 50) -> list[dict[str, object]]:
        """Что бот делал с вакансиями человека: прислал, отсеял (почему), ИИ отклонил."""
        rows = self.conn.execute(
            "SELECT d.status, d.reason, d.score, d.created_at, v.data FROM deliveries d"
            " LEFT JOIN vacancies v ON v.uid = d.vacancy_uid WHERE d.user_id = ?"
            " ORDER BY d.created_at DESC LIMIT ?",
            (user_id, limit),
        )
        out = []
        for r in rows:
            data = json.loads(r["data"]) if r["data"] else {}
            out.append(
                {
                    "status": r["status"],
                    "reason": r["reason"],
                    "score": r["score"],
                    "at": r["created_at"],
                    "title": data.get("title", ""),
                    "company": data.get("company", ""),
                    "source": data.get("source", ""),
                    "url": data.get("url", ""),
                }
            )
        return out

    def payments_of(self, user_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT amount, currency, recurring, created_at FROM payments"
                " WHERE user_id = ? ORDER BY id DESC",
                (user_id,),
            )
        )

    # --- профиль ---------------------------------------------------------------

    def get_profile(self, user_id: int) -> UserProfile:
        row = self.conn.execute("SELECT data FROM profiles WHERE user_id = ?", (user_id,))
        found = row.fetchone()
        return UserProfile.from_json(found[0] if found else None)

    def save_profile(self, user_id: int, profile: UserProfile) -> None:
        self.conn.execute(
            "INSERT INTO profiles(user_id, data) VALUES (?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET data = excluded.data",
            (user_id, profile.to_json()),
        )
