"""Веб-админка: обзор, пользователи, приглашения, рассылка — в браузере, а не в Telegram.

Без сторонних библиотек: маленький HTTP-сервер на `asyncio.start_server`, страницы —
строки HTML. По умолчанию слушает только 127.0.0.1: на VPS открывается через SSH-туннель
`ssh -L 8080:127.0.0.1:8080 user@host`, наружу порт не публикуется.

Вход — по `ADMIN_WEB_TOKEN`. Сессия — HttpOnly-кука с HMAC от токена (смена токена
разлогинивает всех), формы защищены CSRF-полем и `SameSite=Strict`.

`/api/*` — то же для программ (и Claude): заголовок `Authorization: Bearer <токен>`,
ответы в JSON. Логи, настройки, перезапуск, пользователи и их вакансии — без SSH.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote, urlsplit

from jobspybot import __version__
from jobspybot.backup import Backups, run_and_send
from jobspybot.billing import PRO, Billing
from jobspybot.config import (
    EDITABLE_KEYS,
    SECRET_KEYS,
    ConfigError,
    Settings,
    load_env,
    parse_dotenv,
    save_overrides,
)
from jobspybot.db import Database, User, utcnow
from jobspybot.limits import MAX_GRANT_DAYS, parse_int
from jobspybot.telegram import render as r

if TYPE_CHECKING:
    from jobspybot.guard import AdminAlerts, LogBuffer
    from jobspybot.pipeline import Pipeline
    from jobspybot.telegram.bot import Bot

log = logging.getLogger(__name__)

COOKIE = "jsb_admin"
MAX_BODY = 64 * 1024
PAGE_SIZE = 50
#: попыток входа за 10 минут, после — пауза
MAX_LOGIN_FAILURES = 5

STATUS_TEXT = {
    200: "OK",
    303: "See Other",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
}


@dataclass(slots=True)
class Request:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    form: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> dict[str, Any]:
        try:
            data = json.loads(self.body or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def cookie(self, name: str) -> str:
        for part in self.headers.get("cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""


@dataclass(slots=True)
class Response:
    status: int = 200
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = "text/html; charset=utf-8"

    def encode(self) -> bytes:
        payload = self.body.encode("utf-8")
        head = [
            f"HTTP/1.1 {self.status} {STATUS_TEXT.get(self.status, 'OK')}",
            f"Content-Type: {self.content_type}",
            f"Content-Length: {len(payload)}",
            "Connection: close",
            "Cache-Control: no-store",
            "X-Content-Type-Options: nosniff",
            "X-Frame-Options: DENY",
            "Referrer-Policy: no-referrer",
            "Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            *(f"{k}: {v}" for k, v in self.headers.items()),
        ]
        return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + payload


def json_response(data: Any, status: int = 200) -> Response:
    body = json.dumps(data, ensure_ascii=False, default=str, indent=1)
    return Response(status, body, content_type="application/json; charset=utf-8")


@dataclass
class Runtime:
    """Связь админки с процессом: настройки, логи, остановка и перезапуск."""

    env: dict[str, str] = field(default_factory=dict)
    overrides: Path | None = None
    logs: LogBuffer | None = None
    stop: asyncio.Event | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    restart_requested: bool = False

    def request_restart(self) -> bool:
        if self.stop is None:
            return False
        self.restart_requested = True
        self.stop.set()
        return True


def effective_values(s: Settings) -> dict[str, str]:
    """Действующие значения настроек (в том числе значения по умолчанию)."""
    b, ai, g = s.billing, s.ai, s.guard
    values: dict[str, object] = {
        "ACCESS_MODE": b.access_mode,
        "TRIAL_DAYS": b.trial_days,
        "PRO_PRICE_STARS": b.pro_price_stars,
        "SUPPORT_CONTACT": b.support_contact,
        "POLL_INTERVAL_MIN": s.poll_interval_min,
        "MAX_VACANCY_AGE_DAYS": s.max_vacancy_age_days,
        "ENABLED_SOURCES": ",".join(sorted(s.enabled_sources)),
        "LOG_LEVEL": s.log_level,
        "SUPERJOB_API_KEY": s.superjob_api_key,
        "AI_BASE_URL": ai.base_url,
        "AI_API_KEY": ai.api_key,
        "AI_MODEL": ai.model,
        "AI_DAILY_TOKENS_PER_USER": ai.daily_tokens_per_user,
        "AI_MAX_DESCRIPTION_CHARS": ai.max_description_chars,
        "AI_MAX_PER_CYCLE": ai.max_per_cycle,
        "AI_DAILY_TOKENS_TOTAL": ai.daily_tokens_total,
        "AI_PRO_RESERVE_PCT": ai.pro_reserve_pct,
        "AI_FALLBACK_MODEL": ai.fallback_model,
        "RATE_LIMIT_PER_MIN": g.rate_limit_per_min,
        "MAX_UPLOADS_PER_DAY": g.max_uploads_per_day,
        "DELETE_INACTIVE_DAYS": g.inactive_days,
        "ALERT_COOLDOWN_MIN": g.alert_cooldown_min,
        "SOURCE_SILENCE_HOURS": g.source_silence_hours,
        "BACKUP_HOUR_UTC": s.backup.hour_utc,
        "BACKUP_KEEP": s.backup.keep,
        "BACKUP_TO_TELEGRAM": int(s.backup.to_telegram),
    }
    return {k: str(v) for k, v in values.items()}


def redirect(location: str, **headers: str) -> Response:
    return Response(303, "", {"Location": location, **headers})


def fmt_dt(dt: datetime | None) -> str:
    return dt.astimezone(UTC).strftime("%d.%m.%Y %H:%M") if dt else "—"


def _num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


# --- оформление ---------------------------------------------------------------

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#667085;--line:#e4e7ec;
--accent:#3563e9;--accent-text:#fff;--ok:#12805c;--warn:#b54708;--bad:#c4320a;
--ok-bg:#ecfdf3;--warn-bg:#fffaeb;--bad-bg:#fef3f2;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b24;--text:#e6e8ee;
--muted:#98a2b3;--line:#2a303c;--accent:#7c9cff;--accent-text:#0f1218;--ok:#47cd89;
--warn:#fdb022;--bad:#f97066;--ok-bg:#0b2a1e;--warn-bg:#2e2205;--bad-bg:#33130f;
color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line)}
.bar{max-width:1100px;margin:0 auto;padding:10px 16px;display:flex;gap:6px 18px;
flex-wrap:wrap;align-items:center}
.brand{font-weight:700;margin-right:8px}
nav a{color:var(--muted);text-decoration:none;padding:4px 0}
nav a.on,nav a:hover{color:var(--text)}
nav{display:flex;gap:16px;flex-wrap:wrap;flex:1}
main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 16px}h2{font-size:17px;margin:28px 0 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card .v{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}
.card .k{color:var(--muted);font-size:13px}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);
white-space:nowrap;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:13px}
tr:last-child td{border-bottom:0}
td.wrap{white-space:normal;min-width:240px}
a{color:var(--accent)}
.muted{color:var(--muted)}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;
background:var(--line)}
.pill.ok{background:var(--ok-bg);color:var(--ok)}.pill.warn{background:var(--warn-bg);
color:var(--warn)}.pill.bad{background:var(--bad-bg);color:var(--bad)}
.flash{background:var(--ok-bg);color:var(--ok);padding:10px 14px;border-radius:8px;
margin-bottom:16px}
.flash.bad{background:var(--bad-bg);color:var(--bad)}
form.inline{display:inline-flex;gap:6px;align-items:center;margin:0 8px 8px 0}
input,textarea,select{font:inherit;color:var(--text);background:var(--bg);
border:1px solid var(--line);border-radius:8px;padding:7px 10px;max-width:100%}
textarea{width:100%;min-height:120px}
button{font:inherit;border:0;border-radius:8px;padding:8px 14px;cursor:pointer;
background:var(--accent);color:var(--accent-text)}
button.ghost{background:var(--line);color:var(--text)}
button.danger{background:var(--bad);color:#fff}
.tg{white-space:pre-wrap;background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:14px;overflow-wrap:anywhere}
.funnel div{display:flex;gap:10px;align-items:center;margin:6px 0}
.funnel .lbl{width:170px;flex:none}.funnel .bar2{height:10px;border-radius:99px;
background:var(--accent);min-width:2px}
.funnel .n{font-variant-numeric:tabular-nums;color:var(--muted)}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}
table.kv td:last-child{white-space:normal;overflow-wrap:anywhere}
@media (max-width:600px){.funnel .lbl{width:120px}
.bar{justify-content:space-between}.bar nav{order:3;flex-basis:100%;gap:14px}
.cols{grid-template-columns:1fr}}
"""

NAV = [
    ("/", "Обзор"),
    ("/users", "Пользователи"),
    ("/invites", "Приглашения"),
    ("/broadcast", "Рассылка"),
    ("/logs", "Логи"),
    ("/settings", "Настройки"),
]


def page(title: str, body: str, *, active: str = "", flash: str = "", bad: bool = False) -> str:
    links = "".join(
        f'<a href="{href}"{" class=on" if href == active else ""}>{label}</a>'
        for href, label in NAV
    )
    note = f'<div class="flash{" bad" if bad else ""}">{escape(flash)}</div>' if flash else ""
    header = (
        f'<header><div class="bar"><span class="brand">Job Spy · админка</span>'
        f'<nav>{links}</nav><a class="muted" href="/logout">Выйти</a></div></header>'
        if active
        else ""
    )
    return (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)} · Job Spy</title><style>{CSS}</style></head><body>"
        f"{header}<main>{note}<h1>{escape(title)}</h1>{body}</main></body></html>"
    )


def table(
    head: Iterable[str], rows: Iterable[Iterable[str]], empty: str = "Пусто", *, kv: bool = False
) -> str:
    """Ячейки — уже готовый HTML (экранирование на вызывающем). kv — таблица «поле: значение»."""
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    if not body:
        return f'<p class="muted">{escape(empty)}</p>'
    th = "".join(f"<th>{escape(h)}</th>" for h in head)
    cls = ' class="kv"' if kv else ""
    return f'<div class="scroll"><table{cls}><tr>{th}</tr>{body}</table></div>'


def card(label: str, value: str) -> str:
    return (
        f'<div class="card"><div class="v">{value}</div><div class="k">{escape(label)}</div></div>'
    )


@dataclass
class Broadcast:
    text: str
    segment: str
    total: int
    sent: int = 0
    failed: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None


API_ENDPOINTS = [
    "GET /api/status",
    "GET /api/logs?lines=200&level=WARNING&q=текст",
    "GET /api/config",
    "POST /api/config {KEY: value}",
    "POST /api/restart",
    "POST /api/cycle",
    "GET /api/users?q=",
    "GET /api/user?id=ID",
    "POST /api/user {id, action: grant|allow|deny|ban|unban|pause|unpause|reset|message|"
    "delete, ...}",
    "GET /api/backups",
    "POST /api/backup",
]

SEGMENTS = {
    "all": "Все с доступом",
    "free": "Только Free",
    "pro": "Только Pro",
    "no_resume": "Без резюме",
}


class AdminWeb:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        billing: Billing,
        *,
        bot: Bot | None = None,
        pipeline: Pipeline | None = None,
        alerts: AdminAlerts | None = None,
        runtime: Runtime | None = None,
        backups: Backups | None = None,
    ) -> None:
        self.backups = backups
        self.runtime = runtime or Runtime()
        self.settings = settings
        self.db = db
        self.billing = billing
        self.bot = bot
        self.pipeline = pipeline
        self.alerts = alerts
        token = settings.admin_web.token.encode()
        self._session = hmac.new(token, b"session", hashlib.sha256).hexdigest()
        self._csrf = hmac.new(token, b"csrf", hashlib.sha256).hexdigest()[:32]
        self._failures: deque[float] = deque()
        self.broadcast: Broadcast | None = None
        self._broadcast_task: asyncio.Task[None] | None = None
        self.port = settings.admin_web.port

    # --- сервер ---------------------------------------------------------------

    async def serve(self, stop: asyncio.Event, ready: asyncio.Event | None = None) -> None:
        cfg = self.settings.admin_web
        server = await asyncio.start_server(self._client, cfg.host, cfg.port)
        self.port = server.sockets[0].getsockname()[1]
        log.info("admin web on http://%s:%d", cfg.host, self.port)
        if ready:
            ready.set()
        async with server:
            await stop.wait()
        if self._broadcast_task:
            self._broadcast_task.cancel()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            response = await self._read_and_handle(reader)
            writer.write(response.encode())
            await writer.drain()
        except (ConnectionError, TimeoutError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("admin web request failed")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _read_and_handle(self, reader: asyncio.StreamReader) -> Response:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
        except asyncio.LimitOverrunError:
            return Response(413, "Too large")
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            return Response(400, "Bad request")
        method, target, _ = parts
        headers: dict[str, str] = {}
        for line in lines[1:]:
            key, sep, value = line.partition(":")
            if sep:
                headers[key.strip().lower()] = value.strip()
        length = parse_int(headers.get("content-length", "0") or "0") or 0
        if length < 0 or length > MAX_BODY:
            return Response(413, "Too large")
        body = await asyncio.wait_for(reader.readexactly(length), timeout=15) if length else b""
        url = urlsplit(target)
        request = Request(
            method=method.upper(),
            path=url.path or "/",
            query=dict(parse_qsl(url.query)),
            headers=headers,
            form=dict(parse_qsl(body.decode("utf-8", "replace")))
            if body and "json" not in headers.get("content-type", "")
            else {},
            body=body,
        )
        return await self.handle(request)

    # --- маршруты -------------------------------------------------------------

    async def handle(self, req: Request) -> Response:
        if req.path == "/health":
            return Response(200, "ok", content_type="text/plain; charset=utf-8")
        if req.path == "/login":
            return self._login(req)
        if req.path.startswith("/api/"):
            return await self._api(req)
        if not hmac.compare_digest(req.cookie(COOKIE), self._session):
            return redirect("/login")
        if req.method == "POST" and not hmac.compare_digest(req.form.get("csrf", ""), self._csrf):
            return Response(403, page("Сессия устарела", '<a href="/">Обновите страницу</a>'))
        routes = {
            ("GET", "/"): self._dashboard,
            ("GET", "/users"): self._users,
            ("GET", "/user"): self._user,
            ("POST", "/user"): self._user_action,
            ("GET", "/invites"): self._invites,
            ("POST", "/invites"): self._create_invite,
            ("GET", "/broadcast"): self._broadcast_page,
            ("POST", "/broadcast"): self._start_broadcast,
            ("GET", "/logout"): self._logout,
            ("GET", "/logs"): self._logs_page,
            ("GET", "/settings"): self._settings_page,
            ("POST", "/settings"): self._save_settings,
            ("POST", "/restart"): self._restart,
        }
        handler = routes.get((req.method, req.path))
        if handler is None:
            known = {path for _, path in routes}
            return Response(405 if req.path in known else 404, page("Не найдено", ""))
        return await handler(req)

    def _form(self, action: str, fields: str, button_html: str, *, cls: str = "inline") -> str:
        return (
            f'<form class="{cls}" method="post" action="{action}">'
            f'<input type="hidden" name="csrf" value="{self._csrf}">{fields}{button_html}</form>'
        )

    # --- вход -----------------------------------------------------------------

    def _login(self, req: Request) -> Response:
        error = ""
        if req.method == "POST":
            now = time.monotonic()
            while self._failures and self._failures[0] < now - 600:
                self._failures.popleft()
            if len(self._failures) >= MAX_LOGIN_FAILURES:
                return Response(
                    429, page("Вход", "<p>Слишком много попыток. Подождите 10 минут.</p>")
                )
            token = req.form.get("token", "").strip()
            if hmac.compare_digest(token.encode(), self.settings.admin_web.token.encode()):
                self._failures.clear()
                cookie = (
                    f"{COOKIE}={self._session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"
                )
                return redirect("/", **{"Set-Cookie": cookie})
            self._failures.append(now)
            log.warning("admin web: wrong token")
            error = "Неверный токен"
        body = (
            '<form method="post" action="/login" class="card" style="max-width:420px">'
            '<p class="muted">Токен — значение <code>ADMIN_WEB_TOKEN</code> из .env</p>'
            '<p><input type="password" name="token" autofocus required '
            'style="width:100%" autocomplete="current-password"></p>'
            "<button>Войти</button></form>"
        )
        return Response(401 if error else 200, page("Вход", body, flash=error, bad=True))

    async def _logout(self, req: Request) -> Response:
        return redirect("/login", **{"Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/"})

    # --- обзор ----------------------------------------------------------------

    async def _dashboard(self, req: Request) -> Response:
        s = self.db.business_stats()
        funnel = self.db.funnel()
        now = utcnow()
        today = now.date().isoformat()
        month = now.replace(day=1).date().isoformat()
        cards = "".join(
            [
                card("Пользователи с доступом", _num(s["users"])),
                card("Активны за неделю", _num(s["active_week"])),
                card("На Pro", _num(s["pro"])),
                card("Платят", _num(s["paying"])),
                card("Автопродление", _num(s["subscriptions"])),
                card("Звёзды за месяц", f"{_num(s['stars_month'])} ⭐"),
                card("Звёзды всего", f"{_num(s['stars_total'])} ⭐"),
                card("ИИ-токены сегодня", self._ai_today(today)),
                card("ИИ-токены за месяц", _num(self.db.tokens_since(month))),
            ]
        )
        steps = [
            ("Нажали /start", funnel["started"]),
            ("Получили доступ", funnel["allowed"]),
            ("Настроили поиск", funnel["onboarded"]),
            ("Загрузили резюме", funnel["with_resume"]),
            ("Оплатили", funnel["paying"]),
        ]
        top = max(1, funnel["started"])
        funnel_html = (
            '<div class="card funnel">'
            + "".join(
                f'<div><span class="lbl">{escape(label)}</span>'
                f'<span class="bar2" style="width:{max(1, n * 60 // top)}%"></span>'
                f'<span class="n">{_num(n)} · {n * 100 // top}%</span></div>'
                for label, n in steps
            )
            + (
                f'<p class="muted">Заблокировано: {_num(funnel["banned"])}</p>'
                if funnel["banned"]
                else ""
            )
            + "</div>"
        )

        body = f'<div class="grid">{cards}</div><h2>Воронка</h2>{funnel_html}'
        body += "<h2>Площадки</h2>" + self._sources_table()
        if self.pipeline and self.pipeline.last_cycle:
            c = self.pipeline.last_cycle
            body += (
                f'<p class="muted">Последний цикл {fmt_dt(self.pipeline.last_cycle_at)} UTC: '
                f"найдено {c.fetched}, новых {c.new}, отсеяно {c.filtered}, ИИ {c.ai_calls} "
                f"(отклонено {c.ai_rejected}), отправлено {c.sent}</p>"
            )
        body += "<h2>Ошибки и оповещения</h2>" + self._alerts_table()
        cfg, guard = self.settings.billing, self.settings.guard
        body += "<h2>Настройки</h2>" + table(
            ["Параметр", "Значение"],
            [
                ("Версия", escape(__version__)),
                ("Доступ", escape(cfg.access_mode)),
                ("Пробный период", f"{cfg.trial_days} дн."),
                ("Цена Pro", f"{cfg.pro_price_stars} ⭐ / 30 дн."),
                ("Сообщений в минуту", str(guard.rate_limit_per_min)),
                ("Файлов в сутки", str(guard.max_uploads_per_day)),
                (
                    "Удаление неактивных",
                    f"через {guard.inactive_days} дн." if guard.inactive_days else "выключено",
                ),
                ("Опрос площадок", f"каждые {self.settings.poll_interval_min} мин."),
            ],
            kv=True,
        )
        return Response(200, page("Обзор", body, active="/"))

    def _sources_table(self) -> str:
        if self.pipeline is None:
            return '<p class="muted">Пайплайн не подключён</p>'
        now = datetime.now(UTC)
        silence = timedelta(hours=self.settings.guard.source_silence_hours or 12)
        rows = []
        for name, st in sorted(self.pipeline.status.items()):
            if st.checked_at is None:
                state = '<span class="pill">ещё не опрашивалась</span>'
            elif st.error_at and (not st.found_at or st.error_at > st.found_at):
                state = '<span class="pill bad">ошибка</span>'
            elif st.found_at and now - st.found_at < silence:
                state = '<span class="pill ok">работает</span>'
            else:
                state = '<span class="pill warn">молчит</span>'
            rows.append(
                (
                    escape(name),
                    state,
                    fmt_dt(st.checked_at),
                    str(st.last_count),
                    fmt_dt(st.found_at),
                    f'<span class="muted">{escape(st.error[:120])}</span>' if st.error else "",
                )
            )
        return table(
            ["Площадка", "Статус", "Проверка", "Найдено", "Последняя вакансия", "Ошибка"], rows
        )

    def _alerts_table(self) -> str:
        if self.alerts is None or not self.alerts.recent:
            return '<p class="muted">Ошибок нет 🎉</p>'
        rows = [
            (
                fmt_dt(a.at),
                f"×{a.count}" if a.count > 1 else "",
                f'<div style="white-space:pre-wrap;min-width:260px">{escape(a.text)}</div>',
            )
            for a in reversed(self.alerts.recent)
        ]
        return table(["Когда (UTC)", "", "Что"], rows)

    # --- пользователи ---------------------------------------------------------

    def _badge(self, u: User) -> str:
        if u.banned:
            return '<span class="pill bad">бан</span>'
        if not u.allowed:
            return '<span class="pill">нет доступа</span>'
        if self.billing.plan(u) is PRO:
            label = "пробный" if self.billing.is_trial(u) else "Pro"
            return f'<span class="pill ok">{label}</span>'
        return '<span class="pill">Free</span>'

    async def _users(self, req: Request) -> Response:
        q = req.query.get("q", "").strip()[:64]
        pg = max(0, parse_int(req.query.get("page", "0")) or 0)
        users = self.db.search_users(q, limit=PAGE_SIZE + 1, offset=pg * PAGE_SIZE)
        more = len(users) > PAGE_SIZE
        resumes = self.db.resume_counts()
        rows = [
            (
                f'<a href="/user?id={u.id}">{u.id}</a>',
                escape(u.username or "—"),
                self._badge(u) + (" ⏸" if u.paused else ""),
                "✅" if u.onboarded else "—",
                str(resumes.get(u.id, 0)),
                str(len(u.filters.queries)),
                fmt_dt(u.last_seen),
                fmt_dt(u.created_at),
            )
            for u in users[:PAGE_SIZE]
        ]
        search = (
            '<form method="get" action="/users" class="inline">'
            f'<input name="q" value="{escape(q)}" placeholder="id или имя">'
            '<button class="ghost">Найти</button></form>'
        )
        pager = []
        if pg:
            pager.append(f'<a href="/users?q={quote(q)}&page={pg - 1}">← назад</a>')
        if more:
            pager.append(f'<a href="/users?q={quote(q)}&page={pg + 1}">дальше →</a>')
        body = search + table(
            ["ID", "Имя", "Тариф", "Настроен", "Резюме", "Направл.", "Был (UTC)", "Пришёл"],
            rows,
            "Никого не нашлось",
        )
        body += f"<p>{' · '.join(pager)}</p>" if pager else ""
        return Response(200, page("Пользователи", body, active="/users"))

    async def _user(self, req: Request) -> Response:
        uid = parse_int(req.query.get("id", ""))
        user = self.db.get_user(uid) if uid is not None else None
        if user is None:
            return Response(404, page("Пользователь не найден", "", active="/users"))
        plan = self.billing.plan(user)
        stats = self.db.stats(user.id)
        resumes = self.db.get_resumes(user.id)
        info = table(
            ["Поле", "Значение"],
            [
                ("ID", str(user.id)),
                ("Имя", escape(user.username or "—")),
                ("Тариф", self._badge(user)),
                ("Pro до", fmt_dt(user.plan_until)),
                ("Автопродление", "да" if user.sub_charge_id else "нет"),
                ("Пробный использован", "да" if user.trial_used else "нет"),
                ("Поиск", "на паузе" if user.paused else "идёт"),
                ("Пришёл", fmt_dt(user.created_at)),
                ("Последняя активность", fmt_dt(user.last_seen)),
                ("Последняя проверка вакансий", fmt_dt(user.last_check)),
                ("ИИ-токены сегодня", _num(self.db.tokens_today(user.id))),
                (
                    "За 7 дней",
                    f"прислано {stats.get('sent', 0)}, отсеяно {stats.get('filtered', 0)}, "
                    f"ИИ отклонил {stats.get('ai_rejected', 0)}, скрыто {stats.get('disliked', 0)}",
                ),
            ],
            kv=True,
        )
        hid = f'<input type="hidden" name="id" value="{user.id}">'

        def act(action: str, label: str, cls: str = "ghost", extra: str = "") -> str:
            return self._form(
                "/user",
                f'{hid}<input type="hidden" name="action" value="{action}">{extra}',
                f'<button class="{cls}">{label}</button>',
            )

        is_admin = user.id in self.settings.admin_ids
        actions = [
            act(
                "grant",
                "Выдать Pro",
                "",
                '<input type="number" name="days" value="30" min="-3650" max="3650" '
                'style="width:90px" aria-label="дней"> дн.',
            ),
            act("deny", "Закрыть доступ") if user.allowed else act("allow", "Открыть доступ", ""),
            act("unpause", "Возобновить поиск") if user.paused else act("pause", "Пауза"),
            act("reset", "Сбросить настройку"),
        ]
        if not is_admin:
            actions.append(
                act("unban", "Разбанить", "") if user.banned else act("ban", "Забанить", "danger")
            )
        message_form = self._form(
            "/user",
            f'{hid}<input type="hidden" name="action" value="message">'
            '<textarea name="text" maxlength="4000" required '
            'placeholder="Текст сообщения (обычный текст)"></textarea>',
            "<p><button>Отправить в Telegram</button></p>",
            cls="",
        )
        delete_form = self._form(
            "/user",
            f'{hid}<input type="hidden" name="action" value="delete">'
            '<label><input type="checkbox" name="confirm" value="yes" required> '
            "Удалить резюме, профиль, фильтры и историю — без восстановления</label> ",
            '<button class="danger">Удалить данные</button>',
        )
        profile = r.profile_card(self.db.get_profile(user.id), user.filters.queries)
        summary = r.search_summary(
            user.filters,
            (self.pipeline and list(self.pipeline.sources)) or [],
            paused=user.paused,
            resumes=len(resumes),
            plan=plan,
            check_every_min=plan.check_every_min,
        )
        payments = table(
            ["Когда", "Сумма", "Тип"],
            [
                (
                    escape(p["created_at"][:16].replace("T", " ")),
                    f"{p['amount']} {escape(p['currency'])}",
                    "автопродление" if p["recurring"] else "оплата",
                )
                for p in self.db.payments_of(user.id)
            ],
            "Платежей нет",
        )
        resume_rows = [(escape(x.title), escape(", ".join(x.skills[:10]) or "—")) for x in resumes]
        status_pill = {
            "sent": '<span class="pill ok">прислал</span>',
            "disliked": '<span class="pill">скрыл 👎</span>',
            "filtered": '<span class="pill">отсеял</span>',
            "ai_rejected": '<span class="pill warn">ИИ отклонил</span>',
            "error": '<span class="pill bad">не доставлено</span>',
        }
        delivery_rows = [
            (
                escape(str(d["at"])[5:16].replace("T", " ")),
                status_pill.get(str(d["status"]), escape(str(d["status"]))),
                (
                    f'<a href="{escape(str(d["url"]))}" rel="noreferrer">'
                    f"{escape(str(d['title']))}</a>"
                    if str(d["url"]).startswith(("https://", "http://"))
                    else escape(str(d["title"]))
                )
                + f' <span class="muted">{escape(str(d["company"]))}</span>',
                "" if d["score"] is None else str(d["score"]),
                f'<span class="muted">{escape(str(d["reason"])[:90])}</span>',
            )
            for d in self.db.recent_deliveries(user.id, 60)
        ]
        wanted = sorted(self.pipeline.wanted_roles(user)) if self.pipeline else []
        deliveries = (
            f'<p class="muted">Ищет роли: {escape(", ".join(wanted)) or "не определены"}</p>'
            + table(
                ["Когда", "Что", "Вакансия", "Балл", "Причина"],
                delivery_rows,
                "Вакансий ещё не было",
            )
        )
        body = (
            f"{info}<h2>Действия</h2><div>{''.join(actions)}</div>"
            f"<h2>Сообщение</h2>{message_form}"
            f'<div class="cols"><div><h2>Профиль</h2><div class="tg">{profile}</div></div>'
            f'<div><h2>Поиск</h2><div class="tg">{summary}</div></div></div>'
            f"<h2>Резюме</h2>{table(['Название', 'Навыки'], resume_rows, 'Резюме нет')}"
            f"<h2>Последние вакансии</h2>{deliveries}"
            f"<h2>Платежи</h2>{payments}"
            f"<h2>Удаление данных</h2>{delete_form}"
        )
        title = f"{user.username or 'Без имени'} · {user.id}"
        flash = req.query.get("msg", "")[:200]
        return Response(
            200, page(title, body, active="/users", flash=flash, bad=req.query.get("bad") == "1")
        )

    async def _user_action(self, req: Request) -> Response:
        uid = parse_int(req.form.get("id", ""))
        user = self.db.get_user(uid) if uid is not None else None
        if user is None:
            return Response(404, page("Пользователь не найден", "", active="/users"))
        action = req.form.get("action", "")
        ok, text = await self._apply(user, action, req.form)
        log.info("admin web: %s on %s -> %s", action, user.id, text)
        return redirect(f"/user?id={user.id}&msg={quote(text)}{'' if ok else '&bad=1'}")

    async def _apply(self, user: User, action: str, form: dict[str, str]) -> tuple[bool, str]:
        is_admin = user.id in self.settings.admin_ids
        if action == "grant":
            days = parse_int(form.get("days", ""))
            if days is None or not days or abs(days) > MAX_GRANT_DAYS:
                return False, f"Дней — целое число от 1 до {MAX_GRANT_DAYS} (или минус)"
            self.db.set_allowed(user.id, True)
            until = self.billing.extend(user, days)
            if days > 0:
                await self._send(user.id, f"🎁 Вам подключён 💎 Pro до {r.date_ru(until)}!")
            return True, f"Pro до {r.date_ru(until)}"
        if action in ("allow", "deny"):
            self.db.set_allowed(user.id, action == "allow")
            if action == "allow":
                await self._send(
                    user.id, "✅ Доступ открыт! Нажмите /start, чтобы настроить поиск."
                )
            return True, "Доступ открыт" if action == "allow" else "Доступ закрыт"
        if action in ("pause", "unpause"):
            self.db.set_paused(user.id, action == "pause")
            return True, "Поиск на паузе" if action == "pause" else "Поиск возобновлён"
        if action == "reset":
            # резюме, профиль и фильтры — с нуля; доступ, подписка и история остаются
            if self.bot is not None:
                await self.bot.reset_setup(user)
            else:
                self.db.reset_setup(user.id)
            return True, "Настройка сброшена — при следующем /start начнётся заново"
        if action in ("ban", "unban"):
            if is_admin:
                return False, "Администратора забанить нельзя"
            self.db.set_banned(user.id, action == "ban")
            return True, "Забанен" if action == "ban" else "Разбанен"
        if action == "message":
            text = form.get("text", "").strip()[:4000]
            if not text:
                return False, "Пустое сообщение"
            if self.bot is None:
                return False, "Бот не подключён"
            sent = await self._send(user.id, text, plain=True)
            return sent, "Отправлено" if sent else "Не доставлено (бот заблокирован?)"
        if action == "delete":
            if form.get("confirm") != "yes":
                return False, "Поставьте галочку подтверждения"
            if self.bot is not None:
                await self.bot.delete_user_data(user)
            else:
                self.db.delete_user_data(user.id)
            return True, "Данные удалены"
        return False, "Неизвестное действие"

    async def _send(self, chat_id: int, text: str, *, plain: bool = False) -> bool:
        if self.bot is None:
            return False
        try:
            await self.bot.api.send_message(chat_id, text, parse_mode=None if plain else "HTML")
            return True
        except Exception as exc:
            log.info("admin web: message to %s failed: %r", chat_id, exc)
            return False

    # --- логи, настройки, перезапуск ---------------------------------------------

    def _log_lines(self, limit: int, level: str, query: str) -> list[dict[str, str]]:
        if self.runtime.logs is None:
            return []
        return [
            {
                "at": ln.at.isoformat(timespec="seconds"),
                "level": ln.level,
                "name": ln.name,
                "text": ln.text,
            }
            for ln in self.runtime.logs.tail(limit, level=level, query=query)
        ]

    async def _logs_page(self, req: Request) -> Response:
        level = req.query.get("level", "")
        query = req.query.get("q", "")[:100]
        limit = min(2000, max(1, parse_int(req.query.get("lines", "300")) or 300))
        options = "".join(
            f'<option value="{v}"{" selected" if v == level else ""}>{label}</option>'
            for v, label in (
                ("", "все"),
                ("WARNING", "предупреждения и ошибки"),
                ("ERROR", "ошибки"),
            )
        )
        form = (
            '<form method="get" action="/logs" class="inline">'
            f'<select name="level">{options}</select>'
            f'<input name="q" value="{escape(query)}" placeholder="поиск">'
            f'<input name="lines" type="number" value="{limit}" min="1" max="2000" '
            'style="width:90px">'
            '<button class="ghost">Показать</button></form>'
        )
        if self.runtime.logs is None:
            return Response(
                200, page("Логи", '<p class="muted">Логи не подключены</p>', active="/logs")
            )
        lines = self._log_lines(limit, level, query)
        css = {"ERROR": "bad", "CRITICAL": "bad", "WARNING": "warn"}
        text = "\n".join(
            f'<span class="muted">{escape(x["at"][5:16].replace("T", " "))}</span> '
            f'<span class="pill {css.get(x["level"], "")}">{escape(x["level"])}</span> '
            f'<span class="muted">{escape(x["name"])}</span> {escape(x["text"])}'
            for x in reversed(lines)
        )
        body = (
            f'{form}<p class="muted">Свежие сверху, время UTC. Хранятся последние строки с момента '
            f"запуска ({fmt_dt(self.runtime.started_at)}).</p>"
            f'<div class="tg" style="font:12px/1.5 ui-monospace,Consolas,monospace">'
            f"{text or 'Пусто'}</div>"
        )
        return Response(200, page("Логи", body, active="/logs"))

    def _config(self) -> list[dict[str, Any]]:
        """Текущие настройки: действующие сейчас и сохранённые в админке (до перезапуска)."""
        saved: dict[str, str] = {}
        path = self.runtime.overrides
        if path and path.is_file():
            saved = parse_dotenv(path.read_text(encoding="utf-8"))
        effective = effective_values(self.settings)
        out = []
        for key in EDITABLE_KEYS:
            active = self.runtime.env.get(key) or effective.get(key, "")
            pending = saved.get(key)
            if key in os.environ:
                source = "окружение"
            elif pending is not None:
                source = "админка"
            elif key in self.runtime.env:
                source = ".env"
            else:
                source = "по умолчанию"
            secret = key in SECRET_KEYS
            out.append(
                {
                    "key": key,
                    "value": ("задан" if active else "") if secret else active,
                    "saved": (("задан" if pending else "") if secret else pending)
                    if pending is not None
                    else None,
                    "source": source,
                    "secret": secret,
                    "locked": key in os.environ,
                }
            )
        return out

    def _apply_config(self, changes: dict[str, str]) -> tuple[bool, str]:
        if self.runtime.overrides is None:
            return False, "Файл настроек не подключён (бот запущен без админки?)"
        clean = {str(k): str(v) for k, v in changes.items() if k in EDITABLE_KEYS}
        if not clean:
            return False, "Нечего сохранять"
        try:
            # проверяем, что с новыми значениями бот вообще запустится
            merged = load_env(None, self.runtime.overrides)
            merged = {**self.runtime.env, **merged, **clean}
            Settings.from_env({k: v for k, v in merged.items() if v != ""})
            save_overrides(self.runtime.overrides, clean)
        except ConfigError as exc:
            return False, f"Не сохранено: {exc}"
        names = ", ".join(sorted(clean))
        log.warning("admin web: settings changed: %s", names)
        return True, f"Сохранено: {names}. Применится после перезапуска."

    async def _settings_page(self, req: Request) -> Response:
        rows = []
        for c in self._config():
            if c["locked"]:
                field_html = '<span class="muted">задано переменной окружения</span>'
            else:
                kind = "password" if c["secret"] else "text"
                hint = "оставьте пустым, чтобы не менять" if c["secret"] else ""
                value = (
                    ""
                    if c["secret"]
                    else escape(c["saved"] if c["saved"] is not None else c["value"])
                )
                field_html = (
                    f'<input type="{kind}" name="{c["key"]}" value="{value}" '
                    f'placeholder="{hint}" style="width:100%" autocomplete="off">'
                )
            pending = (
                ' <span class="pill warn">ждёт перезапуска</span>'
                if c["saved"] is not None and c["saved"] != c["value"] and not c["secret"]
                else ""
            )
            rows.append(
                f'<tr><td><code>{c["key"]}</code><div class="muted" style="font-size:12px">'
                f"{escape(c['source'])}{pending}</div></td>"
                f'<td style="white-space:normal;min-width:220px">{field_html}</td></tr>'
            )
        form = self._form(
            "/settings",
            '<div class="scroll"><table><tr><th>Параметр</th><th>Значение</th></tr>'
            + "".join(rows)
            + "</table></div>",
            "<p><button>Сохранить</button></p>",
            cls="",
        )
        restart = self._form("/restart", "", '<button class="danger">Перезапустить бота</button>')
        body = (
            '<p class="muted">Токены Telegram и админки, адрес, порт, база и список админов '
            "меняются только в .env — ошибка в них отрежет доступ. Остальное сохраняется "
            "здесь и применяется после перезапуска.</p>"
            f'{form}<h2>Перезапуск</h2><p class="muted">Бот работает с '
            f"{fmt_dt(self.runtime.started_at)} UTC. Перезапуск занимает несколько секунд.</p>"
            f"{restart}"
        )
        flash = req.query.get("msg", "")[:300]
        return Response(
            200,
            page(
                "Настройки", body, active="/settings", flash=flash, bad=req.query.get("bad") == "1"
            ),
        )

    async def _save_settings(self, req: Request) -> Response:
        changes = {
            k: v
            for k, v in req.form.items()
            if k in EDITABLE_KEYS and not (k in SECRET_KEYS and not v.strip())
        }
        current = {c["key"]: c for c in self._config()}
        # неизменённые поля не трогаем — чтобы «по умолчанию» не превращалось в правку
        changes = {
            k: v
            for k, v in changes.items()
            if k in SECRET_KEYS
            or v.strip()
            != (current[k]["saved"] if current[k]["saved"] is not None else current[k]["value"])
        }
        if not changes:
            return redirect("/settings?msg=" + quote("Ничего не изменилось"))
        ok, text = self._apply_config(changes)
        return redirect(f"/settings?msg={quote(text)}{'' if ok else '&bad=1'}")

    async def _restart(self, req: Request) -> Response:
        if not self.runtime.request_restart():
            return redirect("/settings?bad=1&msg=" + quote("Перезапуск недоступен"))
        log.warning("admin web: restart requested")
        return Response(
            200,
            page(
                "Перезапуск",
                '<p>Бот перезапускается. <a href="/">Обновите страницу</a> через 10–20 секунд.</p>',
                active="/settings",
            ),
        )

    # --- API ------------------------------------------------------------------

    async def _api(self, req: Request) -> Response:
        auth = req.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(
            token.encode(), self.settings.admin_web.token.encode()
        ):
            return json_response({"error": "unauthorized"}, 401)
        path = req.path.removeprefix("/api").rstrip("/") or "/"
        try:
            return await self._api_route(req, path)
        except Exception as exc:
            log.exception("admin api %s failed", path)
            return json_response({"error": repr(exc)}, 500)

    async def _api_route(self, req: Request, path: str) -> Response:
        m = req.method
        if (m, path) == ("GET", "/status"):
            return json_response(self._status())
        if (m, path) == ("GET", "/logs"):
            limit = min(3000, max(1, parse_int(req.query.get("lines", "200")) or 200))
            return json_response(
                self._log_lines(limit, req.query.get("level", ""), req.query.get("q", ""))
            )
        if (m, path) == ("GET", "/config"):
            return json_response(self._config())
        if (m, path) == ("POST", "/config"):
            ok, text = self._apply_config({str(k): str(v) for k, v in req.json().items()})
            return json_response({"ok": ok, "message": text}, 200 if ok else 400)
        if (m, path) == ("POST", "/restart"):
            ok = self.runtime.request_restart()
            log.warning("admin api: restart requested")
            return json_response({"ok": ok}, 200 if ok else 409)
        if (m, path) == ("POST", "/cycle"):
            if self.pipeline is None:
                return json_response({"error": "pipeline not attached"}, 409)
            stats = await self.pipeline.run_cycle()
            return json_response({"ok": True, "stats": stats.__dict__})
        if (m, path) == ("GET", "/backups"):
            return json_response(self._backups_json())
        if (m, path) == ("POST", "/backup"):
            if self.backups is None:
                return json_response({"error": "backups disabled (BACKUP_KEEP=0)"}, 409)
            send = self.bot.send_backup if self.bot and self.settings.backup.to_telegram else None
            info = await run_and_send(self.backups, send)
            return json_response({"ok": True, "file": info.path.name, "size": info.size})
        if (m, path) == ("GET", "/users"):
            users = self.db.search_users(req.query.get("q", "")[:64], limit=200)
            return json_response([self._user_json(u) for u in users])
        if path == "/user":
            data = req.json() if m == "POST" else {}
            uid = parse_int(str(data.get("id") or req.query.get("id", "")))
            user = self.db.get_user(uid) if uid is not None else None
            if user is None:
                return json_response({"error": "user not found"}, 404)
            if m == "POST":
                form = {k: str(v) for k, v in data.items()}
                ok, text = await self._apply(user, form.get("action", ""), form)
                return json_response({"ok": ok, "message": text}, 200 if ok else 400)
            return json_response(self._user_json(user, full=True))
        return json_response({"error": "not found", "endpoints": API_ENDPOINTS}, 404)

    def _status(self) -> dict[str, Any]:
        pipe = self.pipeline
        return {
            "version": __version__,
            "started_at": self.runtime.started_at,
            "stats": self.db.business_stats(),
            "funnel": self.db.funnel(),
            "ai_tokens_today": self.db.tokens_since(utcnow().date().isoformat()),
            "ai_tokens_month": self.db.tokens_since(utcnow().replace(day=1).date().isoformat()),
            "ai_daily_tokens_total": self.settings.ai.daily_tokens_total,
            "backups": self._backups_json(),
            "last_cycle_at": pipe.last_cycle_at if pipe else None,
            "last_cycle": pipe.last_cycle.__dict__ if pipe and pipe.last_cycle else None,
            "sources": {n: st.__dict__ for n, st in pipe.status.items()} if pipe else {},
            "alerts": [
                {"at": a.at, "count": a.count, "text": a.text}
                for a in (self.alerts.recent if self.alerts else [])
            ],
        }

    def _ai_today(self, today: str) -> str:
        spent = self.db.tokens_since(today)
        total = self.settings.ai.daily_tokens_total
        if not total:
            return _num(spent)
        return f"{_num(spent)} из {_num(total)} ({spent * 100 // total} %)"

    def _backups_json(self) -> dict[str, Any]:
        if self.backups is None:
            return {"enabled": False}
        return {
            "enabled": True,
            "dir": str(self.backups.directory),
            "last_error": self.backups.last_error,
            "copies": [
                {"file": b.path.name, "size": b.size, "at": b.at} for b in self.backups.copies()
            ],
        }

    def _user_json(self, u: User, *, full: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": u.id,
            "username": u.username,
            "plan": self.billing.plan(u).name,
            "plan_until": u.plan_until,
            "allowed": u.allowed,
            "banned": u.banned,
            "paused": u.paused,
            "onboarded": u.onboarded,
            "last_seen": u.last_seen,
            "queries": u.filters.queries,
        }
        if full:
            profile = self.db.get_profile(u.id)
            data |= {
                "filters": json.loads(u.filters.to_json()),
                "profile": json.loads(profile.to_json()),
                "resumes": [
                    {"id": x.id, "title": x.title, "skills": x.skills, "chars": len(x.text)}
                    for x in self.db.get_resumes(u.id)
                ],
                "wanted_roles": sorted(self.pipeline.wanted_roles(u)) if self.pipeline else None,
                "ai_tokens_today": self.db.tokens_today(u.id),
                "ai_token_limit": self.billing.ai_token_limit(u),
                "stats_7d": self.db.stats(u.id),
                "deliveries": self.db.recent_deliveries(u.id, 100),
            }
        return data

    # --- приглашения ----------------------------------------------------------

    async def _invites(self, req: Request) -> Response:
        username = self.bot.username if self.bot else ""
        rows = []
        for inv in self.db.list_invites():
            link = f"https://t.me/{username}?start={inv['code']}" if username else inv["code"]
            used = (
                f'<a href="/user?id={inv["used_by"]}">{inv["used_by"]}</a>'
                if inv["used_by"]
                else '<span class="pill ok">свободно</span>'
            )
            rows.append((f"<code>{escape(link)}</code>", used, escape(inv["created_at"][:16])))
        create = self._form("/invites", "", "<button>Создать ссылку</button>")
        flash = req.query.get("msg", "")[:300]
        body = (
            f'<p class="muted">Доступ сейчас: <b>{escape(self.settings.billing.access_mode)}</b>. '
            "Одноразовая ссылка пускает одного человека.</p>"
            f"{create}{table(['Ссылка', 'Кем использована', 'Создана'], rows, 'Ссылок ещё нет')}"
        )
        return Response(200, page("Приглашения", body, active="/invites", flash=flash))

    async def _create_invite(self, req: Request) -> Response:
        admin = min(self.settings.admin_ids) if self.settings.admin_ids else 0
        code = self.db.create_invite(admin)
        return redirect(f"/invites?msg={quote('Создана ссылка: ' + code)}")

    # --- рассылка -------------------------------------------------------------

    def _audience(self, segment: str) -> list[User]:
        resumes = self.db.resume_counts()
        users = [u for u in self.db.all_users() if u.allowed and not u.banned]
        if segment == "free":
            return [u for u in users if self.billing.plan(u) is not PRO]
        if segment == "pro":
            return [u for u in users if self.billing.plan(u) is PRO]
        if segment == "no_resume":
            return [u for u in users if not resumes.get(u.id)]
        return users

    async def _broadcast_page(self, req: Request) -> Response:
        counts = {k: len(self._audience(k)) for k in SEGMENTS}
        options = "".join(
            f'<option value="{k}">{escape(v)} — {counts[k]}</option>' for k, v in SEGMENTS.items()
        )
        b = self.broadcast
        status = ""
        if b:
            state = "идёт" if b.finished_at is None else f"завершена {fmt_dt(b.finished_at)}"
            status = (
                f'<div class="card"><b>Последняя рассылка</b> ({escape(SEGMENTS[b.segment])}): '
                f"{state}. Отправлено {b.sent} из {b.total}, ошибок {b.failed}."
                f'<div class="tg" style="margin-top:8px">{escape(b.text[:500])}</div></div>'
            )
        running = self._broadcast_task is not None and not self._broadcast_task.done()
        form = self._form(
            "/broadcast",
            '<p><select name="segment">' + options + "</select></p>"
            '<textarea name="text" maxlength="4000" required '
            'placeholder="Текст рассылки (обычный текст, без разметки)"></textarea>'
            '<p><label><input type="checkbox" name="confirm" value="yes" required> '
            "Понимаю, что сообщение уйдёт всем выбранным</label></p>",
            "<button disabled>Идёт рассылка…</button>" if running else "<button>Отправить</button>",
            cls="",
        )
        body = (
            '<p class="muted">Бот шлёт не быстрее 20 сообщений в секунду. Заблокировавшие бота '
            "считаются ошибками.</p>"
            f"{status}<h2>Новая рассылка</h2>{form}"
        )
        return Response(
            200,
            page("Рассылка", body, active="/broadcast", flash=req.query.get("msg", "")[:200]),
        )

    async def _start_broadcast(self, req: Request) -> Response:
        text = req.form.get("text", "").strip()[:4000]
        segment = req.form.get("segment", "all")
        if not text or segment not in SEGMENTS or req.form.get("confirm") != "yes":
            return redirect("/broadcast?msg=" + quote("Нужны текст, аудитория и подтверждение"))
        if self.bot is None:
            return redirect("/broadcast?msg=" + quote("Бот не подключён"))
        if self._broadcast_task is not None and not self._broadcast_task.done():
            return redirect("/broadcast?msg=" + quote("Предыдущая рассылка ещё идёт"))
        audience = self._audience(segment)
        self.broadcast = Broadcast(text=text, segment=segment, total=len(audience))
        self._broadcast_task = asyncio.create_task(
            self._run_broadcast(self.broadcast, [u.id for u in audience]), name="broadcast"
        )
        log.info("admin web: broadcast to %d users (%s)", len(audience), segment)
        return redirect(
            "/broadcast?msg=" + quote(f"Рассылка запущена: {len(audience)} получателей")
        )

    async def _run_broadcast(self, b: Broadcast, ids: list[int], delay: float = 0.05) -> None:
        try:
            for uid in ids:
                if await self._send(uid, b.text, plain=True):
                    b.sent += 1
                else:
                    b.failed += 1
                await asyncio.sleep(delay)
        finally:
            b.finished_at = datetime.now(UTC)
