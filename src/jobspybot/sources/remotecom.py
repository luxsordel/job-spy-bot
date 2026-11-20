"""Remote.com — доска вакансий EOR-платформы Remote, вакансии проверяются до публикации.

Открытого API нет: страница поиска (Next.js) встраивает выдачу в поток RSC
`self.__next_f.push([1, "…"])`, из него и достаём JSON со списком вакансий.
"""

from __future__ import annotations

import json
import re
from typing import Any

from jobspybot.geo import regions_text
from jobspybot.models import Level, Vacancy, normalize_currency
from jobspybot.sources.base import Source, SourceUnavailable, register
from jobspybot.text import html_to_text, parse_iso

BASE = "https://remote.com/jobs"

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_JOBS_KEY = '"jobsData":{"jobs":'
_POSTING = '{"@context":"https://schema.org","@type":"JobPosting"'

_LEVELS = {
    "entry_level": Level.JUNIOR,
    "junior": Level.JUNIOR,
    "mid_level": Level.MIDDLE,
    "senior": Level.SENIOR,
    "lead": Level.LEAD,
    "manager": Level.LEAD,
    "director": Level.LEAD,
    "executive": Level.LEAD,
}
#: множитель к месячной зарплате
_PERIOD = {"yearly": 1 / 12, "annually": 1 / 12, "monthly": 1, "weekly": 4.33, "hourly": 168}
_FORMATS = {"remote": "remote", "hybrid": "hybrid", "onsite": "office", "on_site": "office"}


def rsc_payload(html: str) -> str:
    return "".join(json.loads(m.group(1)) for m in _PUSH.finditer(html))


def rsc_jobs(html: str) -> list[dict[str, Any]]:
    """Список вакансий из потока RSC страницы поиска."""
    payload = rsc_payload(html)
    start = payload.find(_JOBS_KEY)
    if start < 0:
        raise SourceUnavailable("Remote.com: в странице нет списка вакансий — сменилась вёрстка")
    jobs, _ = json.JSONDecoder().raw_decode(payload, start + len(_JOBS_KEY))
    return [j for j in jobs if isinstance(j, dict)]


@register
class RemoteCom(Source):
    name = "remotecom"
    title = "Remote.com"
    countries = frozenset({"REMOTE"})

    async def search(self, query: str) -> list[Vacancy]:
        html = await self.http.get_text(f"{BASE}/all", params={"query": query})
        jobs = [self.parse_item(j) for j in rsc_jobs(html) if j.get("status") == "published"]
        return jobs[: self.page_size]

    def parse_item(self, item: dict[str, Any]) -> Vacancy:
        company = item.get("companyProfile") or {}
        hiring = item.get("hiringLocation") or {}
        places = [
            loc.get("value") or {}
            for loc in hiring.get("includedLocations") or []
            if isinstance(loc, dict)
        ]
        names = tuple(p.get("name", "") for p in places if p.get("name"))
        regions = ("Worldwide",) if hiring.get("type") == "global" else names
        fmt = _FORMATS.get(((item.get("workplaceLocation") or {}).get("type") or "").lower())
        # офис и гибрид привязаны к стране, удалёнка — «из любой страны» с ограничениями
        codes = [p.get("alpha2Code") for p in places if p.get("alpha2Code")]
        country = "REMOTE" if fmt == "remote" or not codes else codes[0]
        pay = item.get("compensation") or {}
        k = _PERIOD.get((pay.get("frequency") or "yearly").lower(), 1 / 12)
        # суммы приходят в центах
        lo, hi = pay.get("minimum"), pay.get("maximum")
        levels = [_LEVELS[s] for s in item.get("seniority") or [] if s in _LEVELS]
        return Vacancy(
            source=self.name,
            external_id=item.get("slug") or "",
            title=(item.get("title") or "").strip(),
            url=f"{BASE}/{company.get('slug', 'company')}/{item.get('slug', '')}",
            company=(company.get("name") or "").strip(),
            location=regions_text(names) or ("Worldwide" if regions else ""),
            country=country,
            remote=fmt == "remote" if fmt else None,
            work_formats=(fmt,) if fmt else (),
            salary_from=round(lo / 100 * k) if lo else None,
            salary_to=round(hi / 100 * k) if hi and hi != lo else None,
            currency=normalize_currency((pay.get("currency") or {}).get("code")),
            level=levels[0] if levels else None,
            published_at=parse_iso(item.get("publishedAt")),
            regions=regions if fmt == "remote" else (),
        )

    async def fetch_description(self, vacancy: Vacancy) -> str:
        # на странице вакансии есть разметка schema.org JobPosting с полным описанием
        payload = rsc_payload(await self.http.get_text(vacancy.url))
        start = payload.find(_POSTING)
        if start < 0:
            return vacancy.snippet
        posting, _ = json.JSONDecoder().raw_decode(payload, start)
        return html_to_text(posting.get("description")) or vacancy.snippet
