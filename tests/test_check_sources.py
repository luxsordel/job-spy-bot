"""Диагностика площадок: `check-sources` и строгий режим для ночной проверки."""

from __future__ import annotations

import asyncio

from jobspybot import __main__ as cli
from jobspybot.config import Settings
from jobspybot.sources import SourceUnavailable


class _Fake:
    def __init__(self, result):
        self.result = result

    async def search(self, query):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _run(monkeypatch, sources, *, strict):
    monkeypatch.setattr(cli, "build_sources", lambda http, settings: sources)
    settings = Settings.from_env({})
    return asyncio.run(cli.check_sources(settings, "python", strict=strict))


def test_empty_result_fails_only_in_strict_mode(monkeypatch, capsys):
    assert _run(monkeypatch, {"a": _Fake([])}, strict=False) == 0
    assert _run(monkeypatch, {"a": _Fake([])}, strict=True) == 1
    assert "[a] пустая выдача" in capsys.readouterr().out


def test_unavailable_source_fails(monkeypatch, capsys):
    assert _run(monkeypatch, {"a": _Fake(SourceUnavailable("403"))}, strict=False) == 1
    assert "[a] недоступна: 403" in capsys.readouterr().out
