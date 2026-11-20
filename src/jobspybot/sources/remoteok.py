"""Remote OK — мировая удалёнка, открытый JSON API.

Условия API: ссылка на вакансию ведёт на Remote OK и площадка указана как источник —
так и показываем (`render` пишет название площадки рядом со ссылкой).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from jobspybot.models import Vacancy
from jobspybot.sources.base import FeedSource, register
from jobspybot.text import html_to_text, parse_iso

BASE = "https://remoteok.com"


@register
class RemoteOK(FeedSource):
    name = "remoteok"
    title = "Remote OK"
    countries = frozenset({"REMOTE"})

    async def fetch_feed(self) -> list[Vacancy]:
        data = await self.http.get_json(f"{BASE}/api")
        # первый элемент — юридическое уведомление, не вакансия
        return [self.parse_item(x) for x in data if isinstance(x, dict) and x.get("id")]

    def parse_item(self, item: dict[str, Any]) -> Vacancy:
        description = html_to_text(item.get("description"))
        location = (item.get("location") or "").strip()
        epoch = item.get("epoch")
        # зарплата в API — годовая в USD
        lo, hi = item.get("salary_min") or 0, item.get("salary_max") or 0
        return Vacancy(
            source=self.name,
            external_id=str(item["id"]),
            title=(item.get("position") or "").strip(),
            url=item.get("url") or f"{BASE}/remote-jobs/{item.get('slug') or item['id']}",
            company=(item.get("company") or "").strip(),
            location=location or "Remote",
            country="REMOTE",
            remote=True,
            work_formats=("remote",),
            salary_from=round(lo / 12) if lo else None,
            salary_to=round(hi / 12) if hi and hi != lo else None,
            currency="USD" if lo or hi else None,
            skills=tuple(t for t in item.get("tags") or [] if isinstance(t, str)),
            snippet=description[:300],
            published_at=(
                datetime.fromtimestamp(epoch, UTC) if epoch else parse_iso(item.get("date"))
            ),
            regions=(location,) if location else (),
            description=description or None,
        )
