"""Семейство HeadHunter: rabota.by, hh.ru, hh.kz — один API, разные регионы.

API: https://api.hh.ru/openapi/redoc. Анонимные запросы сейчас получают 403,
поэтому нужен токен приложения (`HH_APP_TOKEN`), выданный на https://dev.hh.ru.
"""

from __future__ import annotations

from typing import Any, ClassVar

from jobspybot.config import Settings
from jobspybot.http import HttpError
from jobspybot.models import Level, Vacancy, normalize_currency
from jobspybot.sources.base import Source, SourceUnavailable, register
from jobspybot.text import html_to_text, parse_iso

API = "https://api.hh.ru"

_EXPERIENCE = {
    "noExperience": Level.JUNIOR,
    "between1And3": Level.MIDDLE,
    "between3And6": Level.SENIOR,
    "moreThan6": Level.LEAD,
}


class _HHBase(Source):
    area: ClassVar[int]
    host: ClassVar[str]
    country: ClassVar[str]
    page_size = 100

    @classmethod
    def is_configured(cls, settings: Settings) -> bool:
        return bool(settings.hh_app_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.hh_app_token}",
            "HH-User-Agent": self.settings.user_agent,
        }

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return await self.http.get_json(f"{API}{path}", params=params, headers=self._headers())
        except HttpError as exc:
            if exc.status in (401, 403):
                raise SourceUnavailable(
                    f"{self.title}: доступ запрещён ({exc.status}). Проверьте HH_APP_TOKEN."
                ) from exc
            raise

    async def search(self, query: str) -> list[Vacancy]:
        data = await self._get(
            "/vacancies",
            {
                "text": query,
                "area": self.area,
                "host": self.host,
                "per_page": self.page_size,
                "order_by": "publication_time",
                "period": max(1, self.settings.max_vacancy_age_days),
            },
        )
        return [self.parse_item(item) for item in data.get("items", [])]

    def parse_item(self, item: dict[str, Any]) -> Vacancy:
        salary = item.get("salary") or {}
        employer = item.get("employer") or {}
        area = item.get("area") or {}
        snippet = item.get("snippet") or {}
        exp = (item.get("experience") or {}).get("id")
        work_formats = {f.get("id") for f in item.get("work_format") or []}
        schedule = (item.get("schedule") or {}).get("id")
        remote = "REMOTE" in work_formats or schedule == "remote"
        return Vacancy(
            source=self.name,
            external_id=str(item["id"]),
            title=item.get("name", "").strip(),
            url=item.get("alternate_url") or f"https://{self.host}/vacancy/{item['id']}",
            company=employer.get("name", ""),
            location=area.get("name", ""),
            country=self.country,
            remote=remote,
            salary_from=salary.get("from"),
            salary_to=salary.get("to"),
            currency=normalize_currency(salary.get("currency")),
            level=_EXPERIENCE.get(exp or ""),
            snippet=html_to_text(
                " ".join(filter(None, [snippet.get("requirement"), snippet.get("responsibility")]))
            ),
            published_at=parse_iso(item.get("published_at")),
        )

    async def fetch_description(self, vacancy: Vacancy) -> str:
        data = await self._get(f"/vacancies/{vacancy.external_id}")
        skills = ", ".join(s["name"] for s in data.get("key_skills") or [] if s.get("name"))
        text = html_to_text(data.get("description"))
        return f"{text}\n\nКлючевые навыки: {skills}" if skills else text


@register
class HHBelarus(_HHBase):
    name = "hh_by"
    title = "rabota.by"
    countries = frozenset({"BY"})
    area, host, country = 16, "rabota.by", "BY"


@register
class HHRussia(_HHBase):
    name = "hh_ru"
    title = "hh.ru"
    countries = frozenset({"RU"})
    area, host, country = 113, "hh.ru", "RU"


@register
class HHKazakhstan(_HHBase):
    name = "hh_kz"
    title = "hh.kz"
    countries = frozenset({"KZ"})
    area, host, country = 40, "hh.kz", "KZ"
