# Remnawave Bot — резервные копии

Это папка с резервными копиями PostgreSQL и конфигурации бота.

## Структура

- `5min/` — последние 12 копий (1 час)
- `hourly/` — последние 24 копии (1 сутки)
- `daily/` — последние 30 копий
- `weekly/` — последние 12 копий
- `monthly/` — последние 12 копий

В каждой папке лежат пары:

- `DD_Month_YYYY_HH-MM.dump` — дамп PostgreSQL в custom-формате (`pg_dump -Fc`);
- `DD_Month_YYYY_HH-MM.env.bak` — копия `.env` на тот же момент.

Например:

```text
5min/26_January_2026_22-30.dump
5min/26_January_2026_22-30.env.bak
```

Если контейнер перезапустился в ту же минуту, имя получает суффикс `_2`.

## Шифрование

Имена файлов и папок открытые, содержимое зашифровано rclone crypt.
Чтобы прочитать файлы, нужны:

- rclone;
- конфиг rclone с remote `gdrive` и `gcrypt`;
- пароль crypt (хранится отдельно от сервера, например в менеджере паролей).

Без пароля и конфига файлы не расшифровать. Этот `README.md` не зашифрован.

## Скачать копию

```bash
rclone copyto gcrypt:5min/26_January_2026_22-30.dump ./db.dump
rclone copyto gcrypt:5min/26_January_2026_22-30.env.bak ./.env.restore
```

Если remote `gcrypt` указывает на `gdrive:remnawave-bot`, этого достаточно.
Если конфиг называется иначе — используй своё имя remote.

## Восстановить базу

1. Останови бота:

```bash
docker compose stop bot
```

2. Положи дамп в контейнер `backup`:

```bash
docker compose cp ./db.dump backup:/backups/restore.dump
```

3. Восстанови дамп (у контейнера `backup` есть `pg_restore`):

```bash
docker compose exec -T backup sh -c \
  'pg_restore -U "$PGUSER" -d "$PGDATABASE" --clean --if-exists --no-owner --no-privileges /backups/restore.dump'
```

Либо восстанови на хосте, если установлен `pg_restore`:

```bash
pg_restore --clean --if-exists --no-owner --no-privileges -d "$DATABASE_URL" ./db.dump
```

4. Запусти бота:

```bash
docker compose start bot
```

`--clean --if-exists` удаляет существующие объекты перед восстановлением.
Бота лучше остановить, чтобы он не писал в базу во время восстановления.
Если используешь несколько `-f` файлов compose, добавь их в каждую команду.

## Восстановить `.env`

```bash
docker compose stop bot
cp ./.env.restore ./.env
chmod 600 .env
docker compose up -d bot
```

## Проверка

```bash
docker compose exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM users;"'
```

## Важно

- `.env` содержит токены, пароль БД и ключи платёжных провайдеров. Не пересылай его и не выкладывай публично.
- Пароль crypt не хранится в этой папке. Если он потерян, копии не расшифровать.
- После восстановления проверь, что бот видит базу и платежи, и только потом удаляй старые копии.
