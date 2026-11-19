from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobspybot.config import BillingSettings, Settings
from jobspybot.filters import UserFilters
from jobspybot.models import Vacancy
from jobspybot.telegram import render as r
from jobspybot.telegram.api import TelegramAPI
from jobspybot.telegram.bot import Bot, parse_list

ADMIN = 100
GUEST = 200


class FakeTelegram:
    """Записывает вызовы Bot API и отвечает успехом."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.files: dict[str, bytes] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "/file/" in request.url.path:
            return httpx.Response(200, content=self.files[request.url.path.rsplit("/", 1)[-1]])
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content or b"{}")
        self.calls.append((method, payload))
        if method == "getFile":
            fid = payload["file_id"]
            result = {"file_path": f"docs/{fid}", "file_size": len(self.files[fid])}
        elif method == "createInvoiceLink":
            result = "https://t.me/$invoice"
        elif method == "getMe":
            result = {"username": "spybot"}
        else:
            result = {"message_id": 1}
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


def document(user_id: int, file_id: str, file_name: str) -> dict:
    update = message(user_id, "")
    update["message"]["document"] = {"file_id": file_id, "file_name": file_name, "file_size": 10}
    return update


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


def make_bot(db, tg, **billing) -> Bot:
    api = TelegramAPI("TOKEN", transport=httpx.MockTransport(tg))
    settings = Settings(admin_ids=frozenset({ADMIN}), billing=BillingSettings(**billing))
    return Bot(api, db, settings, ["habr", "hh_by", "justjoin"])


def onboarded_user(db, uid: int, **filters) -> None:
    db.ensure_user(uid, f"user{uid}", allowed=True)
    db.set_onboarded(uid)
    if filters:
        db.save_filters(uid, UserFilters(**filters))


def buttons(tg: FakeTelegram) -> list[dict]:
    """Все inline-кнопки последнего сообщения/редактирования."""
    for method, payload in reversed(tg.calls):
        markup = payload.get("reply_markup")
        if method in ("sendMessage", "editMessageText") and markup and "inline_keyboard" in markup:
            return [b for row in markup["inline_keyboard"] for b in row]
    return []


# --- доступ и онбординг ---------------------------------------------------------


async def test_guest_is_blocked_in_invite_mode(bot, tg, db):
    await bot.handle_update(message(GUEST, "/start"))
    assert "приглашениям" in tg.texts(GUEST)[0]
    assert any(str(GUEST) in t for t in tg.texts(ADMIN))
    await bot.handle_update(message(GUEST, "/query python"))
    assert db.get_user(GUEST).filters.queries == []


async def test_open_mode_admits_with_trial_and_onboarding(db, tg):
    bot = make_bot(db, tg, access_mode="open", trial_days=7)
    await bot.handle_update(message(GUEST, "привет"))
    user = db.get_user(GUEST)
    assert user.allowed and user.trial_used
    assert bot.billing.plan(user).name == "pro" and bot.billing.is_trial(user)
    texts = tg.texts(GUEST)
    assert "Pro на 7 дней" in texts[0]
    assert "Шаг 1 из 4" in texts[1]
    assert any("Новый пользователь" in t for t in tg.texts(ADMIN))


async def test_invite_flow_starts_onboarding(bot, tg, db):
    await bot.handle_update(message(ADMIN, "/invite"))
    code = tg.texts(ADMIN)[-1].split("start=")[1]
    await bot.handle_update(message(GUEST, f"/start {code}"))
    assert db.get_user(GUEST).allowed
    assert "Шаг 1 из 4" in tg.texts(GUEST)[-1]
    await bot.handle_update(message(300, f"/start {code}"))  # ссылка одноразовая
    assert not db.get_user(300).allowed


async def test_admin_approves_by_button(bot, tg, db):
    await bot.handle_update(message(GUEST, "/start"))
    await bot.handle_update(callback(ADMIN, f"allow:{GUEST}"))
    assert db.get_user(GUEST).allowed
    await bot.handle_update(callback(GUEST, "allow:300"))  # не-админ не может
    assert db.get_user(300) is None


async def test_full_onboarding(db, tg):
    bot = make_bot(db, tg, access_mode="open", trial_days=7)
    await bot.handle_update(message(GUEST, "/start"))
    await bot.handle_update(callback(GUEST, "ob:go:2"))  # без направлений дальше нельзя
    assert tg.calls[-1][1]["text"] == "Выберите хотя бы одно направление"
    await bot.handle_update(callback(GUEST, "ob:dir:py"))
    await bot.handle_update(callback(GUEST, "ob:dir:qa"))
    await bot.handle_update(callback(GUEST, "ob:custom"))
    await bot.handle_update(message(GUEST, "аналитик данных, python developer"))
    await bot.handle_update(callback(GUEST, "ob:go:2"))
    await bot.handle_update(callback(GUEST, "ob:lvl:junior"))
    await bot.handle_update(callback(GUEST, "ob:go:3"))
    await bot.handle_update(callback(GUEST, "ob:cty:BY"))
    await bot.handle_update(callback(GUEST, "ob:cty:REMOTE"))
    await bot.handle_update(callback(GUEST, "ob:go:4"))
    await bot.handle_update(callback(GUEST, "ob:done"))
    user = db.get_user(GUEST)
    # ручной ввод только добавляет: уже выбранный «python developer» не снимается
    assert user.filters.queries == ["python developer", "qa engineer", "аналитик данных"]
    assert user.filters.levels == ["junior"] and user.filters.countries == ["BY", "REMOTE"]
    assert user.onboarded
    menu = [
        p for m, p in tg.calls if m == "sendMessage" and "keyboard" in (p.get("reply_markup") or {})
    ]
    assert menu and "Готово" in tg.texts(GUEST)[-1]


async def test_resume_during_onboarding_finishes_it(db, tg):
    bot = make_bot(db, tg, access_mode="open")
    await bot.handle_update(message(GUEST, "/start"))
    await bot.handle_update(callback(GUEST, "ob:dir:py"))
    await bot.handle_update(callback(GUEST, "ob:go:4"))
    tg.files["f1"] = ("Python developer: FastAPI, PostgreSQL, Docker, Git. " * 3).encode()
    await bot.handle_update(document(GUEST, "f1", "cv.txt"))
    assert db.get_user(GUEST).onboarded and len(db.get_resumes(GUEST)) == 1


# --- меню и фильтры -------------------------------------------------------------


async def test_menu_buttons(bot, tg, db):
    onboarded_user(db, GUEST, queries=["python developer"])
    await bot.handle_update(message(GUEST, r.BTN_SEARCH))
    assert "python developer" in tg.texts(GUEST)[-1]
    await bot.handle_update(message(GUEST, r.BTN_FILTERS))
    assert "Фильтры" in tg.texts(GUEST)[-1]
    await bot.handle_update(message(GUEST, r.BTN_STATS))
    assert "Статистика" in tg.texts(GUEST)[-1]
    await bot.handle_update(message(GUEST, r.BTN_HELP))
    assert "Как это работает" in tg.texts(GUEST)[-1]
    await bot.handle_update(message(GUEST, "непонятное"))
    assert "Не понял" in tg.texts(GUEST)[-1]


async def test_filter_buttons(bot, tg, db):
    onboarded_user(db, ADMIN)
    await bot.handle_update(callback(ADMIN, "f:lvl:junior"))
    await bot.handle_update(callback(ADMIN, "f:lvl:middle"))
    await bot.handle_update(callback(ADMIN, "f:lvl:junior"))
    await bot.handle_update(callback(ADMIN, "f:cty:PL"))
    await bot.handle_update(callback(ADMIN, "f:remote"))
    await bot.handle_update(callback(ADMIN, "f:thr:70"))
    await bot.handle_update(callback(ADMIN, "f:salary"))
    await bot.handle_update(message(ADMIN, "от 1 500 $"))
    await bot.handle_update(callback(ADMIN, "f:exclude"))
    await bot.handle_update(message(ADMIN, "senior, 1С"))
    await bot.handle_update(callback(ADMIN, "f:dir:go"))
    f = db.get_user(ADMIN).filters
    assert f.levels == ["middle"] and f.countries == ["PL"] and f.remote_only
    assert f.ai_threshold == 70 and f.min_salary_usd == 1500
    assert f.exclude == ["senior", "1С"] and f.queries == ["golang developer"]
    assert any(b["text"].startswith("✅") and "Go" in b["text"] for b in buttons(tg))


async def test_sources_toggle(bot, tg, db):
    onboarded_user(db, ADMIN)
    await bot.handle_update(callback(ADMIN, "f:src:habr"))
    assert db.get_user(ADMIN).filters.sources == ["hh_by", "justjoin"]
    await bot.handle_update(callback(ADMIN, "f:src:habr"))
    assert db.get_user(ADMIN).filters.sources == []  # снова все
    for name in ("habr", "hh_by", "justjoin"):
        await bot.handle_update(callback(ADMIN, f"f:src:{name}"))
    assert db.get_user(ADMIN).filters.sources == ["justjoin"]  # последнюю не выключить


async def test_legacy_commands_still_work(bot, tg, db):
    onboarded_user(db, ADMIN)
    await bot.handle_update(message(ADMIN, "/query python developer; django"))
    await bot.handle_update(message(ADMIN, "/level junior middle"))
    await bot.handle_update(message(ADMIN, "/country by pl remote"))
    await bot.handle_update(message(ADMIN, "/salary 1200 strict"))
    f = db.get_user(ADMIN).filters
    assert f.queries == ["python developer", "django"] and f.levels == ["junior", "middle"]
    assert f.countries == ["BY", "PL", "REMOTE"] and f.require_salary


# --- тарифы ---------------------------------------------------------------------


async def test_free_plan_limits(db, tg):
    bot = make_bot(db, tg, trial_days=0)
    onboarded_user(db, GUEST)
    await bot.handle_update(callback(GUEST, "f:dir:py"))
    await bot.handle_update(callback(GUEST, "f:dir:java"))
    assert "Free" in tg.calls[-1][1]["text"]  # всплывающая подсказка про Pro
    assert db.get_user(GUEST).filters.queries == ["python developer"]
    await bot.handle_update(message(GUEST, "/query a; b; c"))
    assert db.get_user(GUEST).filters.queries == ["a"]
    db.add_resume(GUEST, "r", "x" * 60)
    await bot.handle_update(message(GUEST, "/resume " + "текст резюме " * 10))
    assert len(db.get_resumes(GUEST)) == 1 and "Pro" in tg.texts(GUEST)[-1]


async def test_plan_screen_and_payment(db, tg):
    bot = make_bot(db, tg, trial_days=0, pro_price_stars=250)
    onboarded_user(db, GUEST)
    await bot.handle_update(message(GUEST, r.BTN_PLAN))
    invoice = next(p for m, p in tg.calls if m == "createInvoiceLink")
    assert invoice["currency"] == "XTR" and invoice["prices"][0]["amount"] == 250
    assert invoice["subscription_period"] == 30 * 24 * 3600
    assert any(b.get("url") == "https://t.me/$invoice" for b in buttons(tg))
    assert "Free" in tg.texts(GUEST)[-1]

    await bot.handle_update(pre_checkout(GUEST, 250))
    assert tg.calls[-1] == ("answerPreCheckoutQuery", {"pre_checkout_query_id": "pc", "ok": True})
    await bot.handle_update(pre_checkout(GUEST, 1))
    assert tg.calls[-1][1]["ok"] is False

    await bot.handle_update(paid(GUEST, "ch1", first=True))
    await bot.handle_update(paid(GUEST, "ch1", first=True))  # повтор апдейта
    user = db.get_user(GUEST)
    assert bot.billing.plan(user).name == "pro" and user.sub_charge_id == "ch1"
    assert db.business_stats()["stars_total"] == 250
    assert "Pro подключён" in tg.texts(GUEST)[-1]
    assert any("Оплата" in t for t in tg.texts(ADMIN))

    await bot.handle_update(callback(GUEST, "sub:cancel"))
    assert (
        "editUserStarSubscription",
        {"user_id": GUEST, "telegram_payment_charge_id": "ch1", "is_canceled": True},
    ) in tg.calls
    assert db.get_user(GUEST).sub_charge_id is None


async def test_payments_disabled_hides_buy_button(db, tg):
    bot = make_bot(db, tg, trial_days=0, pro_price_stars=0)
    onboarded_user(db, GUEST)
    await bot.handle_update(message(GUEST, r.BTN_PLAN))
    assert not any(m == "createInvoiceLink" for m, _ in tg.calls)


async def test_admin_commands(bot, tg, db):
    onboarded_user(db, GUEST)
    await bot.handle_update(message(GUEST, "/admin"))
    assert "Только для администратора" in tg.texts(GUEST)[-1]
    await bot.handle_update(message(ADMIN, f"/grant {GUEST} 30"))
    assert bot.billing.plan(db.get_user(GUEST)).name == "pro"
    assert "Pro до" in tg.texts(GUEST)[-1]
    await bot.handle_update(message(ADMIN, "/admin"))
    assert "На Pro: 1" in tg.texts(ADMIN)[-1]
    await bot.handle_update(message(ADMIN, "/users"))
    assert "💎" in tg.texts(ADMIN)[-1]


# --- резюме и вакансии ------------------------------------------------------------


async def test_text_resume_flow_with_hook(bot, tg, db):
    onboarded_user(db, ADMIN)
    received = []

    async def hook(user, resume):
        received.append(resume)
        return "профиль готов"

    bot.on_resume = hook
    await bot.handle_update(message(ADMIN, "/resume"))
    resume = "Python-разработчик, 2 года опыта, Django, PostgreSQL, Docker. " * 2
    await bot.handle_update(message(ADMIN, resume))
    (stored,) = db.get_resumes(ADMIN)
    assert stored.text == resume.strip() and stored.title == "Резюме 1"
    assert received[0].id == stored.id and tg.texts(ADMIN)[-1] == "профиль готов"


async def test_documents_become_resumes_and_can_be_deleted(bot, tg, db):
    onboarded_user(db, ADMIN)
    tg.files["f1"] = ("QA engineer: Selenium, Postman, SQL, тест-кейсы, баг-репорты. " * 2).encode()
    tg.files["f2"] = b"just a picture"
    await bot.handle_update(document(ADMIN, "f1", "QA_resume.txt"))
    await bot.handle_update(document(ADMIN, "f2", "photo.jpg"))
    (res,) = db.get_resumes(ADMIN)
    assert (res.title, res.file_id, res.file_name) == ("QA resume", "f1", "QA_resume.txt")
    assert "Не удалось прочитать «photo.jpg»" in tg.texts(ADMIN)[-1]
    await bot.handle_update(message(ADMIN, r.BTN_RESUMES))
    assert "QA resume" in tg.texts(ADMIN)[-1]
    await bot.handle_update(callback(ADMIN, f"rdel:{res.id}"))
    assert db.get_resumes(ADMIN) == []
    onboarded_user(db, GUEST)
    other = db.add_resume(GUEST, "x", "y" * 60)
    await bot.handle_update(callback(ADMIN, f"rdel:{other}"))  # чужое удалить нельзя
    assert len(db.get_resumes(GUEST)) == 1


async def test_send_vacancy_and_dislike(bot, tg, db):
    onboarded_user(db, ADMIN)
    user = db.get_user(ADMIN)
    v = Vacancy(source="habr", external_id="1", title="Python <Dev>", url="https://x/1")
    db.save_vacancies([v])
    db.record(ADMIN, v, "sent")
    did = db.delivery_id(ADMIN, v.uid)
    await bot.send_vacancy(user, v, did, extra="🤖 80/100")
    method, payload = tg.calls[-1]
    assert method == "sendMessage" and "Python &lt;Dev&gt;" in payload["text"]
    assert payload["reply_markup"]["inline_keyboard"][0][-1]["callback_data"] == f"nope:{did}"
    await bot.handle_update(callback(ADMIN, f"nope:{did}"))
    assert db.stats(ADMIN) == {"disliked": 1}


async def test_daily_limit_notice_once_per_day(bot, tg, db):
    onboarded_user(db, GUEST)
    user = db.get_user(GUEST)
    await bot.daily_limit_reached(user)
    await bot.daily_limit_reached(user)
    assert sum("лимит" in t for t in tg.texts(GUEST)) == 1


async def test_setup_sets_profile_texts(bot, tg):
    await bot.setup()
    methods = [m for m, _ in tg.calls]
    assert {"getMe", "setMyCommands", "setMyDescription", "setMyShortDescription"} <= set(methods)


def pre_checkout(user_id: int, amount: int) -> dict:
    return {
        "update_id": 3,
        "pre_checkout_query": {
            "id": "pc",
            "from": {"id": user_id},
            "currency": "XTR",
            "total_amount": amount,
            "invoice_payload": f"pro:{user_id}",
        },
    }


def paid(user_id: int, charge: str, *, first: bool) -> dict:
    update = message(user_id, "")
    update["message"]["successful_payment"] = {
        "currency": "XTR",
        "total_amount": 250,
        "invoice_payload": f"pro:{user_id}",
        "telegram_payment_charge_id": charge,
        "is_recurring": True,
        "is_first_recurring": first,
        "subscription_expiration_date": int((datetime.now(UTC) + timedelta(days=30)).timestamp()),
    }
    return update
