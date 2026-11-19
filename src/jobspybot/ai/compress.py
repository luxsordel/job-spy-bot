"""Сжатие описания вакансии перед отправкой в модель — главная экономия токенов.

Из описания выкидываются «вода» (о компании, плюшки, бонусы), дубли и пустые
строки, затем текст обрезается до лимита. Строки про релокацию, визы,
гражданство и удалёнку сохраняются всегда: они важны для кандидата из Беларуси.
"""

from __future__ import annotations

import re

# применяется только к коротким строкам-заголовкам, поэтому без привязки к началу строки
_BOILERPLATE = re.compile(
    r"предлага|гарантиру|услови|преимуществ|бонус|плюшк|льгот|компенсац|соцпакет"
    r"|о (компании|нас|проекте|команде)|почему (мы|стоит|у нас)|кто мы|мы —|мы -"
    r"|we offer|benefits|perks|about (us|the company|the team)|why (us|join)|who we are"
    r"|oferujemy|o nas|benefity|dlaczego|kim jesteśmy|o firmie",
    re.IGNORECASE,
)
_RELEVANT_HEADING = re.compile(
    r"^\W*(требования|задачи|обязанности|чем (предстоит|нужно) заниматься|что (нужно|предстоит)"
    r"|мы ожидаем|ожидаем|будет плюсом|стек|технологии|requirements|responsibilities|"
    r"what you|you will|your tasks|nice to have|tech stack"
    r"|wymagania|zadania|obowiązki|mile widziane)",
    re.IGNORECASE,
)
_ALWAYS_KEEP = re.compile(
    r"релок|relocat|виз[аыу]|visa|гражданств|citizen|беларус|belarus|резидент|resident"
    r"|удал[её]н|remote|work permit|разрешени[ея] на работу|\bb2b\b|\bип\b",
    re.IGNORECASE,
)
_NO_TEXT = re.compile(r"^[\W_]*$")


def _is_heading(line: str) -> bool:
    return len(line) <= 60 and (line.endswith(":") or line.isupper() or line[:1] in "#*")


def compress_description(text: str, max_chars: int) -> str:
    out: list[str] = []
    seen: set[str] = set()
    skipping = False
    for raw in text.splitlines():
        line = raw.strip(" \t•-–—*·")
        if not line or _NO_TEXT.match(line):
            continue
        if _BOILERPLATE.search(line) and (_is_heading(line) or len(line) < 40):
            skipping = True
            if not _ALWAYS_KEEP.search(line):
                continue
        elif skipping and (
            _RELEVANT_HEADING.search(line) or (_is_heading(line) and not _BOILERPLATE.search(line))
        ):
            skipping = False
        if skipping and not _ALWAYS_KEEP.search(line):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)

    result: list[str] = []
    size = 0
    for line in out:
        if size + len(line) + 1 > max_chars:
            room = max_chars - size
            if room > 80:
                result.append(line[: room - 1] + "…")
            break
        result.append(line)
        size += len(line) + 1
    return "\n".join(result)
