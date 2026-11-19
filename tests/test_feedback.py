"""Обучение на 👍 / 👎 (#31)."""

from __future__ import annotations

from jobspybot import feedback
from jobspybot.models import Vacancy
from tests.test_ai import FakeLLM, add_resume, make_ai, vac
from tests.test_bot import ADMIN, FakeTelegram, buttons, callback, make_bot, onboarded_user


def test_dislike_hint():
    disliked = ["Программист 1С", "Разработчик 1С:ERP", "1С программист (junior)", "Python dev"]
    assert feedback.dislike_hint(disliked, [], []) == ("1с", 3)
    # уже в стоп-словах, нравилось или это направление поиска — не предлагаем
    assert feedback.dislike_hint(disliked, [], ["1С"]) is None
    assert feedback.dislike_hint(disliked, ["Аналитик 1С"], []) is None
    assert feedback.dislike_hint(disliked, [], [], keep=["1с разработчик"]) is None
    assert feedback.dislike_hint(disliked, [], [], already_hinted=["1с"]) is None
    # общие слова названий (developer, junior) — не стоп-слова
    assert feedback.dislike_hint(["Junior dev A", "Junior dev B", "Junior dev C"], [], []) == (
        "dev",
        3,
    )
    assert feedback.dislike_hint(["Python Developer"] * 5, [], [], keep=["python"]) is None
    assert [feedback.vacancies_word(n) for n in (1, 3, 5, 11, 22)] == [
        "1 вакансию",
        "3 вакансии",
        "5 вакансий",
        "11 вакансий",
        "22 вакансии",
    ]


def save(db, uid: int, ext: str, title: str, status: str) -> int:
    v = Vacancy(source="habr", external_id=ext, title=title, url=f"https://x/{ext}")
    db.save_vacancies([v])
    db.record(uid, v, status)
    return db.delivery_id(uid, v.uid)


async def test_like_and_stop_word_hint(db):
    tg = FakeTelegram()
    bot = make_bot(db, tg)
    onboarded_user(db, ADMIN, queries=["python developer"])
    user = db.get_user(ADMIN)
    v = Vacancy(source="habr", external_id="9", title="Python Dev", url="https://x/9")
    db.save_vacancies([v])
    db.record(ADMIN, v, "sent")
    did = db.delivery_id(ADMIN, v.uid)
    await bot.send_vacancy(user, v, did)
    row = tg.calls[-1][1]["reply_markup"]["inline_keyboard"][0]
    assert [b.get("callback_data") for b in row[-2:]] == [f"like:{did}", f"nope:{did}"]
    await bot.handle_update(callback(ADMIN, f"like:{did}"))
    assert db.stats(ADMIN) == {"liked": 1}

    ids = [save(db, ADMIN, str(i), f"Программист 1С #{i}", "sent") for i in range(3)]
    for i in ids[:2]:
        await bot.handle_update(callback(ADMIN, f"nope:{i}"))
    assert not any("стоп-слова" in t for t in tg.texts(ADMIN))
    await bot.handle_update(callback(ADMIN, f"nope:{ids[2]}"))
    assert "Вы скрыли 3 вакансии со словом «1с»" in tg.texts(ADMIN)[-1]
    assert {"exw:1с"} == {b.get("callback_data") for b in buttons(tg)}
    await bot.handle_update(callback(ADMIN, "exw:1с"))
    assert db.get_user(ADMIN).filters.exclude == ["1с"]
    # подсказка про то же слово второй раз не приходит
    extra = save(db, ADMIN, "77", "1С программист", "sent")
    sent_before = len(tg.texts(ADMIN))
    await bot.handle_update(callback(ADMIN, f"nope:{extra}"))
    assert len(tg.texts(ADMIN)) == sent_before


async def test_reactions_go_to_ai_context(db):
    fake = FakeLLM()
    ai = make_ai(db, fake)
    user = db.ensure_user(1, allowed=True)
    add_resume(db, 1, "CV", {"summary": "python", "skills": ["python"]})
    save(db, 1, "11", "Python backend в финтехе", "liked")
    save(db, 1, "12", "Программист 1С", "disliked")
    v = vac("1", "Python Dev", description="Django")
    db.save_vacancies([v])
    await ai.evaluate(user, v)
    system = fake.requests[-1]["messages"][0]["content"]
    assert "понравились: Python backend в финтехе" in system
    assert "скрыл: Программист 1С" in system
