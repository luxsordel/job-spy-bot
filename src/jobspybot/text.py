"""Утилиты для текста: HTML → plain text, разбор дат."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from selectolax.lexbor import LexborHTMLParser as HTMLParser

_WS = re.compile(r"[ \t ]+")
_NL = re.compile(r"\n{3,}")
_BLOCK_TAGS = ("p", "br", "li", "div", "h1", "h2", "h3", "h4", "h5", "tr", "ul", "ol")


def html_to_text(html: str | None) -> str:
    """Превращает HTML в текст с сохранением переносов строк между блоками."""
    if not html:
        return ""
    if "<" not in html:
        return clean_ws(html)
    tree = HTMLParser(html)
    for tag in ("script", "style", "noscript"):
        for node in tree.css(tag):
            node.decompose()
    for node in tree.css(",".join(_BLOCK_TAGS)):
        if node.tag == "li":
            node.insert_before("\n• ")
        else:
            node.insert_before("\n")
    root = tree.body or tree.root
    return clean_ws(root.text(separator="") if root else "")


def clean_ws(text: str) -> str:
    lines = (_WS.sub(" ", line).strip() for line in text.replace("\r", "").split("\n"))
    out = "\n".join(line for line in lines)
    return _NL.sub("\n\n", out).strip()


def parse_iso(value: str | None) -> datetime | None:
    """ISO-8601, включая форматы `+0300` (hh.ru) и `Z`. Возвращает aware datetime."""
    if not value:
        return None
    value = value.strip().replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", value):
        value = f"{value[:-2]}:{value[-2:]}"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


_MONTHS = {
    # ru (родительный падеж)
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    # en
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}  # fmt: skip


def parse_day_month(text: str, now: datetime | None = None) -> datetime | None:
    """«5 ноября», «18 November» → дата без года (берётся текущий, либо прошлый)."""
    now = now or datetime.now(UTC)
    m = re.search(r"(\d{1,2})\s+([A-Za-zА-Яа-яё]+)", text or "")
    if not m:
        low = (text or "").lower()
        if "сегодня" in low or "today" in low:
            return now
        if "вчера" in low or "yesterday" in low:
            return now - timedelta(days=1)
        return None
    month = _MONTHS.get(m.group(2).lower())
    if not month:
        return None
    day = int(m.group(1))
    try:
        dt = datetime(now.year, month, day, tzinfo=UTC)
    except ValueError:
        return None
    if dt > now:
        dt = dt.replace(year=now.year - 1)
    return dt
