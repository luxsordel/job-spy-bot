"""jobs.ge — основная площадка Грузии (англоязычная версия), разбираем HTML."""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

from jobspybot.models import Vacancy
from jobspybot.sources.base import Source, register
from jobspybot.text import clean_ws, html_to_text, parse_day_month

BASE = "https://jobs.ge"
_ID = re.compile(r"[?&]id=(\d+)")


@register
class JobsGe(Source):
    name = "jobs_ge"
    title = "jobs.ge"
    countries = frozenset({"GE"})

    async def search(self, query: str) -> list[Vacancy]:
        html = await self.http.get_text(
            f"{BASE}/en/", params={"page": 1, "q": query, "cid": 0, "lid": 0, "jid": 0}
        )
        return self.parse_list(html)

    def parse_list(self, html: str) -> list[Vacancy]:
        result: list[Vacancy] = []
        seen: set[str] = set()
        for row in LexborHTMLParser(html).css("#job_list_table tr"):
            link = row.css_first('a[href*="view=jobs"]')
            if not link:
                continue
            m = _ID.search(link.attributes.get("href") or "")
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            cells = row.css("td")
            company = ""
            for client in row.css('a[href*="view=client"]'):
                logo = client.css_first("img")
                company = clean_ws(client.text()) or (
                    logo.attributes.get("alt") or "" if logo else ""
                )
                if company:
                    break
            published = clean_ws(cells[4].text()) if len(cells) > 4 else ""
            result.append(
                Vacancy(
                    source=self.name,
                    external_id=m.group(1),
                    title=clean_ws(link.text()),
                    url=f"{BASE}/en/?view=jobs&id={m.group(1)}",
                    company=company.strip(),
                    location="Georgia",
                    country="GE",
                    published_at=parse_day_month(published),
                )
            )
        return result

    async def fetch_description(self, vacancy: Vacancy) -> str:
        html = await self.http.get_text(vacancy.url)
        node = LexborHTMLParser(html).css_first("#job table.dtable")
        return html_to_text(node.html) if node else vacancy.snippet
