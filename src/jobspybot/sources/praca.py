"""praca.by — крупная белорусская площадка без публичного API, разбираем HTML."""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser, LexborNode

from jobspybot.models import Level, Vacancy
from jobspybot.sources.base import Source, register
from jobspybot.text import clean_ws, html_to_text, parse_day_month

BASE = "https://praca.by"
_ID = re.compile(r"/vacancy/(\d+)")


def _text(node: LexborNode | None) -> str:
    return clean_ws(node.text()) if node else ""


def _level(experience: str) -> Level | None:
    low = experience.lower()
    if "не имеет значения" in low or "без опыта" in low:
        return Level.JUNIOR
    m = re.search(r"(\d+)", low)
    if not m:
        return None
    years = int(m.group(1))
    return Level.MIDDLE if years < 3 else Level.SENIOR if years < 6 else Level.LEAD


@register
class PracaBy(Source):
    name = "praca_by"
    title = "praca.by"
    countries = frozenset({"BY"})

    async def search(self, query: str) -> list[Vacancy]:
        html = await self.http.get_text(
            f"{BASE}/search/vacancies/", params={"search[query]": query, "upped_period": 7}
        )
        return self.parse_list(html)

    def parse_list(self, html: str) -> list[Vacancy]:
        result: list[Vacancy] = []
        for card in LexborHTMLParser(html).css("li.vac-small"):
            link = card.css_first("a.vac-small__title-link")
            if not link:
                continue
            href = link.attributes.get("href") or ""
            m = _ID.search(href)
            if not m:
                continue
            experience = _text(card.css_first(".vac-small__experience"))
            city = _text(card.css_first(".vac-small__city"))
            result.append(
                Vacancy(
                    source=self.name,
                    external_id=m.group(1),
                    title=_text(link),
                    url=f"{BASE}/vacancy/{m.group(1)}/",
                    company=_text(card.css_first(".vac-small__organization")),
                    location=city,
                    country="BY",
                    remote="удал" in city.lower() or None,
                    level=_level(experience),
                    snippet=_text(card.css_first(".vacancy__search-description")),
                    published_at=parse_day_month(_text(card.css_first(".vac-small__upped-time"))),
                )
            )
        return result

    async def fetch_description(self, vacancy: Vacancy) -> str:
        html = await self.http.get_text(vacancy.url)
        node = LexborHTMLParser(html).css_first(".vacancy__description .description, .description")
        return html_to_text(node.html) if node else vacancy.snippet
