"""Обучение на 👍 / 👎 (#31): контекст для ИИ и подсказки «добавить в стоп-слова?»."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

#: столько последних реакций каждого вида уходит в оценку ИИ
CONTEXT_TITLES = 5
#: слово встретилось в стольких скрытых вакансиях — предлагаем стоп-слово
HINT_MIN_COUNT = 3

_WORD = re.compile(r"[\w+#]+(?:\.[\w+#]+)*", re.UNICODE)

#: общие слова названий вакансий — не годятся в стоп-слова
GENERIC = frozenset(
    [
        "developer",
        "разработчик",
        "разработчица",
        "программист",
        "engineer",
        "инженер",
        "specialist",
        "специалист",
        "junior",
        "middle",
        "senior",
        "lead",
        "младший",
        "старший",
        "ведущий",
        "главный",
        "стажер",
        "стажёр",
        "intern",
        "trainee",
        "team",
        "lead",
        "head",
        "manager",
        "менеджер",
        "руководитель",
        "remote",
        "удаленно",
        "удалённо",
        "офис",
        "гибрид",
        "the",
        "and",
        "for",
        "with",
        "of",
        "in",
        "to",
        "на",
        "по",
        "в",
        "и",
        "с",
        "для",
        "от",
        "без",
        "или",
        "со",
        "из",
    ]
)


def words(title: str) -> set[str]:
    return {
        w
        for w in (m.group().lower() for m in _WORD.finditer(title))
        if len(w) >= 2 and not w.isdigit() and w not in GENERIC
    }


def dislike_hint(
    disliked: Iterable[str],
    liked: Iterable[str],
    exclude: Iterable[str],
    keep: Iterable[str] = (),
    already_hinted: Iterable[str] = (),
) -> tuple[str, int] | None:
    """Слово, которое часто встречается в скрытых вакансиях и ни разу — в понравившихся.

    `keep` — слова из направлений поиска: их не предлагаем, даже если человек скрывал
    вакансии с ними (скрывал «Python + 1С» — значит, не нравится 1С, а не Python).
    """
    counts: Counter[str] = Counter()
    for title in disliked:
        counts.update(words(title))
    banned = {w.lower() for w in exclude} | set(already_hinted)
    for title in [*liked, *keep]:
        banned |= words(title)
    for word, n in counts.most_common():
        if n < HINT_MIN_COUNT:
            break
        if word not in banned and len(word.encode()) <= 40:
            return word, n
    return None


def context(liked: list[str], disliked: list[str]) -> str:
    """Добавка к строке «Кандидат» для оценки ИИ."""
    parts = []
    if liked:
        parts.append("понравились: " + "; ".join(t[:60] for t in liked[:CONTEXT_TITLES]))
    if disliked:
        parts.append("скрыл: " + "; ".join(t[:60] for t in disliked[:CONTEXT_TITLES]))
    return "; ".join(parts)


def vacancies_word(n: int) -> str:
    """«3 вакансии», «5 вакансий», «21 вакансию» (винительный падеж: «скрыли N …»)."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} вакансию"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} вакансии"
    return f"{n} вакансий"
