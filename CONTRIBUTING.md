# Как вносить изменения

## Процесс

1. Задача заводится в [Issues](https://github.com/luxsordel/job-spy-bot/issues), шаблоны есть
   для ошибки, новой площадки и улучшения.
2. Ветка от `master`: `feat/<кратко>`, `fix/<кратко>`, `chore/<кратко>`, `docs/<кратко>`.
3. Коммиты в стиле [Conventional Commits](https://www.conventionalcommits.org/ru/):
   `feat(sources): add rabota.ge`, `fix(pipeline): ...`. Один коммит — одно логическое изменение.
4. Pull request по шаблону, со ссылкой на задачу (`Closes #N`). CI (ruff + pytest) должен быть зелёным.
5. Мерж после ревью.

## Проверки перед PR

```bash
ruff check . && ruff format --check .
pytest
```

## Новая площадка

1. Создайте `src/jobspybot/sources/<name>.py`:

   ```python
   @register
   class MySite(Source):
       name = "mysite"            # id в БД и настройках
       title = "mysite.com"       # как показывать пользователю
       countries = frozenset({"BY"})

       async def search(self, query: str) -> list[Vacancy]:
           data = await self.http.get_json("https://...", params={"q": query})
           return [self.parse_item(x) for x in data["items"]]

       async def fetch_description(self, vacancy: Vacancy) -> str:
           ...  # полное описание; вызывается только для прошедших отсев
   ```

2. Импортируйте модуль в `sources/__init__.py` и добавьте имя в `SOURCE_ORDER`.
3. Если нужен ключ, добавьте поле в `Settings` и `.env.example`, а в площадке переопределите
   `is_configured`. На 401/403 бросайте `SourceUnavailable`.
4. Тест: небольшая фикстура в `tests/fixtures/`, повторяющая структуру настоящего ответа.
   Реальные персональные данные в фикстуры не кладите.
5. Проверьте на живом сайте: `python -m jobspybot check-sources "python"`.

Правила вежливости: только честный User-Agent, без обхода защит и капчи. Не больше одной
страницы за запрос, интервал опроса не меньше 5 минут.

## Принципы

- **Память.** Каждая новая зависимость обосновывается в PR, в разделе «Влияние на память».
- **Токены.** ИИ получает только то, что прошло бесплатный отсев. Любой новый вызов модели
  должен иметь `max_tokens`, кэш или явный запрос пользователя.
