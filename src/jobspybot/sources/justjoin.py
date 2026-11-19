"""justjoin.it — крупнейшая IT-площадка Польши, JSON API кандидата."""

from __future__ import annotations

from typing import Any

from jobspybot.models import Level, Vacancy, normalize_currency
from jobspybot.sources.base import Source, register
from jobspybot.text import html_to_text, parse_iso

BASE = "https://justjoin.it"

_LEVELS = {
    "junior": Level.JUNIOR,
    "mid": Level.MIDDLE,
    "senior": Level.SENIOR,
    "c_level": Level.LEAD,
    "manager": Level.LEAD,
}


@register
class JustJoin(Source):
    name = "justjoin"
    title = "justjoin.it"
    countries = frozenset({"PL"})

    async def search(self, query: str) -> list[Vacancy]:
        data = await self.http.get_json(
            f"{BASE}/api/candidate-api/offers",
            params={
                # `keyword` сервер молча игнорирует, рабочий параметр — `keywords`
                "keywords": query,
                "itemsCount": self.page_size,
                "sortBy": "publishedAt",
                "orderBy": "descending",
            },
        )
        return [self.parse_item(o) for o in data.get("data", [])]

    def parse_item(self, item: dict[str, Any]) -> Vacancy:
        salary = next(
            (
                e
                for e in item.get("employmentTypes") or []
                if e.get("currencySource") == "original" and (e.get("from") or e.get("to"))
            ),
            {},
        )
        hourly = salary.get("unit") == "hour"
        slug = item.get("slug") or item.get("guid")
        return Vacancy(
            source=self.name,
            external_id=str(slug),
            title=(item.get("title") or "").strip(),
            url=f"{BASE}/job-offer/{slug}",
            company=item.get("companyName", ""),
            location=item.get("city", ""),
            country="PL",
            remote=item.get("workplaceType") == "remote",
            # почасовые ставки приводим к месяцу (≈168 ч), чтобы фильтр по зарплате был честным
            salary_from=_monthly(salary.get("from"), hourly),
            salary_to=_monthly(salary.get("to"), hourly),
            currency=normalize_currency(salary.get("currency")),
            level=_LEVELS.get(item.get("experienceLevel") or ""),
            skills=tuple(s["name"] for s in item.get("requiredSkills") or [] if s.get("name")),
            published_at=parse_iso(item.get("publishedAt")),
        )

    async def fetch_description(self, vacancy: Vacancy) -> str:
        data = await self.http.get_json(f"{BASE}/api/candidate-api/offers/{vacancy.external_id}")
        return html_to_text(data.get("body")) or vacancy.snippet


def _monthly(value: float | None, hourly: bool) -> int | None:
    if not value:
        return None
    return int(value * 168) if hourly else int(value)
