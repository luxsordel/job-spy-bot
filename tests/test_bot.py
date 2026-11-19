from __future__ import annotations

import json

import httpx
import pytest

from jobspybot.config import Settings
from jobspybot.models import Vacancy
from jobspybot.telegram.api import TelegramAPI
from jobspybot.telegram.bot import Bot, parse_list

ADMIN = 100
GUEST = 200


class FakeTelegram:
    """Записывает вызовы Bot API и отвечает успехом."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content or b"{}")
        self.calls.append((method, payload))
        result = {"username": "spybot"} if method == "getMe" else {"message_id": 1}
        return httpx.Response(200, json={"ok": True, "result": result})

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [
            p["text"]
            for m, p in self.calls
            if m == "sendMessage" and (chat_id is None or p["chat_id"] == chat_id)
        ]


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def bot(db, tg):
    api = TelegramAPI("TOKEN", transport=httpx.MockTransport(tg))
    return Bot(api, db, Settings(admin_ids=frozenset({ADMIN})), ["habr", "hh_by", "justjoin"])


def message(user_id: int, text: str) -> dict:
    return {
        "update_id": 1,
        "message": {
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "username": f"user{user_id}"},
            "text": text,
        },
    }


def callback(user_id: int, data: str) -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb",
            "from": {"id": user_id},
            "data": data,
            "message": {
                "chat": {"id": user_id},
                "message_id": 5,
                "reply_markup": {"inline_keyboard": [[{"text": "🔗", "url": "https://x"}]]},
            },
        },
    }


def test_parse_list():
    assert parse_list("python, django; sql\nsql") == ["python", "django", "sql"]


async def test_guest_is_blocked_and_admin_asked(bot, tg, db):
    await bot.handle_update(message(GUEST, "/start"))
    assert "приглашениям" in tg.texts(GUEST)[0]
    assert any(str(GUEST) in t for t in tg.texts(ADMIN))
    await bot.handle_update(message(GUEST, "/query python"))
    assert db.get_user(GUEST).filters.queries == []


async def test_invite_flow(bot, tg, db):
    await bot.handle_update(message(ADMIN, "/invite"))
    link = tg.texts(ADMIN)[-1].split()[-1]
    assert "start=" in link
    code = link.split("start=")[1]
    await bot.handle_update(message(GUEST, f"/start {code}"))
    assert db.get_user(GUEST).allowed
    # ссылка одноразовая
    await bot.handle_update(message(300, f"/start {code}"))
    assert not db.get_user(300).allowed


async def test_admin_approves_by_button(bot, tg, db):
    await bot.handle_update(message(GUEST, "/start"))
    await bot.handle_update(callback(ADMIN, f"allow:{GUEST}"))
    assert db.get_user(GUEST).allowed
    # не-админ не может выдавать доступ
    await bot.handle_update(callback(GUEST, "allow:300"))
    assert db.get_user(300) is None


async def test_filter_commands(bot, tg, db):
    await bot.handle_update(message(ADMIN, "/query python developer; django"))
    await bot.handle_update(message(ADMIN, "/exclude senior, 1С"))
    await bot.handle_update(message(ADMIN, "/level junior middle"))
    await bot.handle_update(message(ADMIN, "/country by pl remote"))
    await bot.handle_update(message(ADMIN, "/salary 1200 strict"))
    await bot.handle_update(message(ADMIN, "/remote"))
    await bot.handle_update(message(ADMIN, "/threshold 70"))
    f = db.get_user(ADMIN).filters
    assert f.queries == ["python developer", "django"]
    assert f.exclude == ["senior", "1С"]
    assert f.levels == ["junior", "middle"]
    assert f.countries == ["BY", "PL", "REMOTE"]
    assert (f.min_salary_usd, f.require_salary, f.remote_only) == (1200, True, True)
    assert f.ai_threshold == 70

    await bot.handle_update(message(ADMIN, "/exclude -"))
    await bot.handle_update(message(ADMIN, "/level guru"))
    f = db.get_user(ADMIN).filters
    assert f.exclude == [] and f.levels == ["junior", "middle"]
    assert "Неизвестный уровень" in tg.texts(ADMIN)[-1]

    await bot.handle_update(message(ADMIN, "/settings"))
    assert "python developer" in tg.texts(ADMIN)[-1]


async def test_sources_toggle(bot, tg, db):
    await bot.handle_update(callback(ADMIN, "src:habr"))
    assert db.get_user(ADMIN).filters.sources == ["hh_by", "justjoin"]
    await bot.handle_update(callback(ADMIN, "src:habr"))
    assert db.get_user(ADMIN).filters.sources == []  # снова все
    for name in ("habr", "hh_by", "justjoin"):
        await bot.handle_update(callback(ADMIN, f"src:{name}"))
    assert db.get_user(ADMIN).filters.sources == ["justjoin"]  # последнюю не выключить


async def test_resume_flow_with_hook(bot, tg, db):
    received = []

    async def hook(user, text):
        received.append(text)
        return "профиль готов"

    bot.on_resume = hook
    await bot.handle_update(message(ADMIN, "/resume"))
    resume = "Python-разработчик, 2 года опыта, Django, PostgreSQL, Docker. " * 2
    await bot.handle_update(message(ADMIN, resume))
    assert db.get_user(ADMIN).resume == resume.strip()
    assert received and tg.texts(ADMIN)[-1] == "профиль готов"


async def test_send_vacancy_and_dislike(bot, tg, db):
    user = db.ensure_user(ADMIN, allowed=True)
    v = Vacancy(source="habr", external_id="1", title="Python <Dev>", url="https://x/1")
    db.save_vacancies([v])
    db.record(ADMIN, v, "sent")
    did = db.delivery_id(ADMIN, v.uid)
    await bot.send_vacancy(user, v, did, extra="🤖 80/100")
    method, payload = tg.calls[-1]
    assert method == "sendMessage" and "Python &lt;Dev&gt;" in payload["text"]
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert buttons[-1]["callback_data"] == f"nope:{did}"

    await bot.handle_update(callback(ADMIN, f"nope:{did}"))
    assert db.stats(ADMIN) == {"disliked": 1}
