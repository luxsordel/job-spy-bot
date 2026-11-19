# job-spy-bot

[![CI](https://github.com/luxsordel/job-spy-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/luxsordel/job-spy-bot/actions/workflows/ci.yml)

Telegram-бот, который следит за вакансиями на площадках Беларуси, России, Казахстана,
Грузии и Польши, отсеивает лишнее по фильтрам и отправляет подходящие вакансии с кратким
ИИ-анализом.

> Проект переписывается с Java на Python, чтобы работать на VPS с маленьким объёмом памяти.
> Прогресс: [milestone v1.0](https://github.com/luxsordel/job-spy-bot/milestone/1).
> Java-версия сохранена под тегом [`java-prototype`](https://github.com/luxsordel/job-spy-bot/tree/java-prototype).

## Быстрый старт (разработка)

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env            # заполнить токены
python -m jobspybot check-sources "python"
pytest && ruff check .
```

## Лицензия

MIT
