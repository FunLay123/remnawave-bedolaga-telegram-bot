# Восстановление Restic-бэкапа Remnawave Bot

Один снапшот содержит `remnawave-backup.tar`, внутри — `db.dump` (PostgreSQL
custom format) и `.env` с того же 5-минутного цикла. Restic-репозиторий отдельный
от старых папок `5min/`, `hourly/` и т.д.

Выполняй шаги 1–3 в одном shell с `set -eu`: после ошибки нельзя продолжать, а
`trap` удалит приватные артефакты при выходе. Не используй `pg_restore --clean`
против рабочей базы; переключение на проверенную БД делается конфигурацией, без
переименования и удаления старой базы.

## 1. Достать снапшот в приватный временный каталог

```bash
set -eu
umask 077

DC="docker compose -f docker-compose.yml -f docker-compose.local.yml"
# + -f docker-compose.asteria.yml, если он есть

RESTORE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/remnawave-restore.XXXXXX")
chmod 700 "$RESTORE_DIR"
trap 'rm -rf "$RESTORE_DIR"' EXIT

$DC exec backup restic snapshots
$DC exec backup restic check
$DC exec backup restic ls <snapshot-id>

$DC exec -T backup restic dump <snapshot-id> remnawave-backup.tar \
  > "$RESTORE_DIR/remnawave-backup.tar"
tar -C "$RESTORE_DIR" -xf "$RESTORE_DIR/remnawave-backup.tar"
chmod 600 "$RESTORE_DIR/db.dump" "$RESTORE_DIR/.env"
tar -tvf "$RESTORE_DIR/remnawave-backup.tar"
file "$RESTORE_DIR/db.dump"
$DC exec -T postgres pg_restore --list < "$RESTORE_DIR/db.dump" >/dev/null
```

В каталоге должны быть ровно `db.dump` и `.env`. Плейнтекст tar/dump не кладётся
в репозиторий; каталог создаётся через `mktemp` вне его. `trap` удалит его при
выходе; если нужно сохранить для разбора, убери `trap` и удали вручную.

## 2. Восстановить в свежую БД одной транзакцией

```bash
$DC stop bot backup

ORIGINAL_DB=$($DC exec -T postgres sh -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
[ -n "$ORIGINAL_DB" ] || { echo 'POSTGRES_DB is empty' >&2; exit 1; }
RESTORE_DB="${ORIGINAL_DB}_restore_$(date +%Y%m%d%H%M%S)"

# psql :"restore_db" безопасно квотирует идентификатор БД.
printf 'CREATE DATABASE :"restore_db";\n' |
  $DC exec -T postgres psql -v ON_ERROR_STOP=1 \
    -v restore_db="$RESTORE_DB" -U "$POSTGRES_USER" -d postgres

$DC exec -T postgres sh -c \
  'pg_restore --single-transaction --exit-on-error --no-owner --no-privileges \
     -U "$POSTGRES_USER" -d "$1"' sh "$RESTORE_DB" \
  < "$RESTORE_DIR/db.dump"
```

`--single-transaction` откатывает всё при первой ошибке. `--exit-on-error` не
даёт проглотить ошибку. `ORIGINAL_DB` берётся из фактического `POSTGRES_DB`,
поэтому поддерживается кастомное имя базы.

Проверь:

```bash
$DC exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$1" -v ON_ERROR_STOP=1 \
     -c "SELECT count(*) FROM users;" \
     -c "SELECT count(*) FROM subscriptions;"' sh "$RESTORE_DB"
```

Сравни числа с ожидаемыми и проверь несколько свежих записей. Если этого мало,
подними одноразовый бот на этой БД в отдельной сети/порту. Рабочую базу и
текущий `.env` не меняй, пока проверка не закончена.

## 3. Переключение с сохранением старой БД

Старая база остаётся под исходным `POSTGRES_DB`; новая — под `$RESTORE_DB`.
Переключение — только конфигурация, без renaming/delete.

```bash
$DC stop bot backup

ENV_BACKUP="${TMPDIR:-/tmp}/remnawave-env-before-restore.$(date +%Y%m%d%H%M%S)"
install -m 600 .env "$ENV_BACKUP"

# DB-only cutover: измени в .env только POSTGRES_DB=$RESTORE_DB.
# Если восстанавливаешь и конфигурацию, сначала проверь diff с
# "$RESTORE_DIR/.env" (не выводя секреты), затем установи его и поставь
# POSTGRES_DB=$RESTORE_DB.
$DC up -d --force-recreate bot backup
```

Если бот не стартует или данные неверны, откат одной конфигурацией:

```bash
$DC stop bot backup
install -m 600 "$ENV_BACKUP" .env
$DC up -d --force-recreate bot backup
```

Если восстановленный `.env` содержит другой `POSTGRES_PASSWORD`, существующая
роль PostgreSQL его не подхватит: оставь старый пароль или смени пароль роли
вручную. Старую БД `$ORIGINAL_DB` не переименовывай и не удаляй до проверки.

Не удаляй `$ORIGINAL_DB`, `$RESTORE_DB`, старый `.env` и `$ENV_BACKUP`, пока бот
и платежи не проверены. После стабилизации:

```bash
rm -rf "$RESTORE_DIR"
rm -f "$ENV_BACKUP"
```

## 4. Новый хост

1. Восстанови из офлайн-копии `rclone.conf` и файл пароля Restic.
2. Не выполняй `restic init`: репозиторий уже существует.
3. Проверь `restic snapshots`, подними `postgres`, создай свежую БД и повтори
   шаги 1–3. В `.env` укажи `POSTGRES_DB` восстановленной БД.
4. Только после проверки бота удаляй старые копии.

## Важно

- Healthcheck проверяет свежесть маркера, а не возможность восстановления.
  Периодически делай restore drill.
- Пароль Restic потерян — снапшоты не расшифровать. Храни его офлайн.
- Не запускай `pg_restore --clean` против рабочей базы.
- Не меняй `BACKUP_RESTIC_REPOSITORY` на старую папку `remnawave-bot`: новый
  сервис рассчитан только на отдельный Restic-репозиторий.
