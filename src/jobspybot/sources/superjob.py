"""SuperJob — официальный API, нужен бесплатный ключ `SUPERJOB_API_KEY`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from jobspybot.config import Settings
from jobspybot.http import HttpError
from jobspybot.models import Level, Vacancy, normalize_currency
from jobspybot.sources.base import Source, SourceUnavailable, register
from jobspybot.text import clean_ws, html_to_text

API = "https://api.superjob.ru/2.0"

# experience.id: 1 — без опыта, 2 — от 1 года, 3 — от 3 лет, 4 — от 6 лет
_EXPERIENCE = {1: Level.JUNIOR, 2: Level.MIDDLE, 3: Level.SENIOR, 4: Level.LEAD}
_REMOTE_PLACE_ID = 2  # place_of_work.id: 1 — у работодателя, 2 — удалённо, 3 — разъездная


@register
class SuperJob(Source):
    name = "superjob"
    title = "SuperJob"
    countries = frozenset({"RU"})

    @classmethod
    def is_configured(cls, settings: Settings) -> bool:
        return bool(settings.superjob_api_key)

    async def search(self, query: str) -> list[Vacancy]:
        try:
            data = await self.http.get_json(
                f"{API}/vacancies/",
                params={
                    "keyword": query,
                    "count": self.page_size,
                    "order_field": "date",
                    "order_direction": "desc",
                    "period": 7,
                },
                headers={"X-Api-App-Id": self.settings.superjob_api_key},
            )
        except HttpError as exc:
            if exc.status in (401, 403):
                raise SourceUnavailable(f"SuperJob: ключ отклонён ({exc.status})") from exc
            raise
        return [self.parse_item(o) for o in data.get("objects", [])]

    def parse_item(self, item: dict[str, Any]) -> Vacancy:
        ts = item.get("date_published")
        place = (item.get("place_of_work") or {}).get("id")
        formats = (
            ("remote",) if place == _REMOTE_PLACE_ID else ("office",) if place in (1, 3) else ()
        )
        return Vacancy(
            source=self.name,
            external_id=str(item["id"]),
            title=(item.get("profession") or "").strip(),
            url=item.get("link", ""),
            company=item.get("firm_name") or "",
            location=(item.get("town") or {}).get("title", ""),
            country="RU",
            remote=place == _REMOTE_PLACE_ID,
            work_formats=formats,
            address=item.get("address") or "",
            salary_from=item.get("payment_from") or None,
            salary_to=item.get("payment_to") or None,
            currency=normalize_currency(item.get("currency")),
            level=_EXPERIENCE.get((item.get("experience") or {}).get("id")),
            snippet=clean_ws(item.get("candidat") or "")[:300],
            published_at=datetime.fromtimestamp(ts, UTC) if ts else None,
            description=html_to_text(item.get("vacancyRichText")) or item.get("candidat"),
        )
