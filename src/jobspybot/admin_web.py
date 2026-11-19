"""Веб-админка: обзор, пользователи, приглашения, рассылка — в браузере, а не в Telegram.

Без сторонних библиотек: маленький HTTP-сервер на `asyncio.start_server`, страницы —
строки HTML. По умолчанию слушает только 127.0.0.1: на VPS открывается через SSH-туннель
`ssh -L 8080:127.0.0.1:8080 user@host`, наружу порт не публикуется.

Вход — по `ADMIN_WEB_TOKEN`. Сессия — HttpOnly-кука с HMAC от токена (смена токена
разлогинивает всех), формы защищены CSRF-полем и `SameSite=Strict`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, quote, urlsplit

from jobspybot import __version__
from jobspybot.billing import PRO, Billing
from jobspybot.config import Settings
from jobspybot.db import Database, User, utcnow
from jobspybot.limits import MAX_GRANT_DAYS, parse_int
from jobspybot.telegram import render as r

if TYPE_CHECKING:
    from jobspybot.guard import AdminAlerts
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
    ) -> None:
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
            form=dict(parse_qsl(body.decode("utf-8", "replace"))) if body else {},
        )
        return await self.handle(request)

    # --- маршруты -------------------------------------------------------------

    async def handle(self, req: Request) -> Response:
        if req.path == "/health":
            return Response(200, "ok", content_type="text/plain; charset=utf-8")
        if req.path == "/login":
            return self._login(req)
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
                card("ИИ-токены сегодня", _num(self.db.tokens_since(today))),
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
        body = (
            f"{info}<h2>Действия</h2><div>{''.join(actions)}</div>"
            f"<h2>Сообщение</h2>{message_form}"
            f'<div class="cols"><div><h2>Профиль</h2><div class="tg">{profile}</div></div>'
            f'<div><h2>Поиск</h2><div class="tg">{summary}</div></div></div>'
            f"<h2>Резюме</h2>{table(['Название', 'Навыки'], resume_rows, 'Резюме нет')}"
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
