from __future__ import annotations

import httpx
import pytest

from jobspybot.config import Settings
from jobspybot.models import Level
from jobspybot.sources import SourceUnavailable, build_sources
from jobspybot.sources.hh import HHBelarus
from tests.conftest import fixture_json, make_http


async def test_hh_search_parses_items(settings):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=fixture_json("hh_search.json"))

    http = make_http(handler)
    found = await HHBelarus(http, settings).search("python")
    await http.aclose()

    req = seen[0]
    assert req.url.params["area"] == "16"
    assert req.url.params["host"] == "rabota.by"
    assert req.headers["Authorization"] == "Bearer test-token"

    first, second = found
    assert first.uid == "hh_by:101"
    assert first.currency == "BYN"
    assert first.salary_from == 1500
    assert first.remote is True
    assert first.level is Level.JUNIOR
    assert "Python" in first.snippet and "<" not in first.snippet
    assert first.country == "BY"
    assert second.salary_from is None and second.level is Level.LEAD and second.remote is False


async def test_hh_forbidden_raises_source_unavailable(settings):
    http = make_http(lambda r: httpx.Response(403, json={"errors": [{"type": "forbidden"}]}))
    with pytest.raises(SourceUnavailable):
        await HHBelarus(http, settings).search("python")
    await http.aclose()


async def test_hh_description_includes_key_skills(settings):
    payload = {"description": "<p>Делать <b>сервисы</b></p>", "key_skills": [{"name": "Python"}]}
    http = make_http(lambda r: httpx.Response(200, json=payload))
    vacancy = HHBelarus(http, settings).parse_item(fixture_json("hh_search.json")["items"][0])
    text = await HHBelarus(http, settings).fetch_description(vacancy)
    await http.aclose()
    assert text == "Делать сервисы\n\nКлючевые навыки: Python"


def test_hh_requires_token():
    http = make_http(lambda r: httpx.Response(200))
    assert "hh_by" not in build_sources(http, Settings())
    assert "hh_by" in build_sources(http, Settings(hh_app_token="x"))


async def test_hh_work_formats_and_address(settings):
    http = make_http(lambda r: httpx.Response(200, json=fixture_json("hh_search.json")))
    first, second = await HHBelarus(http, settings).search("python")
    await http.aclose()
    assert first.work_formats == ("remote", "hybrid") and first.remote
    assert first.address == "Минск, улица Притыцкого, 29 (м. Спортивная, Пушкинская)"
    assert second.work_formats == ("office",) and not second.remote
