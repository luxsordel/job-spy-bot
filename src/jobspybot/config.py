"""Настройки из переменных окружения и файла `.env`.

Сторонние библиотеки (pydantic-settings, python-dotenv) не используются намеренно —
каждый мегабайт RAM на счету.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from jobspybot import __version__

USER_AGENT = f"job-spy-bot/{__version__} (+https://github.com/luxsordel/job-spy-bot)"


class ConfigError(ValueError):
    """Некорректная конфигурация."""


def parse_dotenv(text: str) -> dict[str, str]:
    """Простой парсер `.env`: KEY=VALUE, комментарии `#`, кавычки вокруг значения."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        result[key.strip()] = value
    return result


def load_env(
    path: str | os.PathLike[str] | None = ".env",
    overrides: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Значения из `.env`, затем правки из веб-админки, затем реальные переменные окружения."""
    env: dict[str, str] = {}
    for file in (path, overrides):
        if file and Path(file).is_file():
            env.update(parse_dotenv(Path(file).read_text(encoding="utf-8")))
    env.update(os.environ)
    return env


#: файл с правками настроек из веб-админки — рядом с базой, где у сервиса есть право записи
#: (на VPS /etc только для чтения из-за песочницы systemd)
OVERRIDES_FILE = "settings.env"


def overrides_path(env: Mapping[str, str]) -> Path:
    db_path = Path(env.get("DB_PATH", "").strip() or "data/jobspy.sqlite3")
    return db_path.parent / OVERRIDES_FILE


#: что можно менять из веб-админки. Токены Telegram и самой админки, адрес, порт, база
#: и список админов — только в .env: ошибка в них отрезает доступ к боту.
EDITABLE_KEYS = (
    "ACCESS_MODE",
    "TRIAL_DAYS",
    "PRO_PRICE_STARS",
    "SUPPORT_CONTACT",
    "POLL_INTERVAL_MIN",
    "MAX_VACANCY_AGE_DAYS",
    "ENABLED_SOURCES",
    "TG_CHANNELS",
    "LOG_LEVEL",
    "SUPERJOB_API_KEY",
    "AI_BASE_URL",
    "AI_API_KEY",
    "AI_MODEL",
    "AI_DAILY_TOKENS_PER_USER",
    "AI_MAX_DESCRIPTION_CHARS",
    "AI_MAX_PER_CYCLE",
    "AI_DAILY_TOKENS_TOTAL",
    "AI_PRO_RESERVE_PCT",
    "AI_FALLBACK_MODEL",
    "RATE_LIMIT_PER_MIN",
    "MAX_UPLOADS_PER_DAY",
    "DELETE_INACTIVE_DAYS",
    "ALERT_COOLDOWN_MIN",
    "SOURCE_SILENCE_HOURS",
    "BACKUP_HOUR_UTC",
    "BACKUP_KEEP",
    "BACKUP_TO_TELEGRAM",
)
#: значения не показываются, только «задано / не задано»
SECRET_KEYS = frozenset({"SUPERJOB_API_KEY", "AI_API_KEY"})


def save_overrides(path: Path, changes: Mapping[str, str]) -> dict[str, str]:
    """Дописывает правки в файл переопределений. Пустое значение — убрать правку."""
    current = parse_dotenv(path.read_text(encoding="utf-8")) if path.is_file() else {}
    for key, value in changes.items():
        if key not in EDITABLE_KEYS:
            raise ConfigError(f"{key} нельзя менять из админки")
        value = value.strip()
        if any(ch in value for ch in '\r\n"') or len(value) > 500:
            raise ConfigError(f"{key}: недопустимое значение")
        if value:
            current[key] = value
        else:
            current.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "# Правки из веб-админки. Перекрывают .env, но не переменные окружения.\n"
    body += "".join(f'{k}="{v}"\n' for k, v in sorted(current.items()))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
    return current


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} должно быть целым числом, получено {raw!r}") from exc


def _csv(env: Mapping[str, str], key: str) -> list[str]:
    return [p.strip() for p in env.get(key, "").split(",") if p.strip()]


#: открытые Telegram-каналы с вакансиями, проверенные 2026-11-20: отдают веб-превью t.me/s/
DEFAULT_TG_CHANNELS = (
    "geekjobs",
    "forproducts",
    "jobforjunior",
    "it_vakansii_jobs",
    "pythonrabota",
    "remocate",
    "datasciencejobs",
    "analysts_jobs",
    "hrlunapark",
    "normrabota",
)
_TG_CHANNEL = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
MAX_TG_CHANNELS = 30


def _tg_channels(env: Mapping[str, str]) -> tuple[str, ...]:
    """TG_CHANNELS: имена каналов через запятую (можно с @ или ссылкой t.me/…); «-» — выключить."""
    raw = env.get("TG_CHANNELS", "").strip()
    if not raw:
        return DEFAULT_TG_CHANNELS
    if raw in ("-", "none", "off"):
        return ()
    names = []
    for part in _csv(env, "TG_CHANNELS"):
        name = part.rsplit("/", 1)[-1].lstrip("@")
        if not _TG_CHANNEL.fullmatch(name):
            raise ConfigError(f"TG_CHANNELS: «{part}» — не имя публичного канала")
        names.append(name.lower())
    if len(names) > MAX_TG_CHANNELS:
        raise ConfigError(f"TG_CHANNELS: не больше {MAX_TG_CHANNELS} каналов")
    return tuple(dict.fromkeys(names))


@dataclass(frozen=True, slots=True)
class AISettings:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    daily_tokens_per_user: int = 40_000
    max_description_chars: int = 2_500
    max_per_cycle: int = 10
    #: общий дневной бюджет токенов на весь бот (0 — без общего лимита)
    daily_tokens_total: int = 0
    #: последние N % общего бюджета — только для Pro
    pro_reserve_pct: int = 20
    #: запасная модель, если основная не отвечает (пусто — нет)
    fallback_model: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


@dataclass(frozen=True, slots=True)
class BillingSettings:
    #: invite — только по приглашениям; open — любой по /start получает пробный период
    access_mode: str = "invite"
    trial_days: int = 7
    #: цена Pro за 30 дней в Telegram Stars; 0 — оплата выключена
    pro_price_stars: int = 250
    support_contact: str = ""

    @property
    def payments_enabled(self) -> bool:
        return self.pro_price_stars > 0


@dataclass(frozen=True, slots=True)
class GuardSettings:
    """Защита от злоупотреблений, приватность и оповещения."""

    #: сообщений и нажатий кнопок в минуту на пользователя (админы без лимита)
    rate_limit_per_min: int = 30
    #: загрузок файлов в сутки на пользователя
    max_uploads_per_day: int = 10
    #: удалять данные тех, кто не заходил столько дней (0 — не удалять)
    inactive_days: int = 180
    #: одинаковые ошибки админу — не чаще раза в N минут
    alert_cooldown_min: int = 30
    #: площадка молчит (0 вакансий) дольше N часов — сообщить админу (0 — не следить)
    source_silence_hours: int = 12


@dataclass(frozen=True, slots=True)
class BackupSettings:
    """Ежедневные бэкапы базы (#20)."""

    #: папка для копий; пусто — `backups` рядом с базой
    directory: Path | None = None
    #: сколько последних копий хранить (0 — бэкапы выключены)
    keep: int = 7
    #: в котором часу по UTC делать копию
    hour_utc: int = 3
    #: отправлять свежую копию админам в Telegram (копия вне сервера)
    to_telegram: bool = True


@dataclass(frozen=True, slots=True)
class AdminWebSettings:
    """Веб-админка. Включается, если задан токен и порт."""

    host: str = "127.0.0.1"
    port: int = 0
    token: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.port and self.token)


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_token: str = ""
    admin_ids: frozenset[int] = frozenset()
    db_path: Path = Path("data/jobspy.sqlite3")
    poll_interval_min: int = 30
    max_vacancy_age_days: int = 7
    enabled_sources: frozenset[str] = frozenset()  # пусто = все
    tg_channels: tuple[str, ...] = DEFAULT_TG_CHANNELS
    superjob_api_key: str = ""
    http_timeout: float = 20.0
    user_agent: str = USER_AGENT
    log_level: str = "INFO"
    ai: AISettings = field(default_factory=AISettings)
    billing: BillingSettings = field(default_factory=BillingSettings)
    guard: GuardSettings = field(default_factory=GuardSettings)
    admin_web: AdminWebSettings = field(default_factory=AdminWebSettings)
    backup: BackupSettings = field(default_factory=BackupSettings)

    @property
    def backup_dir(self) -> Path:
        return self.backup.directory or self.db_path.parent / "backups"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        try:
            admin_ids = frozenset(int(x) for x in _csv(env, "ADMIN_IDS"))
        except ValueError as exc:
            raise ConfigError("ADMIN_IDS — список числовых Telegram id через запятую") from exc

        settings = cls(
            telegram_token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
            admin_ids=admin_ids,
            db_path=Path(env.get("DB_PATH", "").strip() or "data/jobspy.sqlite3"),
            poll_interval_min=_int(env, "POLL_INTERVAL_MIN", 30),
            max_vacancy_age_days=_int(env, "MAX_VACANCY_AGE_DAYS", 7),
            enabled_sources=frozenset(s.lower() for s in _csv(env, "ENABLED_SOURCES")),
            tg_channels=_tg_channels(env),
            superjob_api_key=env.get("SUPERJOB_API_KEY", "").strip(),
            log_level=(env.get("LOG_LEVEL", "").strip() or "INFO").upper(),
            billing=BillingSettings(
                access_mode=(env.get("ACCESS_MODE", "").strip() or "invite").lower(),
                trial_days=_int(env, "TRIAL_DAYS", 7),
                pro_price_stars=_int(env, "PRO_PRICE_STARS", 250),
                support_contact=env.get("SUPPORT_CONTACT", "").strip(),
            ),
            ai=AISettings(
                base_url=env.get("AI_BASE_URL", "").strip().rstrip("/"),
                api_key=env.get("AI_API_KEY", "").strip(),
                model=env.get("AI_MODEL", "").strip(),
                daily_tokens_per_user=_int(env, "AI_DAILY_TOKENS_PER_USER", 40_000),
                max_description_chars=_int(env, "AI_MAX_DESCRIPTION_CHARS", 2_500),
                max_per_cycle=_int(env, "AI_MAX_PER_CYCLE", 10),
                daily_tokens_total=_int(env, "AI_DAILY_TOKENS_TOTAL", 0),
                pro_reserve_pct=_int(env, "AI_PRO_RESERVE_PCT", 20),
                fallback_model=env.get("AI_FALLBACK_MODEL", "").strip(),
            ),
            guard=GuardSettings(
                rate_limit_per_min=_int(env, "RATE_LIMIT_PER_MIN", 30),
                max_uploads_per_day=_int(env, "MAX_UPLOADS_PER_DAY", 10),
                inactive_days=_int(env, "DELETE_INACTIVE_DAYS", 180),
                alert_cooldown_min=_int(env, "ALERT_COOLDOWN_MIN", 30),
                source_silence_hours=_int(env, "SOURCE_SILENCE_HOURS", 12),
            ),
            backup=BackupSettings(
                directory=Path(d) if (d := env.get("BACKUP_DIR", "").strip()) else None,
                keep=_int(env, "BACKUP_KEEP", 7),
                hour_utc=_int(env, "BACKUP_HOUR_UTC", 3),
                to_telegram=_int(env, "BACKUP_TO_TELEGRAM", 1) != 0,
            ),
            admin_web=AdminWebSettings(
                host=env.get("ADMIN_WEB_HOST", "").strip() or "127.0.0.1",
                port=_int(env, "ADMIN_WEB_PORT", 0),
                token=env.get("ADMIN_WEB_TOKEN", "").strip(),
            ),
        )
        if settings.admin_web.port and len(settings.admin_web.token) < 16:
            raise ConfigError(
                "ADMIN_WEB_TOKEN — не короче 16 символов (сгенерировать: "
                'python -c "import secrets; print(secrets.token_urlsafe(24))")'
            )
        if settings.guard.rate_limit_per_min < 5:
            raise ConfigError("RATE_LIMIT_PER_MIN не может быть меньше 5")
        if settings.billing.access_mode not in ("invite", "open"):
            raise ConfigError("ACCESS_MODE — invite или open")
        if not 0 <= settings.ai.pro_reserve_pct <= 100:
            raise ConfigError("AI_PRO_RESERVE_PCT — от 0 до 100")
        if settings.ai.daily_tokens_total < 0:
            raise ConfigError("AI_DAILY_TOKENS_TOTAL не может быть отрицательным")
        if not 0 <= settings.backup.hour_utc <= 23:
            raise ConfigError("BACKUP_HOUR_UTC — час от 0 до 23")
        if not 0 <= settings.backup.keep <= 365:
            raise ConfigError("BACKUP_KEEP — от 0 (выключено) до 365 копий")
        if settings.poll_interval_min < 5:
            raise ConfigError("POLL_INTERVAL_MIN не может быть меньше 5 — площадки забанят")
        return settings
