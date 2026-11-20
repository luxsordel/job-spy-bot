"""Working Nomads — подборка удалёнки по миру, открытая лента JSON без поиска."""

from __future__ import annotations

from typing import Any

from jobspybot.models import Vacancy
from jobspybot.sources.base import FeedSource, register
from jobspybot.text import html_to_text, parse_iso

BASE = "https://www.workingnomads.com"


@register
class WorkingNomads(FeedSource):
    name = "workingnomads"
    title = "Working Nomads"
    countries = frozenset({"REMOTE"})

    async def fetch_feed(self) -> list[Vacancy]:
        data = await self.http.get_json(f"{BASE}/api/exposed_jobs/")
        return [self.parse_item(x) for x in data if isinstance(x, dict) and x.get("url")]

    def parse_item(self, item: dict[str, Any]) -> Vacancy:
        description = html_to_text(item.get("description"))
        location = (item.get("location") or "").strip()
        url = item["url"]
        tags = [t.strip() for t in (item.get("tags") or "").split(",") if t.strip()]
        return Vacancy(
            source=self.name,
            external_id=url.rstrip("/").rsplit("/", 1)[-1],
            title=(item.get("title") or "").strip(),
            url=url,
            company=(item.get("company_name") or "").strip(),
            location=location or "Remote",
            country="REMOTE",
            remote=True,
            work_formats=("remote",),
            skills=tuple(tags),
            snippet=description[:300],
            published_at=parse_iso(item.get("pub_date")),
            regions=(location,) if location else (),
            description=description or None,
        )
