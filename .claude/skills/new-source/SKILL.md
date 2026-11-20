---
name: new-source
description: Добавить в бота новую площадку вакансий — модуль, регистрация, настройки, фикстура, тест и живая проверка. Аргумент — сайт или название площадки.
argument-hint: <сайт или название площадки>
disable-model-invocation: true
---

# Новая площадка: $ARGUMENTS

Следуй рецепту из CONTRIBUTING.md («Новая площадка»). Образцы: `sources/getmatch.py` (JSON API),
`sources/praca.py` / `sources/jobsge.py` (HTML через selectolax), `sources/superjob.py`
(площадка с ключом). Перед кодом прочитай `sources/base.py` и `models.py` (`Vacancy`).

## 0. Разведка (до кода)

1. Найди у площадки публичный JSON API или стабильную HTML-выдачу поиска. Проверь `robots.txt`
   и условия: если поиск закрыт капчей, Cloudflare-челленджем или требует логина — остановись
   и сообщи пользователю. Обходить защиту нельзя.
2. Выбери `name` (snake_case, станет id в БД — менять потом нельзя), `title`, страны (ISO-2).
3. Убедись, что одна страница выдачи (до `page_size` = 50) содержит id, заголовок, ссылку и
   дату. Если описание есть сразу в выдаче — клади его в `description`, тогда
   `fetch_description` не делает лишний запрос.

## 1. Модуль `src/jobspybot/sources/<name>.py`

```python
"""<Площадка> — кратко: страна, специфика."""

from __future__ import annotations

from jobspybot.models import Vacancy, normalize_currency
from jobspybot.sources.base import Source, SourceUnavailable, register
from jobspybot.text import html_to_text, parse_iso

BASE = "https://..."


@register
class MySite(Source):
    name = "mysite"
    title = "mysite.com"
    countries = frozenset({"BY"})

    async def search(self, query: str) -> list[Vacancy]:
        ...  # ровно один запрос; от новых к старым

    def parse_item(self, item) -> Vacancy:
        ...

    async def fetch_description(self, vacancy: Vacancy) -> str:
        ...  # только для прошедших отсев; если описание уже есть — верни его
```

Правила:
- HTTP только через `self.http` (честный User-Agent, ретраи уже там). Одна страница за поиск.
- На 401/403/бан — `raise SourceUnavailable(...)`.
- Парсер терпим к отсутствующим полям (`.get`, `or ""`), валюту — через `normalize_currency`,
  даты — `parse_iso`, HTML в текст — `html_to_text`. `snippet` — до 300 символов.
- Никаких новых зависимостей без крайней нужды (правило «Влияние на память»).

## 2. Регистрация

- Импорт модуля в `sources/__init__.py` (по алфавиту) и `name` в `SOURCE_ORDER` по порядку
  стран: BY → RU → KZ → GE → PL.

## 3. Ключ (если нужен)

- Поле в `Settings` (`config.py`) и закомментированная строка с пояснением в `.env.example`.
- В площадке переопределить `is_configured`. `.env` не трогать — значение впишет пользователь.

## 4. Фикстура и тест

1. Сохрани реальный ответ поиска, урежь до 2–3 вакансий и **обезличь**: имена, телефоны, email
   рекрутеров и прочие персональные данные — заменить. Положи в
   `tests/fixtures/<name>_search.(json|html)`; при отдельной странице вакансии —
   `<name>_vacancy.html`.
2. Тест в `tests/test_sources.py` по образцу соседних: `make_http(json_handler(...))` /
   `html_handler(...)`, проверь id/url, зарплату и валюту, страну, remote, уровень, навыки,
   `fetch_description`. Если площадка с ключом — тест на `is_configured`.

## 5. Проверки

```bash
ruff check . && ruff format --check .
pytest -q
python -X utf8 -m jobspybot check-sources "python"
```

Живая проверка должна показать непустую выдачу у новой площадки. Если не показала — разберись,
не подгоняй тест.

## 6. Оформление

- Строка в `CHANGELOG.md`, площадка в списке в `README.md`.
- Ветка `feat/source-<name>`, коммит `feat(sources): add <title>`. Коммит и PR — только если
  пользователь попросит.
