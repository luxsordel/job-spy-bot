"""Hirify — русскоязычный агрегатор: вакансии с карьерных сайтов компаний и из Telegram-каналов.

Публичного API для партнёров нет, берём ту же выдачу, что видит сайт. Бережно: одна страница
на запрос раз в цикл, общая для всех пользователей; ссылки ведут на hirify.me.
"""

from __future__ import annotations

from typing import Any

from jobspybot.models import Level, Vacancy, normalize_currency
from jobspybot.sources.base import Source, register
from jobspybot.text import html_to_text, parse_iso

API = "https://api.hirify.me/api"
SITE = "https://hirify.me"

#: страны бота; остальные остаются названием (`Romania`) — фильтр стран их отрежет
_ISO = {"belarus": "BY", "russia": "RU", "kazakhstan": "KZ", "georgia": "GE", "poland": "PL"}
#: множитель к месячной зарплате
_PERIOD = {"year": 1 / 12, "month": 1, "week": 4.33, "day": 21, "hour": 168}
_FORMATS = {"remote": "remote", "hybrid": "hybrid", "onsite": "office", "office": "office"}
#: компанию скрывают за плейсхолдером в премиум-вакансиях
_HIDDEN_COMPANY = "%hirify"
_LEVELS = frozenset(lv.value for lv in Level)


def _country_name(code: str) -> str:
    return code.replace("_", " ").title()


@register
class Hirify(Source):
    name = "hirify"
    title = "Hirify"
    countries = frozenset({"REMOTE", "RU", "BY", "KZ", "GE", "PL"})

    async def search(self, query: str) -> list[Vacancy]:
        data = await self.http.get_json(
            f"{API}/vacancies", params={"search": query, "sort_by": "date", "page": 1}
        )
        return [self.parse_item(x) for x in data.get("data") or [] if x.get("id")]

    def parse_item(self, item: dict[str, Any]) -> Vacancy:
        formats = tuple(
            dict.fromkeys(_FORMATS[f] for f in item.get("work_format") or [] if f in _FORMATS)
        )
        remote = "remote" in formats
        eligible = [c for c in item.get("eligible_countries") or [] if isinstance(c, str)]
        excluded = [c for c in item.get("ineligible_countries") or [] if isinstance(c, str)]
        region_codes = [r.get("code", "") for r in item.get("regions") or [] if r.get("code")]
        places = eligible or region_codes
        own = [_ISO[c] for c in places if c in _ISO]
        if remote and not (len(places) == 1 and own):
            # удалёнка по миру или по нескольким странам
            country = "REMOTE"
        elif own:
            country = own[0]
        else:
            country = _country_name(places[0]) if places else ""

        if item.get("location_scope") == "anywhere":
            regions: tuple[str, ...] = ("Worldwide",)
        else:
            regions = tuple(_country_name(c) for c in eligible)
        if remote:
            regions += tuple(f"except {_country_name(c).lower()}" for c in excluded)
        else:
            regions = ()

        cities = [c.get("name") or c.get("name_en") or "" for c in item.get("cities") or []]
        location = ", ".join(filter(None, cities[:3])) or ", ".join(
            r.get("name") or "" for r in item.get("regions") or []
        )

        salary = item.get("salary") or {}
        k = _PERIOD.get(salary.get("salary_period") or "month", 1)
        lo, hi = salary.get("min"), salary.get("max")
        grades = [g.get("name") for g in item.get("grades") or []]
        # несколько грейдов («middle/senior») — уровень не задаём, иначе отсев отрежет лишнее
        level = Level(grades[0]) if len(grades) == 1 and grades[0] in _LEVELS else None
        company = (item.get("company_title") or "").strip()
        tldr = (item.get("tldr") or "").strip()
        return Vacancy(
            source=self.name,
            external_id=str(item["id"]),
            title=(item.get("title") or "").strip(),
            url=f"{SITE}/jobs/{item.get('slug') or item['id']}",
            company="" if company.startswith(_HIDDEN_COMPANY) else company,
            location=location or ("Worldwide" if regions[:1] == ("Worldwide",) else ""),
            country=country,
            remote=remote if formats else None,
            work_formats=formats,
            salary_from=round(lo * k) if lo else None,
            salary_to=round(hi * k) if hi and hi != lo else None,
            currency=normalize_currency(salary.get("currency")) if lo or hi else None,
            level=level,
            skills=tuple(t.get("name", "") for t in item.get("tags") or [] if t.get("name")),
            snippet=tldr[:300],
            published_at=parse_iso(item.get("created_at")),
            regions=regions,
        )

    async def fetch_description(self, vacancy: Vacancy) -> str:
        data = await self.http.get_json(f"{API}/vacancies/{vacancy.external_id}")
        return html_to_text(data.get("text")) or vacancy.snippet
