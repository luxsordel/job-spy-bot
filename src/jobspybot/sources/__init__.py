"""Площадки вакансий. Импорт модуля регистрирует площадку в `REGISTRY`.

Порядок в списке у пользователя задаётся `SOURCE_ORDER`.
"""

from jobspybot.sources import (  # noqa: F401
    getmatch,
    habr,
    hh,
    jobsge,
    justjoin,
    nofluffjobs,
    praca,
    superjob,
)
from jobspybot.sources.base import REGISTRY, Source, SourceUnavailable, build_sources

#: Порядок показа площадок пользователю: сначала Беларусь, затем RU, KZ, GE, PL
SOURCE_ORDER = (
    "hh_by",
    "praca_by",
    "hh_ru",
    "habr",
    "getmatch",
    "superjob",
    "hh_kz",
    "jobs_ge",
    "justjoin",
    "nofluffjobs",
)
assert set(SOURCE_ORDER) == set(REGISTRY), "SOURCE_ORDER не совпадает с REGISTRY"

__all__ = ["REGISTRY", "SOURCE_ORDER", "Source", "SourceUnavailable", "build_sources"]
