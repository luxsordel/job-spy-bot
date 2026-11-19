from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobspybot.filters import UserFilters, check, contains_term, local_score, to_usd
from jobspybot.models import Level, Vacancy

NOW = datetime(2026, 11, 19, tzinfo=UTC)


def vac(**kw) -> Vacancy:
    base = dict(
        source="s",
        external_id="1",
        title="Python Developer",
        url="u",
        company="Acme",
        published_at=NOW - timedelta(hours=1),
    )
    base.update(kw)
    return Vacancy(**base)


@pytest.mark.parametrize(
    ("text", "term", "expected"),
    [
        ("Senior C++ developer", "c++", True),
        ("C# / .NET", "c#", True),
        ("Программист 1С", "1с", True),
        ("JavaScript developer", "java", False),
        ("Java developer", "java", True),
        ("Ведущий инженер", "ведущий", True),
        ("Наёмный работник", "наемный", True),
        ("anything", "  ", False),
    ],
)
def test_contains_term(text, term, expected):
    assert contains_term(text, term) is expected


def test_to_usd():
    assert to_usd(1000, "BYN") == pytest.approx(310)
    assert to_usd(None, "USD") is None
    assert to_usd(100, "XXX") is None


@pytest.mark.parametrize(
    ("vacancy", "filters", "reason_part"),
    [
        (vac(published_at=NOW - timedelta(days=30)), UserFilters(), "старая"),
        (vac(title="Senior Python"), UserFilters(exclude=["senior"]), "стоп-слово"),
        (vac(company="ООО Казино"), UserFilters(exclude=["казино"]), "стоп-слово"),
        (vac(title="Data Engineer"), UserFilters(include=["python", "django"]), "обязательных"),
        (vac(level=Level.SENIOR), UserFilters(levels=["junior"]), "уровень"),
        (vac(remote=None), UserFilters(remote_only=True), "удалёнка"),
        (vac(country="RU"), UserFilters(countries=["BY", "PL"]), "страна"),
        (vac(salary_from=1000, currency="BYN"), UserFilters(min_salary_usd=1000), "зарплата"),
        (vac(), UserFilters(min_salary_usd=1000, require_salary=True), "не указана"),
    ],
)
def test_check_rejects(vacancy, filters, reason_part):
    assert reason_part in check(vacancy, filters, now=NOW)


@pytest.mark.parametrize(
    ("vacancy", "filters"),
    [
        (vac(), UserFilters()),
        (vac(level=None), UserFilters(levels=["junior"])),  # уровень неизвестен — пропускаем
        (vac(country=""), UserFilters(countries=["BY"])),  # страна неизвестна — пропускаем
        (vac(country="RU", remote=True), UserFilters(countries=["BY", "REMOTE"])),
        (vac(), UserFilters(min_salary_usd=1000)),  # без зарплаты, но не strict
        (vac(salary_to=5000, currency="BYN"), UserFilters(min_salary_usd=1000)),
        (vac(title="Python / Django"), UserFilters(include=["django"])),
    ],
)
def test_check_passes(vacancy, filters):
    assert check(vacancy, filters, now=NOW) is None


def test_local_score_prefers_title_hits():
    skills = ["python", "django", "sql"]
    strong = local_score(vac(title="Python Django developer", skills=("SQL",)), skills)
    weak = local_score(vac(title="Backend developer", snippet="немного sql"), skills)
    none = local_score(vac(title="Маркетолог"), skills)
    assert strong >= 80
    assert 0 < weak < strong
    assert none == 0
    assert local_score(vac(), []) == 50


def test_filters_json_roundtrip_ignores_unknown_keys():
    f = UserFilters(queries=["python"], levels=["junior"], min_salary_usd=800)
    assert UserFilters.from_json(f.to_json()) == f
    assert UserFilters.from_json('{"queries": ["x"], "obsolete": 1}').queries == ["x"]


def test_format_filter():
    f = UserFilters(formats=["hybrid"])
    assert check(vac(work_formats=("hybrid", "remote")), f, now=NOW) is None
    assert "формат" in check(vac(work_formats=("office",)), f, now=NOW)
    assert check(vac(), f, now=NOW) is None  # формат неизвестен — пропускаем
    f.strict_format = True
    assert "не указан" in check(vac(), f, now=NOW)
    # «удалёнка» без явного формата (Habr, praca.by) — тоже формат
    assert "формат" in check(vac(remote=True), UserFilters(formats=["office"]), now=NOW)


def test_home_city_filter():
    f = UserFilters(only_home_city=True, home_city="Минск", home_country="BY")
    assert check(vac(location="Минск", country="BY"), f, now=NOW) is None
    assert check(vac(location="Minsk"), f, now=NOW) is None
    assert "Минск" in check(vac(location="Гомель", country="BY"), f, now=NOW)
    # удалёнка и другие страны (релокация) не режутся
    assert check(vac(location="Гомель", country="BY", work_formats=("remote",)), f, now=NOW) is None
    assert check(vac(location="Warszawa", country="PL"), f, now=NOW) is None
    # адрес тоже считается
    assert check(vac(location="", address="г. Минск, ул. Притыцкого"), f, now=NOW) is None


def test_level_from_title_beats_source_estimate():
    """Регрессия: hh выводит уровень из опыта и превращал «Junior Backend» в middle."""
    from jobspybot.filters import title_levels

    f = UserFilters(levels=["intern", "junior"])

    def lvl(title, level=None):
        v = Vacancy(source="hh_ru", external_id="1", title=title, url="u", level=level)
        return check(v, f)

    assert lvl("Junior Backend-разработчик (Python, FastAPI)", Level.MIDDLE) is None
    assert lvl("Python Backend Junior+/Strong Junior разработчик", Level.SENIOR) is None
    assert lvl("Младший разработчик (Java)", Level.MIDDLE) is None
    assert lvl("Стажёр-разработчик", Level.MIDDLE) is None
    assert lvl("Fullstack-разработчик (Middle+ / Senior)") == "уровень middle/senior"
    assert lvl("Senior Data Engineer") == "уровень senior"
    assert lvl("Lead AI Engineer, Databricks") == "уровень lead"
    assert lvl("Python-разработчик", Level.MIDDLE) == "уровень middle"  # в названии нет — площадка
    assert lvl("Python-разработчик") is None
    assert title_levels("Главный (Senior) инженер") == {"senior", "lead"}
    assert title_levels("Валидация данных, Midnight") == set()
