"""We Work Remotely — одна из старейших досок удалёнки, общий RSS без поиска."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from jobspybot.models import Vacancy
from jobspybot.sources.base import FeedSource, register
from jobspybot.text import clean_ws, html_to_text

BASE = "https://weworkremotely.com"


@register
class WeWorkRemotely(FeedSource):
    name = "weworkremotely"
    title = "We Work Remotely"
    countries = frozenset({"REMOTE"})

    async def fetch_feed(self) -> list[Vacancy]:
        return self.parse_feed(await self.http.get_text(f"{BASE}/remote-jobs.rss"))

    def parse_feed(self, xml: str) -> list[Vacancy]:
        result: list[Vacancy] = []
        for item in ET.fromstring(xml).iterfind("./channel/item"):
            link = (item.findtext("link") or item.findtext("guid") or "").strip()
            raw_title = clean_ws(item.findtext("title") or "")
            if not link or not raw_title:
                continue
            # заголовок вида «Компания: Должность»
            company, _, title = raw_title.partition(": ")
            if not title:
                company, title = "", raw_title
            region = clean_ws(item.findtext("region") or "")
            skills = clean_ws(item.findtext("skills") or "")
            description = html_to_text(item.findtext("description"))
            try:
                published = parsedate_to_datetime(item.findtext("pubDate") or "")
            except (TypeError, ValueError):
                published = None
            result.append(
                Vacancy(
                    source=self.name,
                    external_id=link.rstrip("/").rsplit("/", 1)[-1],
                    title=title.strip(),
                    url=link,
                    company=company.strip(),
                    location=region or "Remote",
                    country="REMOTE",
                    remote=True,
                    work_formats=("remote",),
                    skills=tuple(s.strip() for s in skills.split(",") if s.strip()),
                    snippet=description[:300],
                    published_at=published,
                    regions=(region,) if region else (),
                    description=description or None,
                )
            )
        return result
