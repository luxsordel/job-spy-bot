"""Ограничения на ввод пользователя: разумные диапазоны чисел и длины строк.

Всё, что приходит из Telegram или от ИИ, проходит через эти функции, прежде чем попасть
в профиль и фильтры. Нереалистичные значения (зарплата в 10^30 $) иначе ломают
арифметику (`OverflowError` при умножении на float) и экраны бота.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import Any

#: потолок зарплаты в $ в месяц — всё выше считаем опечаткой
MAX_SALARY_USD = 50_000
#: элементов в списке (направления, стоп-слова, навыки)
MAX_LIST_ITEMS = 30
#: длина одного элемента списка
MAX_ITEM_LEN = 60
MAX_CITY_LEN = 40
MAX_WISHES_LEN = 300
#: длина фразы для правки профиля словами (уходит в ИИ)
MAX_PHRASE_LEN = 500
#: дней Pro, которые админ может выдать за раз
MAX_GRANT_DAYS = 3650

_SALARY = re.compile(
    r"(?P<int>[0-9]+(?:[   ',.][0-9]{3}(?![0-9]))*)"
    r"(?:[.,](?P<frac>[0-9]{1,2})(?![0-9]))?"
    r"\s*(?P<k>k|к|тыс)?",
    re.IGNORECASE,
)
_INT = re.compile(r"-?[0-9]{1,12}")


class InputError(ValueError):
    """Значение вне разумного диапазона; текст — для пользователя."""


def parse_int(raw: str) -> int | None:
    """Целое из строки ASCII-цифр. «²», «١» и прочие юникод-цифры — None."""
    raw = raw.strip()
    return int(raw) if _INT.fullmatch(raw) else None


def parse_salary(text: str) -> int | None:
    """Первая сумма из текста: «1 500 $», «от 1200», «1.5k», «2 тыс». None — числа нет.

    Нереалистично большая сумма — `InputError`.
    """
    m = _SALARY.search(text)
    if not m:
        return None
    digits = re.sub(r"[^0-9]", "", m["int"])
    if len(digits) > 9:
        raise InputError(f"Слишком большая сумма — максимум {MAX_SALARY_USD:,}$.".replace(",", " "))
    value = float(digits)
    if m["frac"]:
        value += int(m["frac"]) / 10 ** len(m["frac"])
    if m["k"]:
        value *= 1000
    return check_salary(value)


def check_salary(value: Any) -> int:
    """Число из любого источника (ввод, JSON от ИИ) → целые $ в разумных пределах."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InputError("Нужно число в долларах, например 1200.") from exc
    if not math.isfinite(number) or number < 0:
        raise InputError("Нужно число в долларах, например 1200.")
    if number > MAX_SALARY_USD:
        raise InputError(
            f"Слишком большая сумма — максимум {MAX_SALARY_USD:,}$ в месяц.".replace(",", " ")
        )
    return int(number)


def safe_salary(value: Any) -> int | None:
    """Как `check_salary`, но мусор → None (для данных из БД и от ИИ)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return check_salary(value)
    except InputError:
        return None


def clean_list(
    items: Iterable[Any], *, max_items: int = MAX_LIST_ITEMS, max_len: int = MAX_ITEM_LEN
) -> list[str]:
    """Без пустых и повторов (без учёта регистра), с обрезкой длины и числа элементов."""
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = " ".join(str(item).split())[:max_len].strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            result.append(text)
        if len(result) >= max_items:
            break
    return result


def clean_text(text: str, max_len: int) -> str:
    return " ".join(text.split())[:max_len].strip()
