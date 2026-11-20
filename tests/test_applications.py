"""Трекер откликов (#29): кнопка под вакансией, статусы, список, напоминания, воронка."""

from __future__ import annotations

from datetime import timedelta

from jobspybot.db import utcnow
from jobspybot.models import Vacancy
from jobspybot.telegram import render as r
from jobspybot.telegram.bot import funnel_line
from tests.test_bot import ADMIN, GUEST, callback, message, onboarded_user


def delivered(db, uid: int, n: int = 1) -> tuple[Vacancy, int]:
    v = Vacancy(
        source="habr", external_id=str(n), title=f"Python <Dev> {n}", url=f"https://x/{n}",
        company="ACME",
    )  # fmt: skip
    db.save_vacancies([v])
    db.record(uid, v, "sent")
    did = db.delivery_id(uid, v.uid)
    assert did is not None
    return v, did


def card_callback(uid: int, data: str, markup: list) -> dict:
    update = callback(uid, data)
    update["callback_query"]["message"]["reply_markup"] = {"inline_keyboard": markup}
    return update


async def test_vacancy_card_has_applied_button(bot, tg, db):
    onboarded_user(db, ADMIN)
    v, did = delivered(db, ADMIN)
    await bot.send_vacancy(db.get_user(ADMIN), v, did)
    rows = tg.calls[-1][1]["reply_markup"]["inline_keyboard"]
    assert rows[1] == [{"text": "✅ Откликнулся", "callback_data": f"ap:{did}"}]


async def test_mark_applied_and_change_status(bot, tg, db):
    onboarded_user(db, ADMIN)
    v, did = delivered(db, ADMIN)
    markup = [
        [{"text": "🔗", "url": v.url}, {"text": "👍", "callback_data": f"like:{did}"}],
        [{"text": "✅ Откликнулся", "callback_data": f"ap:{did}"}],
    ]
    await bot.handle_update(card_callback(ADMIN, f"ap:{did}", markup))
    await bot.handle_update(card_callback(ADMIN, f"ap:{did}", markup))  # повтор — без дубля
    apps = db.applications(ADMIN)
    assert len(apps) == 1 and apps[0].status == "applied" and apps[0].company == "ACME"
    assert db.stats(ADMIN) == {"liked": 1}  # откликнулся — значит, подошла
    _, payload = next(c for c in reversed(tg.calls) if c[0] == "editMessageReplyMarkup")
    rows = payload["reply_markup"]["inline_keyboard"]
    assert rows == [
        [{"text": "🔗", "url": v.url}],
        [{"text": "📋 В откликах", "callback_data": f"apc:{apps[0].id}:new"}],
    ]

    await bot.handle_update(message(ADMIN, r.BTN_APPS))
    text = tg.texts(ADMIN)[-1]
    assert "Python &lt;Dev&gt; 1" in text and "ACME" in text and "сегодня" in text

    app_id = apps[0].id
    await bot.handle_update(callback(ADMIN, f"aps:{app_id}:interview"))
    assert db.get_application(ADMIN, app_id).status == "interview"
    edited = [p for m, p in tg.calls if m == "editMessageText"][-1]["text"]
    assert "🗣 Интервью" in edited
    assert db.application_funnel(ADMIN) == {"interview": 1}

    await bot.handle_update(callback(ADMIN, f"aps:{app_id}:hacked"))  # мусор не пишется
    assert db.get_application(ADMIN, app_id).status == "interview"
    await bot.handle_update(callback(GUEST, f"aps:{app_id}:offer"))  # чужое не меняется
    assert db.get_application(ADMIN, app_id).status == "interview"

    await bot.handle_update(callback(ADMIN, f"aps:{app_id}:del"))
    assert db.applications(ADMIN) == []
    assert "Пока пусто" in [p for m, p in tg.calls if m == "editMessageText"][-1]["text"]


async def test_reminder_after_a_week_once(bot, tg, db):
    onboarded_user(db, ADMIN)
    v, _ = delivered(db, ADMIN)
    app_id = db.add_application(ADMIN, v)
    await bot.send_application_reminders()
    assert not tg.texts(ADMIN)  # ещё рано
    old = (utcnow() - timedelta(days=8)).isoformat()
    db.conn.execute("UPDATE applications SET updated_at = ? WHERE id = ?", (old, app_id))
    await bot.send_application_reminders()
    await bot.send_application_reminders()
    texts = tg.texts(ADMIN)
    assert len(texts) == 1 and "Неделя прошла" in texts[0]
    # сменил статус — напоминание снова возможно, но не для «интервью»
    db.set_application_status(ADMIN, app_id, "interview")
    db.conn.execute("UPDATE applications SET updated_at = ? WHERE id = ?", (old, app_id))
    await bot.send_application_reminders()
    assert len(tg.texts(ADMIN)) == 1


async def test_several_stale_applications_one_message(bot, tg, db):
    onboarded_user(db, ADMIN)
    old = (utcnow() - timedelta(days=9)).isoformat()
    for n in (1, 2, 3):
        v, _ = delivered(db, ADMIN, n)
        db.add_application(ADMIN, v)
    db.conn.execute("UPDATE applications SET updated_at = ?", (old,))
    await bot.send_application_reminders()
    texts = tg.texts(ADMIN)
    assert len(texts) == 1 and "По 3 откликам" in texts[0]


async def test_followup_draft_is_pro_only(bot, tg, db):
    onboarded_user(db, GUEST)
    v, _ = delivered(db, GUEST)
    app_id = db.add_application(GUEST, v)
    asked: list[str] = []

    async def followup(user, app):
        asked.append(app.title)
        return "Здравствуйте! Напоминаю о своём отклике."

    bot.on_followup = followup
    await bot.handle_update(callback(GUEST, f"apf:{app_id}"))
    assert asked == []  # Free — предложение Pro
    await bot.handle_update(message(ADMIN, f"/grant {GUEST} 30"))
    await bot.handle_update(callback(GUEST, f"apf:{app_id}"))
    assert asked == ["Python <Dev> 1"]
    assert "Напоминаю о своём отклике" in tg.texts(GUEST)[-1]


async def test_delete_user_data_removes_applications(db):
    onboarded_user(db, ADMIN)
    v, _ = delivered(db, ADMIN)
    db.add_application(ADMIN, v)
    db.delete_user_data(ADMIN)
    assert db.applications(ADMIN) == []


def test_funnel_line():
    assert funnel_line({}) == ""
    line = funnel_line({"applied": 3, "interview": 1})
    assert "<b>4</b>" in line and "отклик отправлен 3" in line and "интервью 1" in line
