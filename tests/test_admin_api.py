"""Управление ботом без SSH: логи, настройки, перезапуск и данные через админку и API."""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import unquote

import httpx
import pytest

from jobspybot.admin_web import AdminWeb, Request, Runtime
from jobspybot.billing import Billing
from jobspybot.config import (
    AdminWebSettings,
    ConfigError,
    Settings,
    load_env,
    overrides_path,
    save_overrides,
)
from jobspybot.guard import LogBuffer
from jobspybot.pipeline import Pipeline
from jobspybot.telegram.api import TelegramAPI
from jobspybot.telegram.bot import Bot
from tests.test_bot import ADMIN, GUEST, FakeTelegram, onboarded_user
from tests.test_pipeline import FakeSource, vac

TOKEN = "s3cret-token-for-tests-1234"
AUTH = {"authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def runtime(tmp_path):
    logs = LogBuffer()
    logger = logging.getLogger("jobspybot")
    old_level = logger.level
    logger.addHandler(logs)
    logger.setLevel(logging.INFO)
    rt = Runtime(
        env={"TRIAL_DAYS": "7", "AI_API_KEY": "sk-secret"},
        overrides=tmp_path / "settings.env",
        logs=logs,
        stop=asyncio.Event(),
    )
    yield rt
    logger.removeHandler(logs)
    logger.setLevel(old_level)


@pytest.fixture
def web(db, runtime):
    settings = Settings(admin_ids=frozenset({ADMIN}), admin_web=AdminWebSettings(token=TOKEN))
    api = TelegramAPI("TOKEN", transport=httpx.MockTransport(FakeTelegram()))
    billing = Billing(db, settings)
    bot = Bot(api, db, settings, ["a"], billing)
    pipeline = Pipeline(db, {"a": FakeSource("a", {})}, bot, settings, billing=billing)
    return AdminWeb(settings, db, billing, bot=bot, pipeline=pipeline, runtime=runtime)


def cookie(web: AdminWeb) -> dict[str, str]:
    return {"cookie": f"jsb_admin={web._session}"}


async def api(web: AdminWeb, method: str, path: str, body: dict | None = None, **query):
    resp = await web.handle(
        Request(
            method,
            path,
            query={k: str(v) for k, v in query.items()},
            headers={**AUTH, "content-type": "application/json"},
            body=json.dumps(body or {}).encode(),
        )
    )
    return resp.status, json.loads(resp.body)


async def test_api_requires_bearer_token(web):
    assert (await web.handle(Request("GET", "/api/status"))).status == 401
    bad = {"authorization": "Bearer wrong"}
    assert (await web.handle(Request("GET", "/api/status", headers=bad))).status == 401
    # кука браузера к API не подходит — только токен
    assert (await web.handle(Request("GET", "/api/status", headers=cookie(web)))).status == 401
    status, data = await api(web, "GET", "/api/nope")
    assert status == 404 and "GET /api/status" in data["endpoints"]


async def test_api_status_and_logs(web):
    logger = logging.getLogger("jobspybot.test_api")
    logger.info("cycle: fetched=10")
    logger.error("habr failed")
    status, data = await api(web, "GET", "/api/status")
    assert status == 200 and "funnel" in data and "a" in data["sources"]
    _, lines = await api(web, "GET", "/api/logs", lines=50)
    assert [x["text"] for x in lines][-2:] == ["cycle: fetched=10", "habr failed"]
    _, errors = await api(web, "GET", "/api/logs", level="ERROR")
    assert [x["text"] for x in errors] == ["habr failed"]
    _, found = await api(web, "GET", "/api/logs", q="FETCHED")
    assert len(found) == 1


async def test_api_config_masks_secrets_and_validates(web, runtime):
    _, config = await api(web, "GET", "/api/config")
    by_key = {c["key"]: c for c in config}
    assert by_key["AI_API_KEY"]["value"] == "задан"  # сам ключ наружу не отдаётся
    assert "sk-secret" not in json.dumps(config)
    assert by_key["TRIAL_DAYS"]["value"] == "7"
    status, data = await api(web, "POST", "/api/config", {"POLL_INTERVAL_MIN": "1"})
    assert status == 400 and "меньше 5" in data["message"]
    status, _ = await api(web, "POST", "/api/config", {"TELEGRAM_BOT_TOKEN": "x"})
    assert status == 400  # токен бота из админки не меняется
    status, data = await api(web, "POST", "/api/config", {"TRIAL_DAYS": "14", "AI_MODEL": "m2"})
    assert status == 200 and "перезапуска" in data["message"]
    assert load_env(None, runtime.overrides)["TRIAL_DAYS"] == "14"
    _, config = await api(web, "GET", "/api/config")
    by_key = {c["key"]: c for c in config}
    assert by_key["TRIAL_DAYS"]["saved"] == "14" and by_key["TRIAL_DAYS"]["source"] == "админка"


async def test_api_restart(web, runtime):
    status, data = await api(web, "POST", "/api/restart")
    assert status == 200 and data["ok"]
    assert runtime.restart_requested and runtime.stop.is_set()


async def test_api_user_shows_why_vacancies_were_filtered(web, db):
    onboarded_user(db, GUEST, queries=["вайбкодер"])
    v = vac("a", "1", "Бизнес-аналитик")
    db.save_vacancies([v])
    db.record(GUEST, v, "filtered", "другая роль: аналитик")
    status, data = await api(web, "GET", "/api/user", id=GUEST)
    assert status == 200 and "ai" in data["wanted_roles"]
    first = data["deliveries"][0]
    assert (first["title"], first["status"], first["reason"]) == (
        "Бизнес-аналитик",
        "filtered",
        "другая роль: аналитик",
    )
    status, _ = await api(web, "POST", "/api/user", {"id": GUEST, "action": "ban"})
    assert status == 200 and db.get_user(GUEST).banned
    status, _ = await api(web, "GET", "/api/user", id=999)
    assert status == 404
    _, users = await api(web, "GET", "/api/users", q="user")
    assert [u["id"] for u in users] == [GUEST]


async def test_settings_page_and_restart_button(web, runtime):
    page = await web.handle(Request("GET", "/settings", headers=cookie(web)))
    assert "TRIAL_DAYS" in page.body and "sk-secret" not in page.body
    assert "TELEGRAM_BOT_TOKEN" not in page.body
    form = {"csrf": web._csrf, "TRIAL_DAYS": "3", "AI_API_KEY": "", "ACCESS_MODE": "invite"}
    resp = await web.handle(Request("POST", "/settings", headers=cookie(web), form=form))
    assert "Сохранено: TRIAL_DAYS" in unquote(resp.headers["Location"])
    saved = load_env(None, runtime.overrides)
    assert saved["TRIAL_DAYS"] == "3" and "AI_API_KEY" not in saved  # пустой секрет не трогаем
    logs = await web.handle(
        Request("GET", "/logs", headers=cookie(web), query={"level": "WARNING"})
    )
    assert "settings changed: TRIAL_DAYS" in logs.body
    form = {"csrf": web._csrf}
    resp = await web.handle(Request("POST", "/restart", headers=cookie(web), form=form))
    assert resp.status == 200 and runtime.restart_requested


async def test_user_page_lists_recent_vacancies(web, db):
    onboarded_user(db, GUEST, queries=["вайбкодер"])
    v = vac("a", "1", "Бизнес-аналитик <b>")
    db.save_vacancies([v])
    db.record(GUEST, v, "filtered", "другая роль: аналитик")
    page = await web.handle(Request("GET", "/user", query={"id": str(GUEST)}, headers=cookie(web)))
    assert "Последние вакансии" in page.body and "Бизнес-аналитик &lt;b&gt;" in page.body
    assert "другая роль: аналитик" in page.body and "Ищет роли: ai" in page.body


def test_overrides_file(tmp_path):
    path = overrides_path({"DB_PATH": str(tmp_path / "db" / "x.sqlite3")})
    assert path == tmp_path / "db" / "settings.env"
    save_overrides(path, {"TRIAL_DAYS": "5", "AI_MODEL": "m"})
    save_overrides(path, {"AI_MODEL": ""})  # пустое — убрать правку
    assert load_env(None, path) == {**load_env(None, None), "TRIAL_DAYS": "5"}
    with pytest.raises(ConfigError):
        save_overrides(path, {"ADMIN_IDS": "1"})
    with pytest.raises(ConfigError):
        save_overrides(path, {"SUPPORT_CONTACT": "a\nTELEGRAM_BOT_TOKEN=evil"})


async def test_api_backup(web, tmp_path):
    from jobspybot.backup import Backups

    status, _ = await api(web, "POST", "/api/backup")
    assert status == 409  # бэкапы не подключены
    web.backups = Backups(web.db, tmp_path / "backups")
    status, data = await api(web, "POST", "/api/backup")
    assert status == 200 and data["file"].endswith(".sqlite3.gz")
    _, listing = await api(web, "GET", "/api/backups")
    assert [c["file"] for c in listing["copies"]] == [data["file"]]
    _, st = await api(web, "GET", "/api/status")
    assert st["backups"]["enabled"]
    tg = web.bot.api._client._transport.handler
    (upload,) = [p for m, p in tg.calls if m == "sendDocument"]
    assert data["file"] in upload["multipart"] and str(ADMIN) in upload["multipart"]


async def test_admin_api_reset(web, db):
    onboarded_user(db, GUEST, queries=["qa"])
    db.add_resume(GUEST, "CV", "Аналитик: BPMN, SQL, EPC. " * 5)
    status, _ = await api(web, "POST", "/api/user", {"id": GUEST, "action": "reset"})
    assert status == 200
    user = db.get_user(GUEST)
    assert not user.onboarded and user.allowed and db.get_resumes(GUEST) == []


async def test_live_logs_feed_returns_only_new_lines(web, runtime):
    logging.getLogger("jobspybot.ai.service").info("ИИ оценка для 1: 900 токенов")
    page_resp = await web.handle(Request("GET", "/logs", headers=cookie(web)))
    # скрипт живого лога разрешён только по одноразовому nonce
    assert "script-src 'nonce-" in page_resp.csp and "<script nonce=" in page_resp.body
    assert "[ИИ и токены]" in page_resp.body
    seq = runtime.logs.seq
    logging.getLogger("jobspybot.sources").info("hh_by «python»: 25 вакансий за 1.0 с")
    logging.getLogger("jobspybot.telegram.bot").info("← 1 @misha: /start")
    feed = await web.handle(
        Request("GET", "/logs/feed", headers=cookie(web), query={"since": str(seq)})
    )
    data = json.loads(feed.body)
    assert [x["text"] for x in data["lines"]] == [
        "hh_by «python»: 25 вакансий за 1.0 с",
        "← 1 @misha: /start",
    ]
    assert data["seq"] == seq + 2 and data["running"] is False and "ai_today" in data
    only_sources = await web.handle(
        Request("GET", "/logs/feed", headers=cookie(web), query={"topic": "sources"})
    )
    assert [x["name"] for x in json.loads(only_sources.body)["lines"]] == ["jobspybot.sources"]
    _, api_lines = await api(web, "GET", "/api/logs", topic="telegram", since=seq)
    assert [x["text"] for x in api_lines] == ["← 1 @misha: /start"]


async def test_live_logs_feed_needs_login(web):
    resp = await web.handle(Request("GET", "/logs/feed"))
    assert resp.status == 303 and resp.headers["Location"] == "/login"
