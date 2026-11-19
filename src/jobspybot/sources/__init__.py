"""Площадки вакансий. Импорт модуля регистрирует площадку в `REGISTRY`."""

from jobspybot.sources import hh  # noqa: F401
from jobspybot.sources.base import REGISTRY, Source, SourceUnavailable, build_sources

__all__ = ["REGISTRY", "Source", "SourceUnavailable", "build_sources"]
