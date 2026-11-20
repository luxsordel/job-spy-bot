"""Плагинная архитектура площадок.

Чтобы добавить площадку: унаследоваться от `Source`, реализовать `search` (и при
необходимости `fetch_description`), пометить класс декоратором `@register`.
"""

from __future__ import annotations

import re
import time
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


#: слова запроса, которые есть почти в каждом названии — по ним одним не ищем
_GENERIC = frozenset(
    {
        "developer",
        "engineer",
        "programmer",
        "specialist",
        "разработчик",
        "программист",
        "инженер",
        "специалист",
        "remote",
        "удаленно",
        "junior",
        "middle",
        "senior",
        "lead",
    }
)
_WORD = re.compile(r"[\w+#]+(?:\.[\w]+)*")


def query_words(query: str) -> list[str]:
    words = _WORD.findall(query.lower().replace("ё", "е"))
    return [w for w in words if w not in _GENERIC] or words


def matches_query(query: str, text: str) -> bool:
    """Все значимые слова запроса есть в тексте (целыми словами)."""
    words = query_words(query)
    hay = " " + " ".join(_WORD.findall(text.lower().replace("ё", "е"))) + " "
    return bool(words) and all(f" {w} " in hay for w in words)


class FeedSource(Source):
    """Площадка без поиска: забираем общую ленту раз в `feed_ttl` и фильтруем у себя.

    Так на площадку уходит один запрос на цикл, сколько бы запросов ни было у пользователей.
    """

    #: как долго лента считается свежей, секунд
    feed_ttl: ClassVar[float] = 20 * 60

    def __init__(self, http: Http, settings: Settings) -> None:
        super().__init__(http, settings)
        self._feed: list[Vacancy] = []
        self._fetched_at = float("-inf")

    @abstractmethod
    async def fetch_feed(self) -> list[Vacancy]:
        """Вся свежая лента площадки."""

    async def feed(self) -> list[Vacancy]:
        if time.monotonic() - self._fetched_at > self.feed_ttl:
            self._feed = await self.fetch_feed()
            self._fetched_at = time.monotonic()
        return self._feed

    async def search(self, query: str) -> list[Vacancy]:
        found = [v for v in await self.feed() if matches_query(query, haystack(v))]
        return found[: self.page_size]


def haystack(v: Vacancy) -> str:
    return " ".join((v.title, " ".join(v.skills)))


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
