"""Аналитика зарплат по собранным вакансиям — без ИИ, раз в сутки.

Бот хранит все вакансии площадок за 30 дней. Из тех, где указана вилка, собирается
компактный снимок: зарплата в $ в месяц, направления по названию, грейды, страна. По снимку
строится «📈 Рынок»: «junior Python в Беларуси: 800–1 400 $».

Вилки площадок смешанные (до и после налогов, курсы примерные), поэтому показываем
середину рынка — от 25-го до 75-го перцентиля и медиану, а не крайние значения.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

from jobspybot.filters import COUNTRIES, LEVEL_ORDER, title_levels, to_usd
from jobspybot.limits import MAX_SALARY_USD
from jobspybot.models import Vacancy
from jobspybot.relevance import families

#: как часто пересобирать снимок
REFRESH = timedelta(hours=24)
#: за сколько дней брать вакансии (столько они и хранятся в базе)
DAYS = 30
#: меньше вакансий — цифре нельзя верить
MIN_SAMPLE = 5
#: зарплата ниже — скорее почасовая ставка или ошибка площадки
MIN_USD = 100
#: общие семейства: «разработчик» есть в половине названий, по нему одному не группируем
_GENERIC = frozenset({"dev"})
REMOTE = "REMOTE"


@dataclass(frozen=True, slots=True)
class Point:
    usd: int
    families: frozenset[str]
    levels: frozenset[str]
    country: str
    title: str


@dataclass(frozen=True, slots=True)
class Range:
    low: int
    median: int
    high: int
    count: int


def monthly_usd(v: Vacancy) -> int | None:
    """Середина вилки в $ в месяц; None, если вилки нет или она неправдоподобна."""
    lo, hi = v.salary_from, v.salary_to
    amount = (lo + hi) / 2 if lo and hi else lo or hi
    usd = to_usd(amount, v.currency)
    if usd is None or not MIN_USD <= usd <= MAX_SALARY_USD:
        return None
    return round(usd)


def to_point(v: Vacancy) -> Point | None:
    usd = monthly_usd(v)
    if usd is None:
        return None
    levels = title_levels(v.title) or ({v.level.value} if v.level else set())
    return Point(
        usd=usd,
        families=frozenset(families(v.title)),
        levels=frozenset(levels),
        country=v.country or "",
        title=v.title.lower().replace("ё", "е"),
    )


def salary_range(values: Iterable[int]) -> Range | None:
    data = sorted(values)
    if len(data) < MIN_SAMPLE:
        return None
    q1, median, q3 = statistics.quantiles(data, n=4, method="inclusive")
    return Range(_round(q1), _round(median), _round(q3), len(data))


def _round(value: float) -> int:
    """До сотни: точнее вилки площадок всё равно не скажут."""
    return int(round(value / 100) * 100) or 100


def direction_key(query: str) -> tuple[frozenset[str], str]:
    """Чем узнавать направление в названии вакансии: семейства ролей, иначе — сам текст."""
    found = frozenset(families(query))
    fams = found - _GENERIC or found
    return fams, "" if fams else query.lower().replace("ё", "е").strip()


class Market:
    def __init__(self, points: list[Point], built_at: datetime) -> None:
        self.points = points
        self.built_at = built_at

    @classmethod
    def from_vacancies(cls, vacancies: Iterable[Vacancy], built_at: datetime) -> Market:
        points = [p for v in vacancies if (p := to_point(v))]
        return cls(points, built_at)

    def select(self, query: str, country: str) -> Iterator[Point]:
        fams, text = direction_key(query)
        for p in self.points:
            if p.country != country:
                continue
            if (fams and fams <= p.families) or (text and text in p.title):
                yield p

    def by_level(self, query: str, country: str, levels: Iterable[str]) -> dict[str, Range]:
        """Вилки по грейдам. Вакансия «Middle/Senior» считается в обоих."""
        wanted = [lv for lv in LEVEL_ORDER if lv in set(levels)] or list(LEVEL_ORDER)
        points = list(self.select(query, country))
        out: dict[str, Range] = {}
        for level in wanted:
            rng = salary_range(p.usd for p in points if level in p.levels)
            if rng:
                out[level] = rng
        if not out and (rng := salary_range(p.usd for p in points)):
            out["all"] = rng  # по грейдам мало данных — хотя бы общая вилка
        return out


def load(rows: Iterable[str], built_at: datetime) -> Market:
    """Снимок из JSON вакансий базы. Строки читаются по одной — в памяти только точки."""
    return Market.from_vacancies((Vacancy.from_dict(json.loads(raw)) for raw in rows), built_at)


def country_title(code: str) -> str:
    return COUNTRIES.get(code, code)
