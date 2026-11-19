"""Habr Карьера — JSON-эндпоинт фронтенда career.habr.com."""

from __future__ import annotations

from typing import Any

from selectolax.lexbor import LexborHTMLParser

from jobspybot.models import Level, Vacancy, normalize_currency
from jobspybot.sources.base import Source, register
from jobspybot.text import html_to_text, parse_iso

BASE = "https://career.habr.com"

_LEVELS = {
    "intern": Level.INTERN,
    "junior": Level.JUNIOR,
    "middle": Level.MIDDLE,
    "senior": Level.SENIOR,
    "lead": Level.LEAD,
}


@register
class Habr(Source):
    name = "habr"
    title = "Habr Карьера"
    countries = frozenset({"RU"})
    page_size = 25  # фиксировано на стороне сайта

    async def search(self, query: str) -> list[Vacancy]:
        data = await self.http.get_json(
            f"{BASE}/api/frontend/vacancies",
            params={"q": query, "sort": "date", "type": "all", "page": 1},
        )
        return [self.parse_item(item) for item in data.get("list", [])]

    def parse_item(self, item: dict[str, Any]) -> Vacancy:
        salary = item.get("salary") or {}
        company = item.get("company") or {}
        locations = [loc.get("title", "") for loc in item.get("locations") or []]
        qualification = (item.get("qualification") or "").lower()
        return Vacancy(
            source=self.name,
            external_id=str(item["id"]),
            title=item.get("title", "").strip(),
            url=BASE + item.get("href", f"/vacancies/{item['id']}"),
            company=company.get("title", ""),
            location=", ".join(filter(None, locations)),
            country="RU" if locations else "",
            remote=bool(item.get("remoteWork")),
            salary_from=salary.get("from"),
            salary_to=salary.get("to"),
            currency=normalize_currency(salary.get("currency")),
            level=_LEVELS.get(qualification.split()[0]) if qualification else None,
            skills=tuple(s["title"] for s in item.get("skills") or [] if s.get("title")),
            snippet=", ".join(d.get("title", "") for d in item.get("divisions") or []),
            published_at=parse_iso((item.get("publishedDate") or {}).get("date")),
        )

    async def fetch_description(self, vacancy: Vacancy) -> str:
        html = await self.http.get_text(vacancy.url)
        node = LexborHTMLParser(html).css_first(".vacancy-description__text")
        return html_to_text(node.html) if node else vacancy.snippet
