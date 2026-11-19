"""No Fluff Jobs — польская IT-площадка, всегда с вилкой зарплаты."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from jobspybot.models import Level, Vacancy, normalize_currency
from jobspybot.sources.base import Source, register
from jobspybot.text import html_to_text

BASE = "https://nofluffjobs.com"

_LEVELS = {
    "trainee": Level.INTERN,
    "junior": Level.JUNIOR,
    "mid": Level.MIDDLE,
    "senior": Level.SENIOR,
    "expert": Level.LEAD,
}


@register
class NoFluffJobs(Source):
    name = "nofluffjobs"
    title = "No Fluff Jobs"
    countries = frozenset({"PL"})

    async def search(self, query: str) -> list[Vacancy]:
        data = await self.http.post_json(
            f"{BASE}/api/search/posting",
            params={
                "salaryCurrency": "PLN",
                "salaryPeriod": "month",
                "region": "pl",
                "page": 1,
                "pageSize": self.page_size,
            },
            json={"rawSearch": query, "page": 1},
        )
        # одна вакансия приходит несколько раз — по разу на каждый город
        unique = {p["id"]: p for p in data.get("postings", []) if p.get("id")}
        return [self.parse_item(p) for p in unique.values()][: self.page_size]

    def parse_item(self, item: dict[str, Any]) -> Vacancy:
        location = item.get("location") or {}
        cities = [p.get("city", "") for p in location.get("places") or [] if p.get("city")]
        salary = item.get("salary") or {}
        seniority = [s.lower() for s in item.get("seniority") or []]
        posted = item.get("posted")
        # B2B-ставки бывают почасовыми — приводим к месяцу (≈168 ч)
        k = 168 if (salary.get("period") or "").lower() == "hour" else 1
        return Vacancy(
            source=self.name,
            external_id=str(item["id"]),
            title=(item.get("title") or "").strip(),
            url=f"{BASE}/pl/job/{item.get('url') or item['id']}",
            company=item.get("name", ""),
            location=", ".join(dict.fromkeys(cities)),
            country="PL",
            remote=bool(location.get("fullyRemote")),
            salary_from=int(salary["from"] * k) if salary.get("from") else None,
            salary_to=int(salary["to"] * k) if salary.get("to") else None,
            currency=normalize_currency(salary.get("currency")),
            level=_LEVELS.get(seniority[0]) if seniority else None,
            skills=tuple(filter(None, [item.get("technology")])),
            published_at=datetime.fromtimestamp(posted / 1000, UTC) if posted else None,
        )

    async def fetch_description(self, vacancy: Vacancy) -> str:
        data = await self.http.get_json(f"{BASE}/api/posting/{vacancy.external_id}")
        reqs = data.get("requirements") or {}
        musts = ", ".join(m.get("value", "") for m in reqs.get("musts") or [])
        parts = [
            html_to_text((data.get("details") or {}).get("description")),
            f"Обязательно: {musts}" if musts else "",
            html_to_text(reqs.get("description")),
        ]
        return "\n\n".join(p for p in parts if p) or vacancy.snippet
