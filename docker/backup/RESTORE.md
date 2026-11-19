# Восстановление Restic-бэкапа Remnawave Bot

Один снапшот содержит `remnawave-backup.tar`, внутри — `db.dump` (PostgreSQL
custom format) и `.env` с того же 5-минутного цикла. Restic-репозиторий отдельный
от старых папок `5min/`, `hourly/` и т.д.

## 1. Достать снапшот

```bash
DC="docker compose -f docker-compose.yml -f docker-compose.local.yml"
# + -f docker-compose.asteria.yml, если он есть

$DC exec backup restic snapshots
$DC exec backup restic ls <snapshot-id>

install -d -m 700 ./restore
$DC exec -T backup restic dump <snapshot-id> remnawave-backup.tar > ./restore/remnawave-backup.tar
tar -C ./restore -xf ./restore/remnawave-backup.tar
chmod 600 ./restore/db.dump ./restore/.env
tar -tvf ./restore/remnawave-backup.tar
```

Проверь, что в архиве ровно два файла и что дамп не пустой:

```bash
file ./restore/db.dump
cat ./restore/db.dump | $DC exec -T postgres pg_restore --list >/dev/null
```

## 2. Восстановить в свежую БД (без `--clean`)

Останови бота и backup, но рабочую базу не удаляй:

```bash
$DC stop bot backup
$DC exec -T postgres sh -c \
  'createdb -U "$POSTGRES_USER" remnawave_restore_verify'
cat ./restore/db.dump | $DC exec -T postgres sh -c \
  'pg_restore --single-transaction --exit-on-error --no-owner --no-privileges \
     -U "$POSTGRES_USER" -d remnawave_restore_verify'
```

`--single-transaction` откатывает всё при первой ошибке. `--clean` против
рабочей базы не использовать.

Проверь пользователей, подписки и несколько свежих записей:

```bash
$DC exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d remnawave_restore_verify \
     -c "SELECT count(*) FROM users;" \
     -c "SELECT count(*) FROM subscriptions;"'
```

## 3. Переключение с сохранением старой БД

```bash
$DC stop bot backup
$DC exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d postgres -c "ALTER DATABASE remnawave_bot RENAME TO remnawave_bot_old_$(date +%Y%m%d);"'
$DC exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d postgres -c "ALTER DATABASE remnawave_restore_verify RENAME TO remnawave_bot;"'
```

Затем замени `.env` из **того же** снапшота:

```bash
cp .env .env.before-restore-$(date +%Y%m%d)
install -m 600 ./restore/.env .env
$DC up -d
```

Если в восстановленном `.env` другой `POSTGRES_PASSWORD`, существующая роль его
не подхватит: оставь старый пароль или смени пароль роли вручную.

Не удаляй `remnawave_bot_old_*`, старый `.env` и старые GFS-объекты, пока бот и
платежи не проверены. Откат — переименовать БД обратно и вернуть старый `.env`.

## 4. Новый хост

1. Восстанови из офлайн-копии `rclone.conf` и файл пароля Restic.
2. Не выполняй `restic init`: репозиторий уже существует.
3. Проверь `restic snapshots`, подними `postgres`, создай свежую БД и повтори
   шаги 1–3.
4. Только после проверки бота удаляй старые копии.

## Важно

- Healthcheck проверяет свежесть маркера, а не возможность восстановления.
  Периодически делай restore drill.
- Пароль Restic потерян — снапшоты не расшифровать. Храни его офлайн.
- Не запускай `pg_restore --clean` против рабочей базы.
- Не меняй `BACKUP_RESTIC_REPOSITORY` на старую папку `remnawave-bot`: новый
  сервис рассчитан только на отдельный Restic-репозиторий.
