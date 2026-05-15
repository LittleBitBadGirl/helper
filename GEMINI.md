# Project: Team Analyst Bot

## Специфика проекта
- **Admin ID**: 163394712 (Вера).
- **Deployment**: СТРОГО через GitHub Actions или `docker compose` на VPS (91.186.217.66).
- **Path Rules**: Внутри контейнера база данных должна лежать по пути `/app/data/analytics.db`. В `.env` на сервере `DB_URL` должен быть абсолютным: `sqlite+aiosqlite:////app/data/analytics.db`.

## Команды обслуживания (на сервере)
- **Логи**: `docker compose logs -f bot`
- **Перезапуск**: `docker compose up -d --build`
- **Проверка переменных**: `docker exec analitics_manager-bot-1 env`

## Важные исправления (Май 2026)
- Исправлен путь к БД: теперь используется абсолютный путь внутри контейнера, что решило ошибку `unable to open database file`.
- Исправлен Workflow деплоя: `docker-compose` заменен на `docker compose`.
- Настроены небуферизованные логи (`PYTHONUNBUFFERED=1`).
- Деплой теперь использует `git reset --hard`, что исключает ошибки при наличии локальных изменений на сервере.
