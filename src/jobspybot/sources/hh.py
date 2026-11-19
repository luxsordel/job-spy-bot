"""Семейство HeadHunter: rabota.by, hh.ru, hh.kz — один сайт, разные регионы.

Официальный API (api.hh.ru) без зарегистрированного приложения отвечает `403`,
поэтому читаем обычные страницы сайта. В каждой из них сервер встраивает
начальное состояние фронтенда — JSON в `<template id="HH-Lux-InitialState">`:
в выдаче поиска это `vacancySearchResult.vacancies`, на странице вакансии —
`vacancyView.vacancyFull.vacancy`. Ключ не нужен.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any, ClassVar

from jobspybot.http import HttpError
from jobspybot.models import Level, Vacancy, normalize_currency
from jobspybot.sources.base import Source, SourceUnavailable, register
from jobspybot.text import html_to_text, parse_iso

_STATE = re.compile(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.S)

_FORMATS = {"REMOTE": "remote", "HYBRID": "hybrid", "ON_SITE": "office", "FIELD_WORK": "office"}

_EXPERIENCE = {
    "noExperience": Level.JUNIOR,
    "between1And3": Level.MIDDLE,
    "between3And6": Level.SENIOR,
    "moreThan6": Level.LEAD,
}

#: значения фильтра «за период» в поиске hh, в днях
_PERIODS = (1, 3, 7, 30)


def initial_state(page: str) -> dict[str, Any]:
    """JSON-состояние страницы hh. Без него (капча, заглушка) — площадка недоступна."""
    match = _STATE.search(page)
    if not match:
        raise SourceUnavailable("HeadHunter: на странице нет данных (капча или смена вёрстки)")
    try:
        return json.loads(html.unescape(match.group(1)))
    except ValueError as exc:
        raise SourceUnavailable("HeadHunter: не удалось разобрать данные страницы") from exc


class _HHBase(Source):
    area: ClassVar[int]
    host: ClassVar[str]
    country: ClassVar[str]
    page_size = 100

    async def _page(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            page = await self.http.get_text(f"https://{self.host}{path}", params=params)
        except HttpError as exc:
            if exc.status in (403, 429):
                raise SourceUnavailable(f"{self.title}: доступ ограничен ({exc.status})") from exc
            raise
        return initial_state(page)

    async def search(self, query: str) -> list[Vacancy]:
        days = max(1, self.settings.max_vacancy_age_days)
        state = await self._page(
            "/search/vacancy",
            {
                "text": query,
                "area": self.area,
                "order_by": "publication_time",
                "search_period": next((p for p in _PERIODS if p >= days), _PERIODS[-1]),
                "items_on_page": self.page_size,
                "enable_snippets": "true",
            },
        )
        items = (state.get("vacancySearchResult") or {}).get("vacancies") or []
        found: dict[str, Vacancy] = {}
        for item in items:
            if item.get("vacancyId"):
                vacancy = self.parse_item(item)
                found.setdefault(vacancy.external_id, vacancy)
        return list(found.values())

    def parse_item(self, item: dict[str, Any]) -> Vacancy:
        vid = str(item["vacancyId"])
        pay = item.get("compensation") or {}
        # почасовые и посменные ставки не сравнимы с месячной зарплатой из фильтров
        monthly = pay.get("mode") in (None, "MONTH")
        company = item.get("company") or {}
        snippet = item.get("snippet") or {}
        formats = _formats(item.get("workFormats"))
        if not formats and item.get("@workSchedule") == "remote":
            formats = ("remote",)
        return Vacancy(
            source=self.name,
            external_id=vid,
            title=(item.get("name") or "").strip(),
            url=(item.get("links") or {}).get("desktop") or f"https://{self.host}/vacancy/{vid}",
            company=(company.get("visibleName") or company.get("name") or "").strip(),
            location=(item.get("area") or {}).get("name", ""),
            country=self.country,
            remote="remote" in formats,
            salary_from=pay.get("from") if monthly else None,
            salary_to=pay.get("to") if monthly else None,
            currency=normalize_currency(pay.get("currencyCode")) if monthly else None,
            level=_EXPERIENCE.get(item.get("workExperience") or ""),
            skills=_skills(snippet.get("skill")),
            snippet=html_to_text(" ".join(filter(None, [snippet.get("req"), snippet.get("resp")]))),
            published_at=parse_iso((item.get("publicationTime") or {}).get("$")),
            work_formats=formats,
            address=_address(item.get("address")),
        )

    async def fetch_description(self, vacancy: Vacancy) -> str:
        state = await self._page(f"/vacancy/{vacancy.external_id}")
        data = ((state.get("vacancyView") or {}).get("vacancyFull") or {}).get("vacancy") or {}
        text = html_to_text(data.get("description")) or vacancy.snippet
        skills = ", ".join(s for s in data.get("keySkills") or [] if isinstance(s, str) and s)
        return f"{text}\n\nКлючевые навыки: {skills}" if skills else text


def _formats(raw: Any) -> tuple[str, ...]:
    """`[{"workFormatsElement": ["ON_SITE", "HYBRID"]}]` → ("office", "hybrid")."""
    ids = [i for block in raw or [] for i in (block or {}).get("workFormatsElement") or []]
    return tuple(dict.fromkeys(_FORMATS[i] for i in ids if i in _FORMATS))


def _skills(raw: str | None) -> tuple[str, ...]:
    text = html_to_text(raw or "").rstrip(".… ")
    return tuple(s.strip() for s in text.split(",") if s.strip())


def _address(addr: dict[str, Any] | None) -> str:
    if not addr:
        return ""
    street = addr.get("displayName") or ", ".join(
        filter(None, [addr.get("city"), addr.get("street"), addr.get("building")])
    )
    metro = (addr.get("metroStations") or {}).get("metro") or []
    names = ", ".join(dict.fromkeys((m.get("name") or "").strip() for m in metro if m.get("name")))
    if street and names:
        return f"{street} (м. {names})"
    return street or (f"м. {names}" if names else "")


@register
class HHBelarus(_HHBase):
    name = "hh_by"
    title = "rabota.by"
    countries = frozenset({"BY"})
    area, host, country = 16, "rabota.by", "BY"


@register
class HHRussia(_HHBase):
    name = "hh_ru"
    title = "hh.ru"
    countries = frozenset({"RU"})
    area, host, country = 113, "hh.ru", "RU"


@register
class HHKazakhstan(_HHBase):
    name = "hh_kz"
    title = "hh.kz"
    countries = frozenset({"KZ"})
    area, host, country = 40, "hh.kz", "KZ"
