"""Онбординг: длинный ввод, повторные резюме, «начать заново» (фидбек Никиты, #48)."""

from __future__ import annotations

import json

from jobspybot.filters import UserFilters
from jobspybot.profile import UserProfile
from jobspybot.telegram import render as r
from jobspybot.telegram.bot import merge_split_messages
from tests.test_bot import (
    ADMIN,
    GUEST,
    buttons,
    callback,
    document,
    message,
    onboarded_user,
    shown,
)

ZEROS = "0" * 4096
RESUME = "Бизнес-аналитик: BPMN, IDEF0, EPC, SQL, 1С, MS Project, презентации. " * 3


def chunk(user_id: int, text: str, date: int, update_id: int = 1) -> dict:
    update = message(user_id, text)
    update["update_id"] = update_id
    update["message"]["date"] = date
    return update


def chat_member(user_id: int, status: str) -> dict:
    return {
        "update_id": 3,
        "my_chat_member": {
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id},
            "new_chat_member": {"status": status},
        },
    }


def guest(db):
    db.ensure_user(GUEST, "user200", allowed=True)


def parse_hook(db, *, summary="Начинающий бизнес-аналитик", education=None):
    """ИИ-разбор: заполняет профиль соискателя и сохраняет разбор резюме."""

    async def hook(user, resume):
        profile = db.get_profile(user.id)
        profile.merge_extracted(
            {
                "headline": "Бизнес-аналитик",
                "level": "intern",
                "skills": ["sql", "bpmn"],
                "education": education or [],
            }
        )
        db.save_profile(user.id, profile)
        db.set_resume_profile(
            resume.id, json.dumps({"summary": summary, "skills": ["sql", "bpmn"]})
        )
        return "✅ Резюме разобрано: навыки sql, bpmn"

    return hook


# --- длинные сообщения -------------------------------------------------------------


def test_merge_split_messages():
    parts = [chunk(GUEST, ZEROS, 10, 1), chunk(GUEST, ZEROS, 10, 2), chunk(GUEST, "000", 11, 3)]
    other = chunk(ADMIN, "привет", 11, 4)
    merged = merge_split_messages([*parts, other])
    assert len(merged) == 2
    assert merged[0]["update_id"] == 3  # offset сдвигается за последний кусок
    assert len(merged[0]["message"]["text"]) == 4096 * 2 + 3 + 2
    # короткие сообщения подряд не склеиваются, команды — тоже
    short = [chunk(GUEST, "1500", 10, 1), chunk(GUEST, "2000", 10, 2)]
    assert len(merge_split_messages(short)) == 2
    assert len(merge_split_messages([chunk(GUEST, ZEROS, 10, 1), chunk(GUEST, "/start", 10)])) == 2


async def test_flood_of_zeros_does_not_create_resumes(bot, tg, db):
    guest(db)
    await bot.handle_update(message(GUEST, "/start"))  # ждёт резюме
    await bot.handle_update(chunk(GUEST, ZEROS, 100))
    assert "не похоже на резюме" in shown(tg)
    sent = len(tg.texts(GUEST))
    # хвосты того же сообщения, пришедшие отдельной пачкой, молча пропускаются
    await bot.handle_update(chunk(GUEST, ZEROS, 100))
    await bot.handle_update(chunk(GUEST, "0000", 101))
    assert len(tg.texts(GUEST)) == sent
    assert db.get_resumes(GUEST) == []
    # а через пару секунд обычный ответ снова принимается
    bot.on_resume = parse_hook(db)
    await bot.handle_update(chunk(GUEST, RESUME, 110))
    assert len(db.get_resumes(GUEST)) == 1


async def test_huge_salary_is_rejected_once(bot, tg, db):
    guest(db)
    await bot.handle_update(message(GUEST, "/start"))
    await bot.handle_update(callback(GUEST, "ob:manual"))
    await bot.handle_update(callback(GUEST, "pq:salc"))
    for update in merge_split_messages([chunk(GUEST, ZEROS, 50, i) for i in range(4)]):
        await bot.handle_update(update)
    assert "Слишком большая сумма" in shown(tg)
    assert db.get_resumes(GUEST) == [] and db.get_profile(GUEST).salary_usd is None
    await bot.handle_update(message(GUEST, "1300"))
    assert db.get_profile(GUEST).salary_usd == 1300


async def test_same_resume_is_not_stored_twice(bot, tg, db):
    guest(db)
    bot.on_resume = parse_hook(db)
    await bot.handle_update(message(GUEST, "/start"))
    tg.files["f1"] = RESUME.encode()
    await bot.handle_update(document(GUEST, "f1", "cv.txt"))
    await bot.handle_update(document(GUEST, "f1", "cv.txt"))
    assert len(db.get_resumes(GUEST)) == 1
    assert any("уже загружено" in t for t in tg.texts(GUEST))


# --- один экран после разбора резюме -------------------------------------------------


async def test_parsed_resume_is_one_message_with_education(bot, tg, db):
    guest(db)
    bot.on_resume = parse_hook(db, education=["БГЭУ, экономика, не окончено"])
    await bot.handle_update(message(GUEST, "/start"))
    tg.calls.clear()
    tg.files["f1"] = RESUME.encode()
    await bot.handle_update(document(GUEST, "f1", "cv.txt"))
    texts = tg.texts(GUEST)
    # «Читаю…» и карточка; ответ ИИ с теми же навыками отдельно не дублируется
    assert len(texts) == 2 and "Читаю" in texts[0]
    card = texts[1]
    assert "Вот что я понял" in card and "Начинающий бизнес-аналитик" in card
    assert "БГЭУ, экономика, не окончено" in card
    assert card.count("sql") == 1
    assert "Потом спрошу" in card and "зарплату" in card
    assert "</blockquote>\n<blockquote>Навыки" in card  # блоки — отдельными карточками


async def test_failed_parse_still_explains(bot, tg, db):
    guest(db)

    async def hook(user, resume):
        return "✅ Резюме сохранено, но ИИ сейчас недоступен."

    bot.on_resume = hook
    await bot.handle_update(message(GUEST, "/start"))
    await bot.handle_update(message(GUEST, RESUME))
    assert any("ИИ сейчас недоступен" in t for t in tg.texts(GUEST))


def test_garbage_salary_is_asked_again():
    raw = json.dumps({"salary_usd": 10**40, "answered": ["salary"], "headline": "QA"})
    profile = UserProfile.from_json(raw)
    assert profile.salary_usd is None and "salary" not in profile.answered
    assert "salary" in profile.missing(UserFilters())
    keep = UserProfile.from_json(json.dumps({"salary_usd": 0, "answered": ["salary"]}))
    assert keep.salary_usd == 0 and keep.answered == ["salary"]


def test_education_in_card():
    p = UserProfile()
    p.merge_extracted({"education": ["БНТУ, ПОИТ, 2024", "  ", "Курсы Python, 2023"]})
    card = r.profile_card(p, [])
    assert "Образование:" in card and "БНТУ, ПОИТ, 2024" in card and "Курсы Python" in card


# --- начать заново -----------------------------------------------------------------


async def test_start_mid_onboarding_resets(bot, tg, db):
    guest(db)
    bot.on_resume = parse_hook(db)
    await bot.handle_update(message(GUEST, "/start"))
    await bot.handle_update(message(GUEST, RESUME))
    assert db.get_resumes(GUEST)
    # случайный текст посреди настройки не перезапускает онбординг
    await bot.handle_update(message(GUEST, "что дальше?"))
    assert "Ответь на вопрос выше" in shown(tg) and db.get_resumes(GUEST)
    # а /start — начинает с чистого листа
    await bot.handle_update(message(GUEST, "/start"))
    assert any("Начинаем заново" in t for t in tg.texts(GUEST))
    assert db.get_resumes(GUEST) == [] and db.get_profile(GUEST).is_empty
    assert "Пришли резюме" in shown(tg)


async def test_comeback_after_stopping_bot(bot, tg, db):
    onboarded_user(db, GUEST, queries=["python developer"])
    db.add_resume(GUEST, "CV", RESUME)
    await bot.handle_update(chat_member(GUEST, "kicked"))
    user = db.get_user(GUEST)
    assert user.paused and user.stopped_at is not None
    await bot.handle_update(message(GUEST, "/start"))
    assert "С возвращением" in shown(tg)
    assert {"back:keep", "back:reset"} <= {b.get("callback_data") for b in buttons(tg)}
    await bot.handle_update(callback(GUEST, "back:keep"))
    user = db.get_user(GUEST)
    assert not user.paused and user.stopped_at is None and user.onboarded
    assert user.filters.queries == ["python developer"]
    # второй раз: остановил и выбрал «Начать заново»
    await bot.handle_update(chat_member(GUEST, "kicked"))
    await bot.handle_update(message(GUEST, "/start"))
    await bot.handle_update(callback(GUEST, "back:reset"))
    user = db.get_user(GUEST)
    assert not user.onboarded and not user.paused and user.filters.queries == []
    assert db.get_resumes(GUEST) == [] and "Пришли резюме" in shown(tg)


async def test_reset_command_keeps_plan(bot, tg, db):
    onboarded_user(db, GUEST, queries=["qa"])
    bot.billing.extend(db.get_user(GUEST), 30)
    await bot.handle_update(message(GUEST, "/reset"))
    assert {"back:reset", "back:cancel"} <= {b.get("callback_data") for b in buttons(tg)}
    await bot.handle_update(callback(GUEST, "back:reset"))
    user = db.get_user(GUEST)
    assert not user.onboarded and user.plan_until is not None and user.allowed
