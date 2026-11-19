# Бэкапы базы и восстановление

Вся ценность бота — резюме, профили, подписки и платежи — в одном файле SQLite
`/var/lib/job-spy-bot/jobspy.sqlite3`.

## Как делаются копии

- Раз в сутки в `BACKUP_HOUR_UTC`:00 (по умолчанию 03:00 UTC) и сразу при запуске, если
  свежей копии нет.
- Копия — через SQLite backup API, поэтому она согласованна даже во время работы бота.
- Файл `jobspy-ГГГГ-ММ-ДД_ЧЧММСС.sqlite3.gz` в `/var/lib/job-spy-bot/backups`, хранятся
  `BACKUP_KEEP` последних (по умолчанию 7).
- Свежая копия приходит каждому админу файлом в Telegram (`BACKUP_TO_TELEGRAM=1`).
- Сделать копию вручную: `POST /api/backup` (через туннель к админке).
- Сбой бэкапа — ошибка в логе, она приходит админу в Telegram.

## Восстановление

Все команды — **на сервере**.

1. Остановить бота:
   ```bash
   systemctl stop job-spy-bot
   ```
2. Отложить текущую базу (на случай, если понадобится):
   ```bash
   cd /var/lib/job-spy-bot
   mv jobspy.sqlite3 jobspy.sqlite3.broken
   rm -f jobspy.sqlite3-wal jobspy.sqlite3-shm
   ```
3. Распаковать нужную копию (с сервера или файл из Telegram, скопированный через `scp`):
   ```bash
   gunzip -c backups/jobspy-2026-11-20_030000.sqlite3.gz > jobspy.sqlite3
   chown jobspy: jobspy.sqlite3
   ```
   Проверить: `sqlite3 jobspy.sqlite3 "PRAGMA integrity_check; SELECT COUNT(*) FROM users;"`
   (должно быть `ok` и число пользователей).
4. Запустить:
   ```bash
   systemctl start job-spy-bot
   ```

Файл из Telegram скопировать на сервер — **в PowerShell на компьютере**:
```powershell
scp .\jobspy-2026-11-20_030000.sqlite3.gz root@ВАШ_СЕРВЕР:/var/lib/job-spy-bot/backups/
```
