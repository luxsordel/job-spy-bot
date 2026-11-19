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
