"""Аналитика зарплат по собранным вакансиям (#34)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobspybot import market
from jobspybot.models import Level, Vacancy
from jobspybot.telegram import render as r
from tests.test_bot import GUEST, message, onboarded_user

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)


def vac(i: int, title: str, lo: int | None, hi: int | None = None, **kw) -> Vacancy:
    base = dict(country="BY", currency="USD", salary_from=lo, salary_to=hi, company=f"c{i}")
    return Vacancy("hh_by", str(i), title, f"u{i}", **{**base, **kw})


def test_monthly_usd():
    assert market.monthly_usd(vac(1, "x", 1000, 2000)) == 1500
    assert market.monthly_usd(vac(1, "x", None, 800)) == 800
    assert market.monthly_usd(vac(1, "x", 3000, currency="BYN")) == 930
    assert market.monthly_usd(vac(1, "x", None)) is None
    assert market.monthly_usd(vac(1, "x", 1000, currency=None)) is None
    # почасовая ставка или опечатка площадки
    assert market.monthly_usd(vac(1, "x", 25)) is None
    assert market.monthly_usd(vac(1, "x", 900_000)) is None


def test_salary_range_needs_sample():
    assert market.salary_range([1000, 1200, 1400, 1600]) is None
    rng = market.salary_range([1000, 1200, 1400, 1600, 3000])
    assert rng == market.Range(low=1200, median=1400, high=1600, count=5)


def test_by_level_groups_by_direction_level_and_country():
    vacancies = [vac(i, "Junior Python Developer", 800 + i * 100) for i in range(5)]
    vacancies += [vac(10 + i, "Senior Python Engineer", 4000 + i * 100) for i in range(5)]
    # уровень площадки, если в названии его нет
    vacancies += [vac(20 + i, "Python-разработчик", 2000, level=Level.MIDDLE) for i in range(5)]
    # другое направление, другая страна, без вилки — не считаются
    vacancies += [vac(30 + i, "Java Developer", 3000) for i in range(5)]
    vacancies += [vac(40 + i, "Junior Python Developer", 500, country="RU") for i in range(5)]
    vacancies += [vac(50, "Junior Python Developer", None)]
    m = market.Market.from_vacancies(vacancies, NOW)

    ranges = m.by_level("python developer", "BY", ["junior", "middle"])
    assert list(ranges) == ["junior", "middle"]
    assert ranges["junior"] == market.Range(900, 1000, 1100, 5)
    assert ranges["middle"].median == 2000
    assert m.by_level("python developer", "BY", [])["senior"].median == 4200
    assert m.by_level("java developer", "BY", [])["all"].count == 5  # грейдов нет — общая
    assert m.by_level("go developer", "BY", []) == {}


def test_direction_without_role_family_matches_title_text():
    m = market.Market.from_vacancies(
        [vac(i, "Главный бухгалтер", 1000 + i * 100) for i in range(5)], NOW
    )
    # «Главный» — грейд lead
    assert m.by_level("бухгалтер", "BY", [])["lead"].count == 5
    # «разработчик» — общее слово: ищется по семейству, а не по тексту
    assert market.direction_key("разработчик") == (frozenset({"dev"}), "")


def test_db_rows_skip_duplicates_and_old(db):
    same = dict(company="Acme")
    db.save_vacancies([vac(1, "Python Developer", 1000, **same), vac(2, "Python Dev", 1000)])
    db.save_vacancies([Vacancy("habr", "1", "Python Developer", "u", **same)])
    db.save_vacancies([Vacancy("habr", "9", "Go Developer", "u", company="X")])
    db.conn.execute(
        "UPDATE vacancies SET first_seen = ? WHERE uid = 'habr:9'",
        ((datetime.now(UTC) - timedelta(days=40)).isoformat(),),
    )
    # одна и та же вакансия (по отпечатку) с двух площадок считается один раз, старая — нет
    assert len(list(db.vacancy_rows(30))) == 2


async def test_market_screen(bot, tg, db):
    onboarded_user(db, GUEST)
    await bot.handle_update(message(GUEST, r.BTN_MARKET))
    assert "выберите направления" in tg.texts(GUEST)[-1]

    onboarded_user(db, GUEST, queries=["python developer"], countries=["BY", "REMOTE"])
    db.save_vacancies(vac(i, "Junior Python Developer", 800 + i * 100) for i in range(5))
    await bot.handle_update(message(GUEST, "/market"))
    text = tg.texts(GUEST)[-1]
    assert "📈 Рынок зарплат" in text and "<b>python developer</b>" in text
    assert "junior: <b>900–1 100 $</b> · медиана 1 000 $ (5)" in text
    assert "🌍 Удалёнка из любой страны\n   мало вакансий с вилкой" in text

    # снимок живёт сутки: новые вакансии появятся в нём только после пересборки
    db.save_vacancies(vac(100 + i, "Junior Python Developer", 5000) for i in range(5))
    await bot.handle_update(message(GUEST, r.BTN_MARKET))
    assert "(5)" in tg.texts(GUEST)[-1]
    bot._market = market.Market([], bot._market.built_at - market.REFRESH - timedelta(1))
    await bot.handle_update(message(GUEST, r.BTN_MARKET))
    assert "(10)" in tg.texts(GUEST)[-1]
