from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx

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
        if request.headers.get("content-type", "").startswith("multipart/"):
            payload = {"multipart": request.read().decode("latin-1")}
        else:
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


def shown(tg: FakeTelegram) -> str:
    """Текст последнего отправленного или отредактированного сообщения."""
    for method, payload in reversed(tg.calls):
        if method in ("sendMessage", "editMessageText"):
            return payload["text"]
    return ""


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
    assert "Пришлите резюме" in texts[1]
    assert any("Новый пользователь" in t for t in tg.texts(ADMIN))


async def test_invite_flow_starts_onboarding(bot, tg, db):
    await bot.handle_update(message(ADMIN, "/invite"))
    code = tg.texts(ADMIN)[-1].split("start=")[1]
    await bot.handle_update(message(GUEST, f"/start {code}"))
    assert db.get_user(GUEST).allowed
    assert "Пришлите резюме" in tg.texts(GUEST)[-1]
    await bot.handle_update(message(300, f"/start {code}"))  # ссылка одноразовая
    assert not db.get_user(300).allowed


async def test_admin_approves_by_button(bot, tg, db):
    await bot.handle_update(message(GUEST, "/start"))
    await bot.handle_update(callback(ADMIN, f"allow:{GUEST}"))
    assert db.get_user(GUEST).allowed
    await bot.handle_update(callback(GUEST, "allow:300"))  # не-админ не может
    assert db.get_user(300) is None


async def test_manual_onboarding_questionnaire(db, tg):
    bot = make_bot(db, tg, access_mode="open", trial_days=7)
    await bot.handle_update(message(GUEST, "/start"))
    await bot.handle_update(callback(GUEST, "ob:manual"))
    assert "Что ищем?" in shown(tg)
    await bot.handle_update(callback(GUEST, "pq:next:directions"))
    assert tg.calls[-1][1]["text"] == "Выберите хотя бы одно направление"
    await bot.handle_update(callback(GUEST, "pq:dir:py"))
    await bot.handle_update(callback(GUEST, "pq:custom"))
    await bot.handle_update(message(GUEST, "аналитик данных, python developer"))
    await bot.handle_update(callback(GUEST, "pq:next:directions"))
    assert "уровня" in shown(tg)
    await bot.handle_update(callback(GUEST, "pq:next:level"))
    assert tg.calls[-1][1]["text"] == "Выберите хотя бы один уровень"
    # несколько грейдов сразу: стажёр и junior, а потом junior снят и добавлен middle
    for lvl in ("intern", "junior", "middle", "junior"):
        await bot.handle_update(callback(GUEST, f"pq:level:{lvl}"))
    assert db.get_user(GUEST).filters.levels == ["intern", "middle"]
    await bot.handle_update(callback(GUEST, "pq:next:level"))
    await bot.handle_update(callback(GUEST, "pq:next:work_formats"))
    assert tg.calls[-1][1]["text"] == "Выберите хотя бы один формат"
    await bot.handle_update(callback(GUEST, "pq:fmt:remote"))
    await bot.handle_update(callback(GUEST, "pq:fmt:office"))
    await bot.handle_update(callback(GUEST, "pq:next:work_formats"))
    await bot.handle_update(callback(GUEST, "pq:reloc:yes"))
    await bot.handle_update(callback(GUEST, "pq:rc:PL"))
    await bot.handle_update(callback(GUEST, "pq:next:relocation_countries"))
    await bot.handle_update(callback(GUEST, "pq:salc"))
    await bot.handle_update(message(GUEST, "1 500 $"))

    user = db.get_user(GUEST)
    f, profile = user.filters, db.get_profile(GUEST)
    assert user.onboarded
    # ручной ввод только добавляет: уже выбранный «python developer» не снимается
    assert f.queries == ["python developer", "аналитик данных"]
    assert f.levels == ["intern", "middle"]
    assert f.countries == ["BY", "PL", "REMOTE"] and not f.remote_only
    assert f.min_salary_usd == 1200  # 80% от ожиданий
    assert profile.relocation and profile.relocation_countries == ["PL"]
    assert "Готово" in tg.texts(GUEST)[-1]


async def test_resume_onboarding_asks_only_missing(db, tg):
    bot = make_bot(db, tg, access_mode="open")

    async def fake_ai(user, resume):
        profile = db.get_profile(user.id)
        profile.merge_extracted(
            {
                "level": "junior",
                "skills": ["python", "fastapi"],
                "city": "Минск",
                "country": "BY",
                "work_formats": ["remote", "office"],
                "relocation": False,
                "directions": ["python developer", "backend developer"],
            }
        )
        db.save_profile(user.id, profile)
        return "✅ Резюме разобрано"

    bot.on_resume = fake_ai
    await bot.handle_update(message(GUEST, "/start"))
    tg.files["f1"] = ("Python developer: FastAPI, PostgreSQL, Docker, Git. " * 3).encode()
    await bot.handle_update(document(GUEST, "f1", "cv.txt"))
    # сначала человек видит, что понял ИИ, и может это поправить
    assert "Вот что я понял" in shown(tg)
    assert {"ob:next", "pe:level", "pe:directions"} <= {b.get("callback_data") for b in buttons(tg)}
    assert db.get_user(GUEST).filters.levels == ["intern", "junior"]
    # ИИ ошибся с уровнем: убираем стажёра, добавляем middle и возвращаемся к профилю
    await bot.handle_update(callback(GUEST, "pe:level"))
    await bot.handle_update(callback(GUEST, "pq:level:intern"))
    await bot.handle_update(callback(GUEST, "pq:level:middle"))
    await bot.handle_update(callback(GUEST, "pq:next:level"))
    assert "Вот что я понял" not in shown(tg) and "Middle" in shown(tg)
    assert not db.get_user(GUEST).onboarded
    await bot.handle_update(callback(GUEST, "ob:next"))
    assert "Сколько хотите получать" in shown(tg)  # всё остальное есть в резюме
    await bot.handle_update(callback(GUEST, "pq:sal:0"))
    user = db.get_user(GUEST)
    assert user.onboarded
    assert user.filters.queries == ["python developer", "backend developer"]
    assert user.filters.countries == ["BY", "REMOTE"] and user.filters.min_salary_usd == 0
    assert user.filters.levels == ["junior", "middle"]


async def test_profile_edit_updates_search(bot, tg, db):
    onboarded_user(db, ADMIN, queries=["python developer"])
    await bot.handle_update(message(ADMIN, r.BTN_PROFILE))
    assert "Профиль" in tg.texts(ADMIN)[-1]
    assert any(b.get("callback_data") == "pe:fill_missing" for b in buttons(tg))
    await bot.handle_update(callback(ADMIN, "pe:level"))
    await bot.handle_update(callback(ADMIN, "pq:level:middle"))
    await bot.handle_update(callback(ADMIN, "pq:level:senior"))
    assert db.get_user(ADMIN).filters.levels == ["middle", "senior"]
    await bot.handle_update(callback(ADMIN, "pq:next:level"))
    assert "Профиль" in shown(tg)  # вернулись к карточке
    assert "Middle" in shown(tg) and "Senior" in shown(tg)
    await bot.handle_update(callback(ADMIN, "pe:skills"))
    await bot.handle_update(message(ADMIN, "python, sql"))
    assert db.get_profile(ADMIN).skills == ["python", "sql"]
    assert db.get_user(ADMIN).filters.skills == ["python", "sql"]


async def test_resume_without_ai_goes_to_questions(db, tg):
    bot = make_bot(db, tg, access_mode="open")
    await bot.handle_update(message(GUEST, "/start"))
    tg.files["f1"] = ("Python developer: FastAPI, PostgreSQL, Docker, Git. " * 3).encode()
    await bot.handle_update(document(GUEST, "f1", "cv.txt"))
    assert len(db.get_resumes(GUEST)) == 1
    assert "Что ищем?" in tg.texts(GUEST)[-1]


# --- меню и фильтры -------------------------------------------------------------


async def test_menu_buttons(bot, tg, db):
    onboarded_user(db, GUEST, queries=["python developer"])
    await bot.handle_update(message(GUEST, r.BTN_SEARCH))
    assert "python developer" in tg.texts(GUEST)[-1]
    await bot.handle_update(message(GUEST, r.BTN_FILTERS))
    assert "Фильтры" in tg.texts(GUEST)[-1]
    await bot.handle_update(message(GUEST, r.BTN_STATS))
    assert "Статистика" in tg.texts(GUEST)[-1]
    await bot.handle_update(message(GUEST, r.BTN_PROFILE))
    assert "Профиль" in tg.texts(GUEST)[-1]
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
    assert received[0].id == stored.id and "профиль готов" in tg.texts(ADMIN)


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


async def test_format_and_city_screen(bot, tg, db):
    onboarded_user(db, ADMIN)
    await bot.handle_update(callback(ADMIN, "pe:fmtcity"))
    assert "Формат и город" in shown(tg)
    await bot.handle_update(callback(ADMIN, "pf:fmt:hybrid"))
    await bot.handle_update(callback(ADMIN, "pf:city"))  # города нет — спросит
    await bot.handle_update(message(ADMIN, "Минск"))
    await bot.handle_update(callback(ADMIN, "pf:strict"))
    p, f = db.get_profile(ADMIN), db.get_user(ADMIN).filters
    assert p.work_formats == ["hybrid"] and p.city == "Минск" and p.only_home_city
    assert f.formats == ["hybrid"] and f.only_home_city and f.home_city == "Минск"
    assert f.strict_format
    await bot.handle_update(callback(ADMIN, "pf:fmt:hybrid"))  # последний формат не снять
    assert db.get_profile(ADMIN).work_formats == ["hybrid"]


async def test_wishes(bot, tg, db):
    onboarded_user(db, ADMIN)
    await bot.handle_update(callback(ADMIN, "pe:wishes"))
    await bot.handle_update(message(ADMIN, "Фрунзенский район, без ночных смен"))
    assert db.get_profile(ADMIN).wishes == "Фрунзенский район, без ночных смен"
    assert "Пожелания" in tg.texts(ADMIN)[-1]


async def test_free_text_edit_with_confirmation(bot, tg, db):
    onboarded_user(db, ADMIN, queries=["python developer"])
    profile = db.get_profile(ADMIN)
    profile.city, profile.country, profile.work_formats = "Минск", "BY", ["remote", "office"]
    db.save_profile(ADMIN, profile)

    async def fake_parse(user, text):
        if "непонятно" in text:
            return {"unclear": "о каком городе речь"}
        return {"work_formats": ["hybrid"], "only_home_city": True, "wishes": "Фрунзенский район"}

    bot.on_profile_edit = fake_parse
    await bot.handle_update(message(ADMIN, "хочу только гибрид во Фрунзенском районе"))
    assert "Понял так" in tg.texts(ADMIN)[-1] and "Гибрид" in tg.texts(ADMIN)[-1]
    assert db.get_profile(ADMIN).work_formats == ["remote", "office"]  # ещё не применено
    await bot.handle_update(callback(ADMIN, "nl:apply"))
    p, f = db.get_profile(ADMIN), db.get_user(ADMIN).filters
    assert p.work_formats == ["hybrid"] and p.wishes == "Фрунзенский район"
    assert f.formats == ["hybrid"] and f.only_home_city

    await bot.handle_update(message(ADMIN, "сделай непонятно что"))
    assert "о каком городе речь" in tg.texts(ADMIN)[-1]
    await bot.handle_update(message(ADMIN, "только офис"))
    await bot.handle_update(callback(ADMIN, "nl:cancel"))
    assert db.get_profile(ADMIN).work_formats == ["hybrid"]


async def test_vacancy_card_shows_format_and_address(bot, tg, db):
    onboarded_user(db, ADMIN)
    v = Vacancy(
        source="habr",
        external_id="2",
        title="Dev",
        url="https://x/2",
        location="Минск",
        work_formats=("hybrid",),
        address="ул. Притыцкого, 29 (м. Пушкинская)",
    )
    db.save_vacancies([v])
    db.record(ADMIN, v, "sent")
    await bot.send_vacancy(db.get_user(ADMIN), v, db.delivery_id(ADMIN, v.uid))
    text = tg.calls[-1][1]["text"]
    assert "🔀 гибрид" in text and "м. Пушкинская" in text


# --- фидбек тестировщиков: ввод, грейды, защита, данные ----------------------------


async def test_unrealistic_salary_does_not_break_bot(bot, tg, db):
    """Регрессия: огромная зарплата ломала бота (OverflowError в профиле)."""
    onboarded_user(db, ADMIN)
    await bot.handle_update(callback(ADMIN, "pe:salary"))
    await bot.handle_update(callback(ADMIN, "pq:salc"))
    await bot.handle_update(message(ADMIN, "9" * 500))
    assert "Слишком большая сумма" in tg.texts(ADMIN)[-1]
    assert db.get_profile(ADMIN).salary_usd is None
    # бот продолжает ждать сумму и принимает нормальную
    await bot.handle_update(message(ADMIN, "1 800 $"))
    assert db.get_profile(ADMIN).salary_usd == 1800
    assert db.get_user(ADMIN).filters.min_salary_usd == 1440
    await bot.handle_update(callback(ADMIN, "f:salary"))
    await bot.handle_update(message(ADMIN, "100000000000000000000"))
    assert "Слишком большая сумма" in tg.texts(ADMIN)[-1]
    await bot.handle_update(message(ADMIN, "/salary 10" + "0" * 30))
    assert "Слишком большая сумма" in tg.texts(ADMIN)[-1]
    await bot.handle_update(message(ADMIN, "/threshold ²"))
    assert "от 0 до 100" in tg.texts(ADMIN)[-1]
    await bot.handle_update(message(ADMIN, r.BTN_PROFILE))
    assert "Профиль" in tg.texts(ADMIN)[-1]


async def test_long_lists_are_trimmed(bot, tg, db):
    onboarded_user(db, ADMIN)
    await bot.handle_update(callback(ADMIN, "f:exclude"))
    await bot.handle_update(message(ADMIN, ", ".join(f"слово{i}" * 20 for i in range(100))))
    f = db.get_user(ADMIN).filters
    assert len(f.exclude) == 30 and all(len(w) <= 60 for w in f.exclude)


async def test_level_removed_in_filters_stays_removed(bot, tg, db):
    """Регрессия: снятый в фильтрах грейд возвращался после любой правки профиля."""
    onboarded_user(db, ADMIN, levels=["intern", "junior"])
    profile = db.get_profile(ADMIN)
    profile.level = "junior"
    db.save_profile(ADMIN, profile)
    await bot.handle_update(callback(ADMIN, "f:lvl:intern"))
    await bot.handle_update(callback(ADMIN, "f:lvl:middle"))
    assert db.get_user(ADMIN).filters.levels == ["junior", "middle"]
    await bot.handle_update(callback(ADMIN, "pf:strict"))  # любая правка профиля
    assert db.get_user(ADMIN).filters.levels == ["junior", "middle"]
    await bot.handle_update(message(ADMIN, r.BTN_PROFILE))
    assert "Junior" in tg.texts(ADMIN)[-1] and "Middle" in tg.texts(ADMIN)[-1]


async def test_new_resume_shows_editable_profile(bot, tg, db):
    onboarded_user(db, ADMIN)

    async def fake_ai(user, resume):
        profile = db.get_profile(user.id)
        profile.merge_extracted({"level": "senior", "skills": ["go"]})
        db.save_profile(user.id, profile)
        return "✅ Резюме разобрано"

    bot.on_resume = fake_ai
    await bot.handle_update(message(ADMIN, "/resume " + "Go developer, Kubernetes. " * 5))
    assert "Профиль дополнен из резюме" in shown(tg)
    assert {"pe:level", "pe:fill"} <= {b.get("callback_data") for b in buttons(tg)}


async def test_rate_limit(db, tg):
    bot = make_bot(db, tg)
    onboarded_user(db, GUEST)
    for _ in range(30):
        await bot.handle_update(message(GUEST, r.BTN_HELP))
    before = len(tg.texts(GUEST))
    await bot.handle_update(message(GUEST, r.BTN_HELP))
    await bot.handle_update(message(GUEST, r.BTN_HELP))
    texts = tg.texts(GUEST)
    assert len(texts) == before + 1 and "Слишком много" in texts[-1]
    await bot.handle_update(callback(GUEST, "st:show"))
    assert tg.calls[-1][1]["text"] == "Слишком часто — подождите минуту"
    # админа лимит не касается
    onboarded_user(db, ADMIN)
    for _ in range(40):
        await bot.handle_update(message(ADMIN, r.BTN_HELP))
    assert "Как это работает" in tg.texts(ADMIN)[-1]


async def test_daily_upload_limit(db, tg):
    bot = make_bot(db, tg)
    onboarded_user(db, GUEST)
    tg.files["f"] = b"x"
    for _ in range(10):
        await bot.handle_update(document(GUEST, "f", "photo.jpg"))
    await bot.handle_update(document(GUEST, "f", "photo.jpg"))
    assert "лимит — 10 файлов" in tg.texts(GUEST)[-1]


async def test_ban_and_unban(bot, tg, db):
    onboarded_user(db, GUEST)
    await bot.handle_update(message(GUEST, "/ban 100"))
    assert "Только для администратора" in tg.texts(GUEST)[-1]
    await bot.handle_update(message(ADMIN, f"/ban {GUEST}"))
    assert db.get_user(GUEST).banned and "Забанен" in tg.texts(ADMIN)[-1]
    assert GUEST not in [u.id for u in db.active_users()]
    before = len(tg.calls)
    await bot.handle_update(message(GUEST, r.BTN_SEARCH))
    assert len(tg.calls) == before  # забаненному не отвечаем
    await bot.handle_update(message(ADMIN, f"/ban {ADMIN}"))
    assert "нельзя" in tg.texts(ADMIN)[-1]
    await bot.handle_update(message(ADMIN, f"/unban {GUEST}"))
    await bot.handle_update(message(GUEST, r.BTN_SEARCH))
    assert "Мой поиск" in tg.texts(GUEST)[-1]


async def test_privacy_and_delete_my_data(bot, tg, db):
    onboarded_user(db, GUEST, queries=["python developer"])
    db.add_resume(GUEST, "cv", "Python developer " * 10)
    profile = db.get_profile(GUEST)
    profile.city = "Минск"
    db.save_profile(GUEST, profile)
    db.set_plan(GUEST, datetime.now(UTC) + timedelta(days=3), trial_used=True)
    await bot.handle_update(message(GUEST, "/privacy"))
    assert "ИИ-провайдеру" in tg.texts(GUEST)[-1] and "180 дней" in tg.texts(GUEST)[-1]
    await bot.handle_update(callback(GUEST, "del:ask"))
    assert "Удалить мои данные?" in shown(tg)
    await bot.handle_update(callback(GUEST, "del:no"))
    assert db.get_resumes(GUEST)
    await bot.handle_update(message(GUEST, "/delete_me"))
    await bot.handle_update(callback(GUEST, "del:yes"))
    user = db.get_user(GUEST)
    assert db.get_resumes(GUEST) == [] and db.get_profile(GUEST).city == ""
    assert user.filters.queries == [] and not user.onboarded
    assert user.trial_used and user.plan_until is not None  # пробный период заново не взять
    assert any("данные удалены" in p.get("text", "") for _, p in tg.calls)
    assert any("удалил свои данные" in t for t in tg.texts(ADMIN))


async def test_help_and_welcome_mention_privacy(db, tg):
    bot = make_bot(db, tg, access_mode="open")
    await bot.handle_update(message(GUEST, "/start"))
    assert "/privacy" in tg.texts(GUEST)[0]
    await bot.handle_update(message(GUEST, "/help"))
    assert "/delete_me" in tg.texts(GUEST)[-1]


def test_describe_update_hides_message_text():
    from jobspybot.telegram.bot import describe_update

    sender = {"id": 7, "username": "misha"}
    assert describe_update({"message": {"from": sender, "text": "/start abc"}}) == (
        "7 @misha: /start"
    )
    assert describe_update({"message": {"from": sender, "text": "моё резюме"}}) == (
        "7 @misha: текст, 10 симв."
    )
    assert describe_update({"callback_query": {"from": {"id": 7}, "data": "menu"}}) == (
        "7: кнопка menu"
    )
