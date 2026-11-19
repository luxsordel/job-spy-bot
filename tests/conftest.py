from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from jobspybot.config import Settings
from jobspybot.db import Database
from jobspybot.http import Http

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str):
    return json.loads(fixture_text(name))


Handler = Callable[[httpx.Request], httpx.Response]


def make_http(handler: Handler) -> Http:
    return Http(
        user_agent="test",
        retries=2,
        min_interval=0,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(hh_app_token="test-token", superjob_api_key="sj-key")


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()
