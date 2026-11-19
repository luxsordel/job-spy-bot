"""Профиль соискателя: заполняется ИИ из резюме, недостающее — ответами на вопросы.

Профиль — источник правды для анкеты поиска: из него выводятся направления,
уровни, страны, удалёнка и зарплата (`apply_to_filters`). Его же видит ИИ при
оценке вакансий и написании писем (`context_line`).
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from jobspybot.filters import COUNTRIES, UserFilters
from jobspybot.limits import (
    MAX_CITY_LEN,
    MAX_WISHES_LEN,
    clean_list,
    clean_text,
    safe_salary,
)
from jobspybot.models import Level

WORK_FORMATS = {"remote": "🏠 Удалёнка", "hybrid": "🔀 Гибрид", "office": "🏢 Офис"}
#: страны для релокации (без «удалёнки из любой страны»)
RELOCATION_COUNTRIES = {k: v for k, v in COUNTRIES.items() if k != "REMOTE"}
SALARY_PRESETS = (500, 800, 1200, 1500, 2000, 3000)

#: уровень кандидата → уровни вакансий, которые имеет смысл показывать
LEVEL_RANGE = {
    Level.INTERN.value: [Level.INTERN.value, Level.JUNIOR.value],
    Level.JUNIOR.value: [Level.INTERN.value, Level.JUNIOR.value],
    Level.MIDDLE.value: [Level.JUNIOR.value, Level.MIDDLE.value],
    Level.SENIOR.value: [Level.MIDDLE.value, Level.SENIOR.value],
    Level.LEAD.value: [Level.SENIOR.value, Level.LEAD.value],
}

LEVEL_ORDER = [lv.value for lv in Level]

#: порядок вопросов анкеты
QUESTIONS = ("directions", "level", "work_formats", "relocation", "salary")


@dataclass(slots=True)
class UserProfile:
    name: str = ""
    headline: str = ""  # роль одной строкой
    level: str = ""  # уровень кандидата по резюме
    #: грейды вакансий, которые показывать (выбирает человек, можно несколько)
    levels: list[str] = field(default_factory=list)
    experience_years: float | None = None
    skills: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    #: образование: «БГУИР, информатика, 2024» или «БНТУ, экономика, не окончено»
    education: list[str] = field(default_factory=list)
    city: str = ""
    country: str = ""  # где живёт, ISO alpha-2
    citizenship: str = "BY"
    work_formats: list[str] = field(default_factory=list)
    relocation: bool | None = None
    relocation_countries: list[str] = field(default_factory=list)
    salary_usd: int | None = None  # 0 — не важно, None — неизвестно
    directions: list[str] = field(default_factory=list)  # поисковые запросы из резюме
    #: офис / гибрид — только в своём городе
    only_home_city: bool = False
    #: скрывать вакансии без указанного формата
    strict_format: bool = False
    #: свободные пожелания: район, метро, график… — учитывает ИИ
    wishes: str = ""
    answered: list[str] = field(default_factory=list)  # вопросы, на которые ответил человек
    #: ручные правки анкеты в ⚙️ Фильтрах поверх выведенного из профиля:
    #: поле фильтра → [значение из профиля на момент правки, своё значение]
    overrides: dict[str, list[Any]] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | None) -> UserProfile:
        if not raw:
            return cls()
        data = json.loads(raw)
        known = {f.name for f in fields(cls)}
        profile = cls(**{k: v for k, v in data.items() if k in known})
        # старые записи могли сохранить мусор (огромную зарплату) — чиним при чтении
        # и спрашиваем зарплату заново: «отвечено» без суммы быть не может («не важно» — 0)
        profile.salary_usd = safe_salary(profile.salary_usd)
        if profile.salary_usd is None:
            profile.answered = [q for q in profile.answered if q != "salary"]
        profile.education = clean_list(profile.education or [], max_items=4, max_len=120)
        profile.levels = [lv for lv in LEVEL_ORDER if lv in (profile.levels or [])]
        if not isinstance(profile.overrides, dict):
            profile.overrides = {}
        return profile

    @property
    def is_empty(self) -> bool:
        return not (self.headline or self.skills or self.level or self.levels)

    def target_levels(self) -> list[str]:
        """Грейды вакансий: выбранные человеком или выведенные из уровня по резюме."""
        if self.levels or "levels" in self.answered:
            return list(self.levels)
        return list(LEVEL_RANGE.get(self.level, []))

    def set_levels(self, levels: list[str]) -> None:
        """Выбор человека; пустой список — любой уровень."""
        self.levels = [lv for lv in LEVEL_ORDER if lv in levels]
        self.mark("levels")
        self.mark("level")

    def toggle_level(self, level: str) -> None:
        current = self.target_levels()
        self.set_levels(
            [x for x in current if x != level] if level in current else [*current, level]
        )

    def mark(self, question: str) -> None:
        if question not in self.answered:
            self.answered.append(question)

    # --- слияние с данными из резюме -----------------------------------------

    def merge_extracted(self, data: dict[str, Any]) -> list[str]:
        """Заполняет пустые поля данными ИИ. Ответы человека не перезаписываются."""
        changed: list[str] = []

        def text(key: str, limit: int) -> str:
            value = data.get(key)
            return str(value).strip()[:limit] if value else ""

        def str_list(key: str, limit: int, item_len: int = 40) -> list[str]:
            items = data.get(key) or []
            if not isinstance(items, list):
                return []
            return [str(x).strip()[:item_len] for x in items if str(x).strip()][:limit]

        for attr, key, limit in (
            ("name", "name", 60),
            ("headline", "headline", 80),
            ("city", "city", 40),
        ):
            if not getattr(self, attr) and (value := text(key, limit)):
                setattr(self, attr, value)
                changed.append(attr)

        country = text("country", 2).upper()
        if not self.country and country in RELOCATION_COUNTRIES:
            self.country = country
            changed.append("country")
        citizenship = text("citizenship", 2).upper()
        if citizenship and len(citizenship) == 2 and "citizenship" not in self.answered:
            self.citizenship = citizenship

        level = text("level", 10).lower()
        if "level" not in self.answered and level in LEVEL_RANGE and not self.level:
            self.level = level
            changed.append("level")

        try:
            years = float(data.get("experience_years"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            years = None
        if self.experience_years is None and years is not None and 0 <= years < 60:
            self.experience_years = round(years, 1)
            changed.append("experience_years")

        for attr, key, limit in (("skills", "skills", 40), ("languages", "languages", 8)):
            current = getattr(self, attr)
            known = {s.lower() for s in current}
            added = [s for s in str_list(key, limit) if s.lower() not in known]
            if added:
                setattr(self, attr, [*current, *added][:limit])
                changed.append(attr)

        education = str_list("education", 4, item_len=120)
        if education and not self.education:
            self.education = education
            changed.append("education")

        formats = [f for f in str_list("work_formats", 3) if f in WORK_FORMATS]
        if formats and not self.work_formats and "work_formats" not in self.answered:
            self.work_formats = formats
            changed.append("work_formats")

        relocation = data.get("relocation")
        if isinstance(relocation, bool) and "relocation" not in self.answered:
            if self.relocation is None:
                self.relocation = relocation
                changed.append("relocation")
            countries = [
                c.upper() for c in str_list("relocation_countries", 6) if c.upper() in COUNTRIES
            ]
            if relocation and countries and not self.relocation_countries:
                self.relocation_countries = countries

        salary = safe_salary(data.get("salary_usd"))
        if salary and self.salary_usd is None and "salary" not in self.answered:
            self.salary_usd = salary
            changed.append("salary_usd")

        directions = str_list("directions", 4, item_len=50)
        known = {d.lower() for d in self.directions}
        added = [d for d in directions if d.lower() not in known]
        if added:
            self.directions = [*self.directions, *added][:6]
            changed.append("directions")
        return changed

    # --- анкета ---------------------------------------------------------------

    def missing(self, filters: UserFilters) -> list[str]:
        """Вопросы, которые ещё нужно задать (в порядке анкеты)."""
        unknown = {
            "directions": not (filters.queries or self.directions),
            "level": not (self.level or self.levels),
            "work_formats": not self.work_formats,
            "relocation": self.relocation is None,
            "salary": self.salary_usd is None,
        }
        return [q for q in QUESTIONS if unknown[q] and q not in self.answered]

    # --- вывод ----------------------------------------------------------------

    def context_line(self) -> str:
        """Короткая строка для ИИ: обстоятельства кандидата, которых нет в резюме."""
        parts = [f"гражданство {self.citizenship or 'BY'}"]
        if self.city or self.country:
            parts.append(f"живёт: {', '.join(filter(None, [self.city, self.country]))}")
        if self.work_formats:
            parts.append("формат: " + ", ".join(self.work_formats))
        if self.relocation is True:
            where = ", ".join(self.relocation_countries) or "любая страна"
            parts.append(f"готов к релокации ({where})")
        elif self.relocation is False:
            parts.append("без релокации")
        if self.salary_usd:
            parts.append(f"ожидания от {self.salary_usd}$/мес")
        if self.only_home_city and self.city:
            parts.append(f"офис/гибрид только в г. {self.city}")
        if self.wishes:
            parts.append(f"пожелания: {self.wishes}")
        return "; ".join(parts)


def derived_filters(p: UserProfile) -> dict[str, Any]:
    """Поля анкеты поиска, которые следуют из профиля (только известные)."""
    out: dict[str, Any] = {
        "formats": [x for x in p.work_formats if x in WORK_FORMATS],
        "strict_format": p.strict_format,
        "only_home_city": p.only_home_city,
        "home_city": p.city,
        "home_country": p.country or p.citizenship,
    }
    levels = p.target_levels()
    if levels or "levels" in p.answered:
        out["levels"] = levels
    if p.work_formats:
        countries: list[str] = []
        if {"office", "hybrid"} & set(p.work_formats):
            home = p.country or p.citizenship
            if home in COUNTRIES:
                countries.append(home)
        if p.relocation:
            countries += [c for c in p.relocation_countries if c not in countries]
        if "remote" in p.work_formats:
            countries.append("REMOTE")
        out["countries"] = countries
        out["remote_only"] = p.work_formats == ["remote"]
    if p.salary_usd is not None:
        # чуть ниже ожиданий: вилки часто занижены, а торг возможен
        out["min_salary_usd"] = p.salary_usd * 4 // 5
    return out


#: поля, которые можно поменять в ⚙️ Фильтрах поверх профиля
OVERRIDABLE = ("countries", "remote_only", "min_salary_usd")


def apply_to_filters(p: UserProfile, f: UserFilters, *, max_queries: int) -> None:
    """Выводит анкету поиска из профиля.

    Ручная правка в фильтрах (`remember_override`) сохраняется, пока не поменяется то,
    из чего поле выводится: снятая страна не вернётся от правки зарплаты, но вернётся,
    если человек сам изменит релокацию или формат.
    """
    if p.directions and not f.queries:
        f.queries = p.directions[:max_queries]
    for name, auto in derived_filters(p).items():
        saved = p.overrides.get(name)
        if saved is not None and saved[0] == auto:
            value = saved[1]
        else:
            p.overrides.pop(name, None)
            value = auto
        setattr(f, name, copy.deepcopy(value))


def remember_override(p: UserProfile, f: UserFilters, name: str) -> None:
    """Запоминает ручную правку поля фильтров, чтобы профиль её не перетёр."""
    auto = derived_filters(p).get(name)
    if name not in OVERRIDABLE or auto is None:
        return
    value = getattr(f, name)
    if value == auto:
        p.overrides.pop(name, None)
    else:
        p.overrides[name] = [auto, copy.deepcopy(value)]


#: какие ключи может вернуть ИИ при разборе фразы «хочу только гибрид…»
PATCH_KEYS = (
    "work_formats",
    "only_home_city",
    "strict_format",
    "city",
    "level",
    "levels",
    "relocation",
    "relocation_countries",
    "salary_usd",
    "wishes",
    "directions_add",
    "directions_remove",
    "exclude_add",
)


def apply_patch(
    p: UserProfile, f: UserFilters, patch: dict[str, Any], *, max_queries: int
) -> list[str]:
    """Применяет правку к профилю и анкете. Возвращает описание изменений для человека."""
    changes: list[str] = []

    def str_list(key: str) -> list[str]:
        value = patch.get(key) or []
        return clean_list(value) if isinstance(value, list) else []

    if "work_formats" in patch:
        formats = [x for x in str_list("work_formats") if x in WORK_FORMATS]
        if formats:
            p.work_formats = formats
            p.mark("work_formats")
            changes.append("формат: " + ", ".join(WORK_FORMATS[x] for x in formats))
    if isinstance(patch.get("city"), str) and patch["city"].strip():
        p.city = clean_text(patch["city"], MAX_CITY_LEN)
        changes.append(f"город: {p.city}")
    if isinstance(patch.get("only_home_city"), bool):
        p.only_home_city = patch["only_home_city"]
        where = f"г. {p.city}" if p.city else "мой город"
        changes.append(f"офис и гибрид только в {where}" if p.only_home_city else "любой город")
    if isinstance(patch.get("strict_format"), bool):
        p.strict_format = patch["strict_format"]
        changes.append(
            "скрывать вакансии без формата" if p.strict_format else "показывать без формата"
        )
    levels = [x.lower() for x in str_list("levels") if x.lower() in LEVEL_RANGE]
    level = str(patch.get("level") or "").lower()
    if not levels and level in LEVEL_RANGE:
        p.level = level
        levels = LEVEL_RANGE[level]
    if levels:
        p.set_levels(levels)
        changes.append("уровни: " + ", ".join(p.levels))
    if isinstance(patch.get("relocation"), bool):
        p.relocation = patch["relocation"]
        p.mark("relocation")
        if not p.relocation:
            p.relocation_countries = []
        changes.append("готов к релокации" if p.relocation else "без релокации")
    countries = [c.upper() for c in str_list("relocation_countries") if c.upper() in COUNTRIES]
    if countries:
        p.relocation, p.relocation_countries = True, countries
        p.mark("relocation")
        changes.append("релокация: " + ", ".join(countries))
    if "salary_usd" in patch:
        salary = safe_salary(patch["salary_usd"])
        if salary is not None:
            p.salary_usd = salary
            p.mark("salary")
            changes.append(f"зарплата от {p.salary_usd}$" if p.salary_usd else "зарплата не важна")
    if isinstance(patch.get("wishes"), str):
        p.wishes = clean_text(patch["wishes"], MAX_WISHES_LEN)
        changes.append(f"пожелания: {p.wishes}" if p.wishes else "пожелания очищены")

    apply_to_filters(p, f, max_queries=max_queries)

    removed = {d.lower() for d in str_list("directions_remove")}
    if removed:
        f.queries = [q for q in f.queries if q.lower() not in removed]
        changes.append("убрал направления: " + ", ".join(sorted(removed)))
    added = [
        d for d in str_list("directions_add") if d.lower() not in {q.lower() for q in f.queries}
    ]
    room = max(0, max_queries - len(f.queries))
    if added[:room]:
        f.queries = [*f.queries, *added[:room]]
        changes.append("добавил направления: " + ", ".join(added[:room]))
    if len(added) > room:
        changes.append(f"не влезло по тарифу: {', '.join(added[room:])}")
    stop = [w for w in str_list("exclude_add") if w.lower() not in {x.lower() for x in f.exclude}]
    if stop:
        f.exclude = clean_list([*f.exclude, *stop])
        changes.append("стоп-слова: " + ", ".join(stop))
    return changes


def preview_patch(
    p: UserProfile, f: UserFilters, patch: dict[str, Any], *, max_queries: int
) -> list[str]:
    """То же, что apply_patch, но без изменения оригиналов — для подтверждения."""
    return apply_patch(copy.deepcopy(p), copy.deepcopy(f), patch, max_queries=max_queries)
