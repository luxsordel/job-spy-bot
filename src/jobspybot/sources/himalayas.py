"""Himalayas — мировая удалёнка с явными ограничениями по странам, открытый JSON API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from jobspybot.geo import regions_text
from jobspybot.models import Level, Vacancy, normalize_currency
from jobspybot.sources.base import Source, register
from jobspybot.text import html_to_text

BASE = "https://himalayas.app"

_LEVELS = {
    "entry-level": Level.JUNIOR,
    "junior": Level.JUNIOR,
    "mid-level": Level.MIDDLE,
    "senior": Level.SENIOR,
    "manager": Level.LEAD,
    "director": Level.LEAD,
    "executive": Level.LEAD,
}

#: множитель к месячной зарплате
_PERIOD = {"annual": 1 / 12, "yearly": 1 / 12, "monthly": 1, "weekly": 4.33, "hourly": 168}


@register
class Himalayas(Source):
    name = "himalayas"
    title = "Himalayas"
    countries = frozenset({"REMOTE"})
    page_size = 20  # больше одна страница поиска не отдаёт

    async def search(self, query: str) -> list[Vacancy]:
        data = await self.http.get_json(f"{BASE}/jobs/api/search", params={"q": query})
        jobs = [self.parse_item(x) for x in data.get("jobs") or [] if x.get("guid")]
        oldest = datetime.min.replace(tzinfo=UTC)
        return sorted(jobs, key=lambda v: v.published_at or oldest, reverse=True)

    def parse_item(self, item: dict[str, Any]) -> Vacancy:
        description = html_to_text(item.get("description"))
        regions = tuple(r for r in item.get("locationRestrictions") or [] if isinstance(r, str))
        k = _PERIOD.get((item.get("salaryPeriod") or "annual").lower(), 1 / 12)
        lo, hi = item.get("minSalary"), item.get("maxSalary")
        seniority = [s.lower() for s in item.get("seniority") or [] if isinstance(s, str)]
        level = next((_LEVELS[s] for s in seniority if s in _LEVELS), None)
        pub = item.get("pubDate")
        return Vacancy(
            source=self.name,
            external_id=item["guid"].rstrip("/").rsplit("/", 1)[-1],
            title=(item.get("title") or "").strip(),
            url=item["guid"],
            company=(item.get("companyName") or "").strip(),
            location=regions_text(regions) or "Worldwide",
            country="REMOTE",
            remote=True,
            work_formats=("remote",),
            salary_from=round(lo * k) if lo else None,
            salary_to=round(hi * k) if hi and hi != lo else None,
            currency=normalize_currency(item.get("currency")) if lo or hi else None,
            level=level,
            snippet=(item.get("excerpt") or description)[:300],
            published_at=datetime.fromtimestamp(pub, UTC) if pub else None,
            # пустой список ограничений у Himalayas значит «весь мир»
            regions=regions or ("Worldwide",),
            description=description or None,
        )
