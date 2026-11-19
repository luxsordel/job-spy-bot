from __future__ import annotations

import httpx
import pytest

from jobspybot.config import Settings
from jobspybot.models import Level
from jobspybot.sources import SourceUnavailable, build_sources
from jobspybot.sources.hh import HHBelarus
from tests.conftest import fixture_text, make_http


def _page(name: str):
    return lambda r: httpx.Response(200, text=fixture_text(name))


async def test_hh_search_reads_site_page(settings):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=fixture_text("hh_search.html"))

    http = make_http(handler)
    found = await HHBelarus(http, settings).search("python")
    await http.aclose()

    req = seen[0]
    assert req.url.host == "rabota.by" and req.url.path == "/search/vacancy"
    assert req.url.params["area"] == "16"
    assert req.url.params["text"] == "python"
    assert req.url.params["search_period"] == "7"
    assert req.url.params["enable_snippets"] == "true"
    assert "Authorization" not in req.headers

    # дубль с тем же id отброшен
    first, second, third = found
    assert first.uid == "hh_by:101"
    assert first.url == "https://rabota.by/vacancy/101"
    assert first.company == "Пример"
    assert first.location == "Минск" and first.country == "BY"
    assert (first.salary_from, first.salary_to, first.currency) == (1500, 2500, "BYN")
    assert first.level is Level.JUNIOR
    assert first.snippet == "Знание Python и SQL. Писать сервисы на Django..."
    assert first.skills == ("Python", "Django", "PostgreSQL")
    assert first.published_at is not None and first.published_at.day == 19

    assert second.salary_from is None and second.currency is None
    assert second.level is Level.LEAD and second.remote is False

    # почасовая ставка не попадает в месячную зарплату
    assert third.salary_from is None and third.currency is None


async def test_hh_work_formats_and_address(settings):
    http = make_http(_page("hh_search.html"))
    first, second, third = await HHBelarus(http, settings).search("python")
    await http.aclose()
    assert first.work_formats == ("remote", "hybrid") and first.remote
    assert first.address == "Минск, улица Притыцкого, 29 (м. Спортивная, Пушкинская)"
    assert second.work_formats == ("office",) and not second.remote
    assert third.work_formats == () and third.address == ""


@pytest.mark.parametrize(("days", "period"), [(1, "1"), (2, "3"), (7, "7"), (10, "30"), (90, "30")])
async def test_hh_search_period_rounds_up(days, period):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=fixture_text("hh_search.html"))

    http = make_http(handler)
    await HHBelarus(http, Settings(max_vacancy_age_days=days)).search("python")
    await http.aclose()
    assert seen[0].url.params["search_period"] == period


async def test_hh_description_includes_key_skills(settings):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=fixture_text("hh_vacancy.html"))

    http = make_http(handler)
    source = HHBelarus(http, settings)
    text = await source.fetch_description(source.parse_item({"vacancyId": 101, "name": "Dev"}))
    await http.aclose()
    assert str(seen[0].url) == "https://rabota.by/vacancy/101"
    assert text == "Делать сервисы\n\nКлючевые навыки: Python, Django"


async def test_hh_page_without_state_is_unavailable(settings):
    captcha = "<html>Подтвердите, что вы не робот</html>"
    http = make_http(lambda r: httpx.Response(200, text=captcha))
    with pytest.raises(SourceUnavailable):
        await HHBelarus(http, settings).search("python")
    await http.aclose()


async def test_hh_forbidden_raises_source_unavailable(settings):
    http = make_http(lambda r: httpx.Response(403, text="Forbidden"))
    with pytest.raises(SourceUnavailable):
        await HHBelarus(http, settings).search("python")
    await http.aclose()


def test_hh_needs_no_token():
    http = make_http(lambda r: httpx.Response(200))
    sources = build_sources(http, Settings())
    assert {"hh_by", "hh_ru", "hh_kz"} <= set(sources)
