"""Первичный отсев — бесплатный и быстрый, до любых обращений к ИИ.

Две ступени:
1. `check` — жёсткие фильтры (возраст, стоп-слова, уровень, страна, зарплата).
2. `local_score` — дешёвая оценка совпадения навыков пользователя с вакансией.
   В ИИ уходят только вакансии с лучшим локальным баллом.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from jobspybot.models import Level, Vacancy

# Примерные курсы к USD — для фильтра «зарплата от» точность до процентов не важна.
FX_TO_USD: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.08,
    "PLN": 0.25,
    "BYN": 0.31,
    "RUB": 0.0125,
    "KZT": 0.002,
    "GEL": 0.37,
    "UAH": 0.024,
    "AMD": 0.0026,
    "RSD": 0.0092,
    "GBP": 1.27,
    "CHF": 1.13,
}

COUNTRIES = {
    "BY": "🇧🇾 Беларусь",
    "RU": "🇷🇺 Россия",
    "KZ": "🇰🇿 Казахстан",
    "GE": "🇬🇪 Грузия",
    "PL": "🇵🇱 Польша",
    "REMOTE": "🌍 Удалёнка из любой страны",
}

LEVEL_TITLES = {
    Level.INTERN: "стажёр",
    Level.JUNIOR: "junior",
    Level.MIDDLE: "middle",
    Level.SENIOR: "senior",
    Level.LEAD: "lead",
}


def to_usd(amount: int | float | None, currency: str | None) -> float | None:
    if not amount or not currency:
        return None
    rate = FX_TO_USD.get(currency.upper())
    return amount * rate if rate else None


@dataclass(slots=True)
class UserFilters:
    queries: list[str] = field(default_factory=list)  # поисковые запросы к площадкам
    include: list[str] = field(default_factory=list)  # хотя бы одно слово в названии
    exclude: list[str] = field(default_factory=list)  # стоп-слова в названии / компании
    min_salary_usd: int = 0
    require_salary: bool = False
    levels: list[str] = field(default_factory=list)  # пусто = любой уровень
    countries: list[str] = field(default_factory=list)  # пусто = любые
    remote_only: bool = False
    sources: list[str] = field(default_factory=list)  # пусто = все доступные
    skills: list[str] = field(default_factory=list)  # для локального скоринга
    min_local_score: int = 25
    ai_threshold: int = 60
    #: допустимые форматы работы (remote / hybrid / office); пусто — любые
    formats: list[str] = field(default_factory=list)
    #: скрывать вакансии, где площадка не указала формат
    strict_format: bool = False
    #: офис и гибрид — только в своём городе (вакансии в стране проживания)
    only_home_city: bool = False
    home_city: str = ""
    home_country: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | None) -> UserFilters:
        if not raw:
            return cls()
        data = json.loads(raw)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@lru_cache(maxsize=2048)
def _term_re(term: str) -> re.Pattern[str]:
    # границы «слова» с учётом c++, c#, .net, 1с
    return re.compile(rf"(?<![\w+#.]){re.escape(term)}(?![\w+#])")


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def contains_term(text: str, term: str) -> bool:
    term = _norm(term.strip())
    return bool(term) and bool(_term_re(term).search(_norm(text)))


def check(
    v: Vacancy, f: UserFilters, *, max_age_days: int = 7, now: datetime | None = None
) -> str | None:
    """`None`, если вакансия проходит отсев, иначе короткая причина отказа."""
    now = now or datetime.now(UTC)
    if v.published_at and v.published_at < now - timedelta(days=max_age_days):
        return "старая"

    title_company = f"{v.title} {v.company}"
    for word in f.exclude:
        if contains_term(title_company, word):
            return f"стоп-слово «{word}»"

    if f.include and not any(contains_term(v.title, w) for w in f.include):
        return "нет обязательных слов в названии"

    if f.levels and v.level and v.level.value not in f.levels:
        return f"уровень {v.level.value}"

    if f.remote_only and v.remote is not True:
        return "не удалёнка"

    if reason := format_reason(v, f):
        return reason

    remote_ok = bool(v.remote) and "REMOTE" in f.countries
    if f.countries and v.country and v.country not in f.countries and not remote_ok:
        return f"страна {v.country}"

    if f.min_salary_usd:
        usd = to_usd(v.salary_to or v.salary_from, v.currency)
        if usd is None:
            if f.require_salary:
                return "не указана зарплата"
        elif usd < f.min_salary_usd:
            return f"зарплата ≈{usd:.0f}$"
    return None


#: русские и латинские написания городов — площадки пишут по-разному
CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "минск": ("минск", "minsk", "mińsk"),
    "гомель": ("гомель", "gomel", "homel"),
    "гродно": ("гродно", "grodno", "hrodna"),
    "брест": ("брест", "brest"),
    "витебск": ("витебск", "vitebsk"),
    "могилев": ("могилев", "mogilev", "mahilyow"),
    "москва": ("москва", "moscow"),
    "санкт-петербург": ("санкт-петербург", "петербург", "спб", "saint petersburg"),
    "варшава": ("варшава", "warszawa", "warsaw"),
    "краков": ("краков", "kraków", "krakow"),
    "вроцлав": ("вроцлав", "wrocław", "wroclaw"),
    "гданьск": ("гданьск", "gdańsk", "gdansk", "trójmiasto"),
    "познань": ("познань", "poznań", "poznan"),
    "лодзь": ("лодзь", "łódź", "lodz"),
    "тбилиси": ("тбилиси", "tbilisi"),
    "батуми": ("батуми", "batumi"),
    "алматы": ("алматы", "almaty", "алма-ата"),
    "астана": ("астана", "astana"),
}


def city_variants(city: str) -> tuple[str, ...]:
    key = _norm(city.strip())
    for canon, variants in CITY_ALIASES.items():
        if key == canon or key in variants:
            return variants
    return (key,) if key else ()


def format_reason(v: Vacancy, f: UserFilters) -> str | None:
    """Проверка формата работы и города. None — подходит."""
    formats = set(v.work_formats) or ({"remote"} if v.remote else set())
    if f.formats:
        if formats and not formats & set(f.formats):
            return "формат " + "/".join(sorted(formats))
        if not formats and f.strict_format:
            return "формат не указан"
    if f.only_home_city and f.home_city and formats != {"remote"}:
        same_country = not v.country or not f.home_country or v.country == f.home_country
        place = _norm(f"{v.location} {v.address}")
        if (
            same_country
            and place.strip()
            and not any(alias in place for alias in city_variants(f.home_city))
        ):
            return f"не в г. {f.home_city}"
    return None


_TOKEN = re.compile(r"[a-zа-яё0-9+#.]+")


def local_score(v: Vacancy, skills: list[str]) -> int:
    """0–100: насколько навыки пользователя встречаются в вакансии. Без навыков — 50."""
    if not skills:
        return 50
    title = v.title.lower()
    body = " ".join([*v.skills, v.snippet, (v.description or "")[:1500]]).lower()
    hits = 0.0
    for skill in skills:
        if contains_term(title, skill):
            hits += 2
        elif contains_term(body, skill):
            hits += 1
    # 3 «сильных» совпадения (или 6 слабых) уже дают 100
    target = min(len(skills), 3) * 2
    return min(100, round(hits * 100 / target))
