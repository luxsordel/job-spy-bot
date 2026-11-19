"""Единая модель вакансии, к которой приводятся все площадки."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

#: форматы работы: удалённо, гибрид, офис
WORK_FORMAT_VALUES = ("remote", "hybrid", "office")


class Level(StrEnum):
    """Нормализованный уровень опыта."""

    INTERN = "intern"
    JUNIOR = "junior"
    MIDDLE = "middle"
    SENIOR = "senior"
    LEAD = "lead"


# Коды валют приводятся к ISO 4217.
_CURRENCY_ALIASES = {
    "RUR": "RUB",
    "BYR": "BYN",
    "₽": "RUB",
    "$": "USD",
    "€": "EUR",
    "ZŁ": "PLN",
    "ZL": "PLN",
    "₾": "GEL",
    "₸": "KZT",
}


def normalize_currency(code: str | None) -> str | None:
    if not code:
        return None
    upper = code.strip().upper()
    return _CURRENCY_ALIASES.get(upper, upper) or None


_FP_STRIP = re.compile(r"[^0-9a-zа-яё]+")
_FP_NOISE = re.compile(r"\b(ооо|оао|зао|ип|чуп|уп|llc|ltd|inc|sp z o o|gmbh)\b")


def _norm(text: str) -> str:
    text = _FP_STRIP.sub(" ", text.lower().replace("ё", "е"))
    return " ".join(_FP_NOISE.sub(" ", text).split())


@dataclass(slots=True)
class Vacancy:
    source: str
    external_id: str
    title: str
    url: str
    company: str = ""
    location: str = ""
    country: str = ""  # ISO-3166 alpha-2, пусто если неизвестно
    remote: bool | None = None
    salary_from: int | None = None
    salary_to: int | None = None
    currency: str | None = None
    level: Level | None = None
    skills: tuple[str, ...] = field(default_factory=tuple)
    snippet: str = ""
    published_at: datetime | None = None
    #: форматы, которые указала площадка; пусто — неизвестно
    work_formats: tuple[str, ...] = field(default_factory=tuple)
    #: адрес / метро, если площадка их отдаёт
    address: str = ""
    description: str | None = None  # заполняется лениво, только для прошедших отсев

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.external_id}"

    @property
    def fingerprint(self) -> str:
        """Ключ для поиска одной и той же вакансии на разных площадках."""
        return f"{_norm(self.title)}|{_norm(self.company)}"

    def salary_text(self) -> str:
        if self.salary_from is None and self.salary_to is None:
            return ""
        cur = self.currency or ""
        if self.salary_from and self.salary_to:
            return f"{self.salary_from:,}–{self.salary_to:,} {cur}".replace(",", " ").strip()
        if self.salary_from:
            return f"от {self.salary_from:,} {cur}".replace(",", " ").strip()
        return f"до {self.salary_to:,} {cur}".replace(",", " ").strip()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["level"] = self.level.value if self.level else None
        data["skills"] = list(self.skills)
        data["work_formats"] = list(self.work_formats)
        data["published_at"] = self.published_at.isoformat() if self.published_at else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Vacancy:
        data = dict(data)
        data["level"] = Level(data["level"]) if data.get("level") else None
        data["skills"] = tuple(data.get("skills") or ())
        data["work_formats"] = tuple(data.get("work_formats") or ())
        if data.get("published_at"):
            data["published_at"] = datetime.fromisoformat(data["published_at"])
        return cls(**data)
