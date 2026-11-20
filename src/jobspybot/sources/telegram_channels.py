"""Вакансии из открытых Telegram-каналов — через веб-превью `t.me/s/<канал>`.

Без API и аккаунта: превью отдаёт ~20 последних постов канала. Все каналы читаются
одним проходом раз в `feed_ttl`, запросы пользователей сопоставляются у нас.

Разбор поста бесплатный, без ИИ: название — первая содержательная строка, реклама и курсы
отсеиваются по словам-маркерам. Полный текст поста уходит ИИ при оценке, как описание.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import ClassVar

from selectolax.lexbor import LexborHTMLParser

from jobspybot.config import Settings
from jobspybot.http import HttpError
from jobspybot.models import Vacancy
from jobspybot.sources.base import FeedSource, register
from jobspybot.text import html_to_text, parse_iso

log = logging.getLogger(__name__)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

BASE = "https://t.me"

#: признаки вакансии в тексте поста
_VACANCY = re.compile(
    r"#?ваканси|\bищем\b|\bищут\b|\bтребовани|\bобязанност|\bзарплат|\bзп\b|\bвилка\b|"
    r"\bоклад|\bудал[её]н|\bгибрид|\bофис|\bрелокац|\bотклик|\bрезюме\b|\bопыт работы|"
    r"\bhiring\b|\bvacancy\b|\brequirements\b|\bresponsibilities\b|\bsalary\b|\bremote\b|"
    r"\bhybrid\b|\bonsite\b|\boffice\b|\brelocation\b|\bjob description\b|\bapply\b|"
    r"\blocation\b|[$€₽]|\d\s?k\b|\bруб\b|\busd\b|\beur\b",
    re.IGNORECASE,
)
#: реклама, анонсы курсов и вебинаров, розыгрыши — такой пост не вакансия.
#: Общие слова («обучение», «курсы», «скидки») бывают в льготах вакансий — их не берём
_NOT_VACANCY = re.compile(
    r"\berid\b|#реклама|^реклама\b|о рекламодателе|открыт\w* урок|старт\w* курса|курс «|"
    r"буткемп|на вебинаре|вебинар «|розыгрыш|разыгрыва|промокод|школа анализ|школа аналитик",
    re.IGNORECASE | re.MULTILINE,
)
#: слова должности и грейда: строка с ними похожа на название вакансии
_ROLE = re.compile(
    r"developer|engineer|разработчи|программист|инженер|analyst|аналитик|manager|менеджер|"
    r"designer|дизайнер|тестировщ|\b(?:a?qa|sre|devops|ml|ai|ux|ui|hr|cto|cpo|cmo|vp|pm)\b|"
    r"scientist|researcher|architect|архитектор|\blead\b|\bhead\b|director|директор|"
    r"руководител|specialist|специалист|consultant|консультант|owner|marketer|маркетолог|"
    r"\bsmm|\bseo\b|copywriter|копирайтер|editor|редактор|recruiter|рекрутер|бухгалтер|"
    r"accountant|support|поддержк|оператор|administrator|администратор|стаж[её]р|intern|"
    r"junior|middle|senior|джун|мидл|сеньор|креатор|producer|продюсер|officer|coordinator|"
    r"координатор|преподават|teacher|tutor|\bsales\b|продаж|full-?stack|front-?end|back-?end|"
    r"data |gamedev|геймдизайн|artist|художник|writer|representative|executive|assistant|"
    r"ассистент|помощник|business development|монтаж",
    re.IGNORECASE,
)
_HASHTAG = re.compile(r"#(\w[\w+#.-]*)")
_URL = re.compile(r"https?://\S+|t\.me/\S+|@\w+")
_LETTERS = re.compile(r"[A-Za-zА-Яа-яЁё]")
#: эмодзи и значки в начале строки
_LEADING = re.compile(r"^[^\wА-Яа-яЁё«\"(\[]+")
#: «Должность в Компания: ссылка» — формат Remocate и похожих каналов
_TITLE_AT = re.compile(r"^(?P<title>.+?) в (?P<company>[«\"'A-ZА-ЯЁ0-9].*?)\s*:?\s*$")
_PREFIX = re.compile(
    r"^(tl;?dr|(?:новая |горячая |прямая )?ваканси[яи]|vacancy|job|позиция|position)\s*[:.-]\s*",
    re.IGNORECASE,
)
_SUFFIX = re.compile(r"\s*\|\s*смотреть ваканси\w*$", re.IGNORECASE)
#: сколько первых строк смотреть в поисках названия
_TITLE_LINES = 4


def has_role(line: str) -> bool:
    return bool(_ROLE.search(line))


#: короткое название с должностью — вакансия даже без слов-признаков в тексте
_SHORT_TITLE = 80


def is_vacancy(text: str, title: str) -> bool:
    """Не реклама и не статья: в названии должность, в тексте признаки вакансии.

    Посты-статьи («ИИ-рекрутер: что это за зверь?») отсекает вопрос в названии.
    """
    if _NOT_VACANCY.search(text) or "?" in title or not has_role(title):
        return False
    return len(title) <= _SHORT_TITLE or bool(_VACANCY.search(text))


def _clean(raw: str) -> str:
    line = _URL.sub(" ", _HASHTAG.sub(" ", raw))
    line = " ".join(line.split()).strip(" -—–:•|*")
    line = _SUFFIX.sub("", _PREFIX.sub("", _LEADING.sub("", line)))
    return line.strip(" -—–:•|*")


def title_line(text: str) -> str:
    """Название из первых строк поста: первая строка с должностью, иначе первая содержательная.

    «Новая вакансия» и приветствия пропускаются, эмодзи, хэштеги и ссылки убираются.
    """
    lines = [_clean(raw) for raw in text.split("\n")]
    lines = [ln for ln in lines if len(_LETTERS.findall(ln)) >= 3][:_TITLE_LINES]
    return next((ln for ln in lines if has_role(ln)), lines[0] if lines else "")


def split_title(line: str) -> tuple[str, str]:
    """Название и компания из «Python Developer в Яндекс»."""
    m = _TITLE_AT.match(line)
    if m and len(m.group("company")) <= 60:
        return m.group("title").strip(), m.group("company").strip(" «»\"'")
    return line, ""


def formats_of(text: str) -> tuple[str, ...]:
    low = text.lower()
    found = []
    if re.search(r"удал[её]н|\bremote\b|из любой точки", low):
        found.append("remote")
    if re.search(r"гибрид|\bhybrid\b", low):
        found.append("hybrid")
    if re.search(r"\bофис|\boffice\b|on-?site", low):
        found.append("office")
    return tuple(found)


@register
class TelegramChannels(FeedSource):
    name = "telegram"
    title = "Telegram-каналы"
    countries = frozenset({"BY", "RU", "KZ", "GE", "PL", "REMOTE"})
    feed_ttl: ClassVar[float] = 25 * 60

    @classmethod
    def is_configured(cls, settings: Settings) -> bool:
        return bool(settings.tg_channels)

    async def fetch_feed(self) -> list[Vacancy]:
        result: list[Vacancy] = []
        for channel in self.settings.tg_channels:
            try:
                html = await self.http.get_text(f"{BASE}/s/{channel}")
            except HttpError as exc:
                log.warning("Telegram-канал @%s недоступен: HTTP %s", channel, exc.status)
                continue
            posts = self.parse_channel(html)
            if not posts and "tgme_widget_message" not in html:
                log.warning("Telegram-канал @%s: нет постов — закрыт или переименован", channel)
            result += posts
        result.sort(key=lambda v: v.published_at or _EPOCH, reverse=True)
        return result

    def parse_channel(self, html: str) -> list[Vacancy]:
        result: list[Vacancy] = []
        for msg in LexborHTMLParser(html).css(".tgme_widget_message[data-post]"):
            post = msg.attributes.get("data-post") or ""
            node = msg.css_first(".tgme_widget_message_text")
            if not post or not node:
                continue
            text = html_to_text(node.html)
            line = title_line(text)
            if not line or not is_vacancy(text, line):
                continue
            title, company = split_title(line)
            time_node = msg.css_first("time[datetime]")
            formats = formats_of(text)
            tags = tuple(dict.fromkeys(t.lower() for t in _HASHTAG.findall(text)))
            result.append(
                Vacancy(
                    source=self.name,
                    external_id=post,
                    title=title[:120],
                    url=f"{BASE}/{post}",
                    company=company,
                    location=f"@{post.split('/', 1)[0]}",
                    remote=("remote" in formats) or None,
                    work_formats=formats,
                    skills=tags[:15],
                    snippet=text[:300],
                    published_at=parse_iso(time_node.attributes.get("datetime"))
                    if time_node
                    else None,
                    description=text,
                )
            )
        return result

    def haystack(self, v: Vacancy) -> str:
        # у поста нет тегов площадки: стек обычно в первых строках текста
        return " ".join((v.title, " ".join(v.skills), v.snippet))
