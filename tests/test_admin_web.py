from __future__ import annotations

import asyncio
from urllib.parse import unquote

import httpx
import pytest

from jobspybot.admin_web import COOKIE, AdminWeb, Request
from jobspybot.billing import Billing
from jobspybot.config import AdminWebSettings, Settings
from jobspybot.guard import AdminAlerts
from jobspybot.telegram.api import TelegramAPI
from jobspybot.telegram.bot import Bot
from tests.test_bot import ADMIN, GUEST, FakeTelegram, onboarded_user

TOKEN = "s3cret-token-for-tests-1234"


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def web(db, tg):
    settings = Settings(
        admin_ids=frozenset({ADMIN}), admin_web=AdminWebSettings(port=0, token=TOKEN)
    )
    api = TelegramAPI("TOKEN", transport=httpx.MockTransport(tg))
    billing = Billing(db, settings)
    bot = Bot(api, db, settings, ["habr"], billing)
    alerts = AdminAlerts()
    alerts.alert("log:x", "❗️ ERROR <script>alert(1)</script>")
    return AdminWeb(settings, db, billing, bot=bot, alerts=alerts)


def login(web: AdminWeb) -> str:
    return f"{COOKIE}={web._session}"


async def get(web: AdminWeb, path: str, **query: str):
    return await web.handle(Request("GET", path, query=query, headers={"cookie": login(web)}))


async def post(web: AdminWeb, path: str, csrf: bool = True, **form: str):
    if csrf:
        form["csrf"] = web._csrf
    return await web.handle(Request("POST", path, headers={"cookie": login(web)}, form=form))


async def test_requires_login(web):
    resp = await web.handle(Request("GET", "/"))
    assert resp.status == 303 and resp.headers["Location"] == "/login"
    resp = await web.handle(Request("GET", "/users", headers={"cookie": f"{COOKIE}=forged"}))
    assert resp.status == 303
    assert (await web.handle(Request("GET", "/health"))).body == "ok"


async def test_login_and_bruteforce_protection(web):
    resp = await web.handle(Request("POST", "/login", form={"token": TOKEN}))
    assert resp.status == 303 and "HttpOnly" in resp.headers["Set-Cookie"]
    assert "SameSite=Strict" in resp.headers["Set-Cookie"]
    for _ in range(5):
        resp = await web.handle(Request("POST", "/login", form={"token": "wrong"}))
        assert resp.status == 401
    resp = await web.handle(Request("POST", "/login", form={"token": TOKEN}))
    assert resp.status == 429  # даже верный токен после серии ошибок


async def test_dashboard_escapes_and_shows_stats(web, db):
    onboarded_user(db, GUEST)
    resp = await get(web, "/")
    assert resp.status == 200 and "Воронка" in resp.body and "Настроили поиск" in resp.body
    assert "<script>alert(1)</script>" not in resp.body
    assert "&lt;script&gt;" in resp.body
    assert "Content-Security-Policy" in resp.encode().decode("utf-8")


async def test_users_list_and_search(web, db):
    db.ensure_user(GUEST, "<b>evil_user</b>", allowed=True)
    db.ensure_user(300, "other", allowed=True)
    resp = await get(web, "/users", q="evil_")
    assert "&lt;b&gt;evil_user" in resp.body and "other" not in resp.body
    resp = await get(web, "/user", id=str(GUEST))
    assert resp.status == 200 and "Забанить" in resp.body
    assert (await get(web, "/user", id="abc")).status == 404


async def test_post_without_csrf_is_rejected(web, db):
    onboarded_user(db, GUEST)
    resp = await post(web, "/user", csrf=False, id=str(GUEST), action="ban")
    assert resp.status == 403 and not db.get_user(GUEST).banned


async def test_user_actions(web, db, tg):
    onboarded_user(db, GUEST)
    db.add_resume(GUEST, "cv", "x" * 60)

    async def act(**form):
        resp = await post(web, "/user", id=str(GUEST), **form)
        assert resp.status == 303
        return unquote(resp.headers["Location"])

    assert "Pro до" in await act(action="grant", days="30")
    assert any("Pro до" in t for t in tg.texts(GUEST))
    assert "от 1 до" in await act(action="grant", days="99999")
    await act(action="ban")
    assert db.get_user(GUEST).banned
    await act(action="unban")
    assert not db.get_user(GUEST).banned
    await act(action="pause")
    assert db.get_user(GUEST).paused
    assert "Отправлено" in await act(action="message", text="Привет <b>!</b>")
    assert (
        tg.calls[-1][1]["text"] == "Привет <b>!</b>" and tg.calls[-1][1].get("parse_mode") is None
    )
    assert "галочку" in await act(action="delete")
    assert db.get_resumes(GUEST)
    await act(action="delete", confirm="yes")
    assert db.get_resumes(GUEST) == [] and not db.get_user(GUEST).onboarded
    onboarded_user(db, ADMIN)
    resp = await post(web, "/user", id=str(ADMIN), action="ban")
    assert "нельзя" in unquote(resp.headers["Location"]) and not db.get_user(ADMIN).banned


async def test_invites(web, db):
    resp = await post(web, "/invites")
    assert resp.status == 303
    (invite,) = db.list_invites()
    assert invite["code"] in (await get(web, "/invites")).body


async def test_broadcast(web, db, tg):
    for uid in (GUEST, 300, 301):
        onboarded_user(db, uid)
    db.set_banned(301, True)
    page = await get(web, "/broadcast")
    assert "Все с доступом — 2" in page.body
    resp = await post(web, "/broadcast", text="Новая функция!", segment="all")
    assert "подтверждение" in unquote(resp.headers["Location"])
    resp = await post(web, "/broadcast", text="Новая функция!", segment="all", confirm="yes")
    assert "2 получателей" in unquote(resp.headers["Location"])
    await asyncio.wait_for(web._broadcast_task, timeout=2)
    assert web.broadcast.sent == 2 and web.broadcast.failed == 0
    assert tg.texts(GUEST) == ["Новая функция!"] and tg.texts(301) == []
    assert "Отправлено 2 из 2" in (await get(web, "/broadcast")).body


async def test_real_http_roundtrip(web):
    stop, ready = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(web.serve(stop, ready))
    await asyncio.wait_for(ready.wait(), timeout=2)
    try:
        base = f"http://127.0.0.1:{web.port}"
        async with httpx.AsyncClient(base_url=base) as client:
            resp = await client.get("/")
            assert resp.status_code == 303
            resp = await client.post("/login", data={"token": TOKEN})
            assert resp.status_code == 303
            resp = await client.get("/users")
            assert resp.status_code == 200 and "Пользователи" in resp.text
            resp = await client.post("/login", content=b"x" * 100_000)
            assert resp.status_code == 413
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
