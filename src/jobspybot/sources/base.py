"""Плагинная архитектура площадок.

Чтобы добавить площадку: унаследоваться от `Source`, реализовать `search` (и при
необходимости `fetch_description`), пометить класс декоратором `@register`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from jobspybot.config import Settings
from jobspybot.http import Http
from jobspybot.models import Vacancy

REGISTRY: dict[str, type[Source]] = {}


class SourceUnavailable(Exception):
    """Площадка временно или постоянно недоступна (нет ключа, бан, смена API)."""


class Source(ABC):
    #: Уникальный короткий идентификатор (используется в БД и настройках пользователя)
    name: ClassVar[str]
    #: Человекочитаемое название
    title: ClassVar[str]
    #: Страны, которые покрывает площадка (ISO alpha-2)
    countries: ClassVar[frozenset[str]]
    #: Сколько вакансий запрашивать за один поиск
    page_size: ClassVar[int] = 50

    def __init__(self, http: Http, settings: Settings) -> None:
        self.http = http
        self.settings = settings

    @classmethod
    def is_configured(cls, settings: Settings) -> bool:
        """Есть ли всё необходимое (например, API-ключ) для работы площадки."""
        return True

    @abstractmethod
    async def search(self, query: str) -> list[Vacancy]:
        """Свежие вакансии по запросу, отсортированные от новых к старым."""

    async def fetch_description(self, vacancy: Vacancy) -> str:
        """Полное описание вакансии. Вызывается только для прошедших первичный отсев."""
        return vacancy.description or vacancy.snippet


def register(cls: type[Source]) -> type[Source]:
    if cls.name in REGISTRY:
        raise ValueError(f"Площадка {cls.name!r} уже зарегистрирована")
    REGISTRY[cls.name] = cls
    return cls


def build_sources(http: Http, settings: Settings) -> dict[str, Source]:
    """Экземпляры всех включённых и сконфигурированных площадок."""
    # импорт модулей регистрирует площадки
    from jobspybot import sources as _  # noqa: F401

    result: dict[str, Source] = {}
    for name, cls in REGISTRY.items():
        if settings.enabled_sources and name not in settings.enabled_sources:
            continue
        if not cls.is_configured(settings):
            continue
        result[name] = cls(http, settings)
    return result
