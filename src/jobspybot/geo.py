"""Можно ли работать на удалёнке из своей страны.

Мировые площадки пишут, откуда берут исполнителей: «Worldwide», «USA only», «Europe, LATAM».
Явный отказ («только США и Канада») режется бесплатно, до ИИ. Неоднозначные регионы
(«Europe», «EMEA», часовой пояс) решает ИИ — по строке «Кому доступна» в описании вакансии.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: «работаем откуда угодно»
WORLDWIDE = frozenset(
    {
        "worldwide",
        "anywhere",
        "anywhere in the world",
        "global",
        "world",
        "any location",
        "any country",
        "international",
        "весь мир",
        "любая страна",
        "из любой точки мира",
    }
)

#: регионы и страны, в которые входит страна кандидата
INCLUDES: dict[str, frozenset[str]] = {
    "BY": frozenset({"belarus", "беларусь", "cis", "снг", "рб"}),
    "RU": frozenset({"russia", "россия", "рф", "cis", "снг"}),
    "KZ": frozenset({"kazakhstan", "казахстан", "cis", "снг", "central asia"}),
    "GE": frozenset({"georgia", "грузия", "caucasus"}),
    "PL": frozenset(
        {"poland", "польша", "eu", "european union", "eea", "schengen", "europe", "cee"}
    ),
}

#: неоднозначно: страна может входить, а может нет — пусть решает ИИ
AMBIGUOUS = frozenset(
    {
        "europe",
        "eastern europe",
        "emea",
        "cee",
        "cet",
        "eet",
        "европа",
        "восточная европа",
        "utc+3",
        "utc+2",
        "gmt+3",
        "gmt+2",
    }
)

#: точно не страны кандидатов бота (если среди них нет своей — удалёнка не для него)
OTHER = frozenset(
    {
        "us",
        "usa",
        "u.s.",
        "united states",
        "united states of america",
        "america",
        "north america",
        "canada",
        "mexico",
        "latam",
        "latin america",
        "south america",
        "brazil",
        "argentina",
        "colombia",
        "chile",
        "peru",
        "uk",
        "united kingdom",
        "england",
        "ireland",
        "apac",
        "asia pacific",
        "asia",
        "australia",
        "new zealand",
        "india",
        "pakistan",
        "philippines",
        "singapore",
        "japan",
        "china",
        "hong kong",
        "korea",
        "south korea",
        "vietnam",
        "indonesia",
        "malaysia",
        "thailand",
        "africa",
        "south africa",
        "nigeria",
        "kenya",
        "egypt",
        "middle east",
        "uae",
        "saudi arabia",
        "israel",
        "turkey",
        "germany",
        "france",
        "spain",
        "portugal",
        "italy",
        "netherlands",
        "belgium",
        "luxembourg",
        "switzerland",
        "austria",
        "sweden",
        "norway",
        "denmark",
        "finland",
        "iceland",
        "estonia",
        "latvia",
        "lithuania",
        "czech republic",
        "czechia",
        "slovakia",
        "hungary",
        "romania",
        "bulgaria",
        "greece",
        "croatia",
        "slovenia",
        "serbia",
        "cyprus",
        "malta",
        "ukraine",
        "moldova",
        "poland",
        "georgia",
        "kazakhstan",
        "armenia",
        "azerbaijan",
        "uzbekistan",
        "eu",
        "european union",
        "eea",
        "schengen",
        "dach",
        "nordics",
        "benelux",
        "americas",
        "usa timezones",
        "us timezones",
        "est",
        "pst",
        "cst",
        "mst",
    }
)

#: столько стран в списке — уже явный перечень, а не случайное слово
LONG_LIST = 5

#: префикс исключённой страны в `Vacancy.regions`
EXCEPT = "except "

_SPLIT = re.compile(r"\s*(?:[,;/|+&]|\band\b|\bor\b|\bи\b|\bили\b)\s*")
_NOISE = re.compile(
    r"\b(only|remote|based|residents?|citizens?|time ?zones?|timezone|"
    r"approved states|locations?|countries|"
    r"только|удал[её]нно)\b|[()\[\]*.!:]"
)


def _tokens(regions: Iterable[str]) -> list[str]:
    out: list[str] = []
    for raw in regions:
        for part in _SPLIT.split(raw.lower().replace("ё", "е")):
            cleaned = " ".join(_NOISE.sub(" ", part).replace("-", " ").split())
            if cleaned:
                out.append(cleaned)
    return out


def eligible(regions: Iterable[str], countries: Iterable[str]) -> bool | None:
    """Можно ли работать из одной из `countries` (ISO-коды кандидата).

    True — можно (весь мир или страна названа), False — регионы названы и ни один не
    подходит или страна исключена («except belarus»), None — неясно (пусто, «Europe»,
    неизвестный регион).
    """
    tokens = _tokens(regions)
    if not tokens:
        return None
    mine = frozenset().union(*(INCLUDES.get(c, frozenset()) for c in countries))
    # «except belarus» — площадка прямо исключает страну
    if any(t.removeprefix(EXCEPT) in mine for t in tokens if t.startswith(EXCEPT)):
        return False
    tokens = [t for t in tokens if not t.startswith(EXCEPT)]
    if not tokens:
        return None
    ambiguous = unknown = False
    for token in tokens:
        if token in WORLDWIDE or token in mine:
            return True
        if token in AMBIGUOUS:
            ambiguous = True
        elif token not in OTHER:
            unknown = True
    if ambiguous:
        return None
    # длинный список стран без своей — явное «не для вас», даже если какие-то страны незнакомы
    return False if not unknown or len(tokens) >= LONG_LIST else None


def regions_text(regions: Iterable[str], limit: int = 60) -> str:
    text = ", ".join(r.strip() for r in regions if r.strip())
    return text if len(text) <= limit else text[: limit - 1].rstrip(", ") + "…"
