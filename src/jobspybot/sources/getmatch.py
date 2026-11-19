"""GetMatch — IT-вакансии с обязательной вилкой, часто удалёнка / релокация."""

from __future__ import annotations

from typing import Any

from jobspybot.models import Vacancy, normalize_currency
from jobspybot.sources.base import Source, register
from jobspybot.text import html_to_text, parse_iso

BASE = "https://getmatch.ru"

_COUNTRIES = {
    "россия": "RU",
    "беларусь": "BY",
    "казахстан": "KZ",
    "грузия": "GE",
    "польша": "PL",
    "армения": "AM",
    "сербия": "RS",
}


@register
class GetMatch(Source):
    name = "getmatch"
    title = "GetMatch"
    countries = frozenset({"RU", "BY", "KZ", "GE", "AM", "RS"})

    async def search(self, query: str) -> list[Vacancy]:
        data = await self.http.get_json(
            f"{BASE}/api/offers", params={"q": query, "limit": self.page_size, "offset": 0}
        )
        return [self.parse_item(o) for o in data.get("offers", []) if o.get("is_active", True)]

    def parse_item(self, item: dict[str, Any]) -> Vacancy:
        locs = item.get("location_requirements") or []
        formats = {loc.get("format") for loc in locs}
        cities = [loc.get("city") or loc.get("country") or "" for loc in locs]
        country = next((_COUNTRIES.get((loc.get("country") or "").lower(), "") for loc in locs), "")
        skills = tuple(
            s.get("title") or s.get("name") or ""
            for s in item.get("skills_objects") or []
            if isinstance(s, dict)
        )
        description = html_to_text(item.get("offer_description") or item.get("description_html"))
        return Vacancy(
            source=self.name,
            external_id=str(item["id"]),
            title=(item.get("position") or "").strip(),
            url=BASE + (item.get("url") or f"/vacancies/{item['id']}"),
            company=(item.get("company") or {}).get("name", ""),
            location=", ".join(dict.fromkeys(filter(None, cities))),
            country=country,
            remote="remote" in formats,
            salary_from=item.get("salary_display_from"),
            salary_to=item.get("salary_display_to"),
            currency=normalize_currency(item.get("salary_currency")),
            skills=tuple(filter(None, skills)),
            snippet=description[:300],
            published_at=parse_iso(item.get("published_at")),
            description=description or None,
        )
