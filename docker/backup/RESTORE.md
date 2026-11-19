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

RESTORE_DIR=$(mktemp -d /tmp/remnawave-restore.XXXXXX)
chmod 700 "$RESTORE_DIR"
trap 'rm -rf "$RESTORE_DIR"' EXIT

$DC exec backup restic --no-cache snapshots
# restic check запускай, когда backup не держит operation lock (loop спит
# между слотами). Не делай unlock, пока идёт активный backup.
$DC exec backup restic --no-cache check
if [ -z "${SNAPSHOT_ID:-}" ]; then
    printf 'Snapshot ID: '
    read -r SNAPSHOT_ID
fi
[ -n "$SNAPSHOT_ID" ] || { echo 'SNAPSHOT_ID is required' >&2; exit 1; }

$DC exec backup restic --no-cache ls "$SNAPSHOT_ID"
$DC exec -T backup restic --no-cache dump "$SNAPSHOT_ID" remnawave-backup.tar \
  > "$RESTORE_DIR/remnawave-backup.tar"
tar -C "$RESTORE_DIR" -xf "$RESTORE_DIR/remnawave-backup.tar"
chmod 600 "$RESTORE_DIR/db.dump" "$RESTORE_DIR/.env"
tar -tvf "$RESTORE_DIR/remnawave-backup.tar"
file "$RESTORE_DIR/db.dump"
$DC exec -T postgres pg_restore --list < "$RESTORE_DIR/db.dump" >/dev/null
```

В tar должны быть ровно два члена: `db.dump` и `.env`. После распаковки в
`$RESTORE_DIR` временно лежит и сам `remnawave-backup.tar`; это нормально, его
удалит `trap` (или явный `rm` ниже). Плейнтекст tar/dump не кладётся в
репозиторий; каталог создаётся через `mktemp` вне его. Если нужно сохранить для
разбора, убери `trap` и удали вручную.

## 2. Восстановить в свежую БД одной транзакцией

```bash
$DC stop bot backup

ORIGINAL_DB=$($DC exec -T postgres sh -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
[ -n "$ORIGINAL_DB" ] || { echo 'POSTGRES_DB is empty' >&2; exit 1; }
# Fixed short ASCII prefix + timestamp/PID, independent of ORIGINAL_DB:
# PostgreSQL truncates identifiers over 63 bytes, including multibyte names.
RESTORE_DB="remnawave_restore_$(date +%Y%m%d%H%M%S)_$$"

# psql :"restore_db" безопасно квотирует идентификатор. POSTGRES_USER должен
# раскрыться внутри контейнера, а не в host shell под set -u.
printf 'CREATE DATABASE :"restore_db";\n' |
  $DC exec -T postgres sh -c \
    'psql -v ON_ERROR_STOP=1 -v restore_db="$1" -U "$POSTGRES_USER" -d postgres' \
    sh "$RESTORE_DB"

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

Сравни числа с ожидаемыми и проверь несколько свежих записей. Не поднимай второй
экземпляр production-бота с тем же `BOT_TOKEN`: Telegram допускает один poller, а
тестовая БД не должна получать production webhook. Для проверки достаточно SQL
и `psql`; при необходимости используй отдельный тестовый токен и сеть. Рабочую
базу и текущий `.env` не меняй, пока проверка не закончена.

## 3. Переключение с сохранением старой БД

Старая база остаётся под исходным `POSTGRES_DB`; новая — под `$RESTORE_DB`.
Переключение — только конфигурация, без renaming/delete.

В `.env` установи **ровно** `POSTGRES_DB=$RESTORE_DB` (если строки нет — добавь).
Не полагайся на export в текущем shell: Compose отдаёт приоритет переменной
окружения shell над `.env`, поэтому перед `up` её нужно снять.

```bash
$DC stop bot backup

# Идентифицируемая приватная копия .env вне репозитория, только для rollback.
ROLLBACK_DIR=$(mktemp -d /tmp/remnawave-env-rollback.XXXXXX)
chmod 700 "$ROLLBACK_DIR"
ENV_BACKUP="$ROLLBACK_DIR/.env.before-restore"
install -m 600 .env "$ENV_BACKUP"
echo "rollback env: $ENV_BACKUP"

# Если восстанавливаешь и конфигурацию: сначала проверь diff с
# "$RESTORE_DIR/.env" (не выводя секреты), затем установи его и обязательно
# поставь POSTGRES_DB=$RESTORE_DB.
#
# Снимаем унаследованный POSTGRES_DB, иначе Compose проигнорирует .env.
unset POSTGRES_DB
$DC up -d --no-deps --force-recreate bot backup

BOT_DB=$($DC exec -T bot sh -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
BACKUP_DB=$($DC exec -T backup sh -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'effective bot POSTGRES_DB=%s\n' "$BOT_DB"
printf 'effective backup PGDATABASE=%s\n' "$BACKUP_DB"
[ "$BOT_DB" = "$RESTORE_DB" ] && [ "$BACKUP_DB" = "$RESTORE_DB" ] || {
  echo 'effective DB mismatch; stop and fix config before continuing' >&2
  exit 1
}
```

Если бот не стартует или данные неверны, откат одной конфигурацией. Rollback
тоже должен снять возможный host-override:

```bash
$DC stop bot backup
unset POSTGRES_DB
install -m 600 "$ENV_BACKUP" .env
$DC up -d --no-deps --force-recreate bot backup

BOT_DB=$($DC exec -T bot sh -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
BACKUP_DB=$($DC exec -T backup sh -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'rollback bot POSTGRES_DB=%s\n' "$BOT_DB"
printf 'rollback backup PGDATABASE=%s\n' "$BACKUP_DB"
[ "$BOT_DB" = "$ORIGINAL_DB" ] && [ "$BACKUP_DB" = "$ORIGINAL_DB" ] || {
  echo 'rollback DB mismatch; stop and fix config before continuing' >&2
  exit 1
}
```

Если восстановленный `.env` содержит другой `POSTGRES_PASSWORD`, существующая
роль PostgreSQL его не подхватит: оставь старый пароль или смени пароль роли
вручную. Старую БД `$ORIGINAL_DB` не переименовывай и не удаляй до проверки.

Не удаляй `$ORIGINAL_DB`, `$RESTORE_DB`, старый `.env`, `$ENV_BACKUP` и
`$ROLLBACK_DIR`, пока бот и платежи не проверены. После стабилизации:

```bash
rm -rf "$RESTORE_DIR"
rm -rf "$ROLLBACK_DIR"
```

## 4. Новый хост

1. Восстанови из офлайн-копии `rclone.conf` и файл пароля Restic.
2. Не выполняй `restic init`: репозиторий уже существует.
3. Проверь `restic --no-cache snapshots`, подними базовые сервисы:
   `$DC up -d --no-deps postgres redis`.
4. Создай свежую БД и повтори шаги 1–3. В `.env` укажи `POSTGRES_DB`
   восстановленной БД.
5. Подними приложение без рекурсии зависимостей:
   `$DC up -d --no-deps --force-recreate bot backup`. Только после проверки бота
   удаляй старые копии.

## Важно

- Healthcheck проверяет свежесть маркера, а не возможность восстановления.
  Периодически делай restore drill.
- Пароль Restic потерян — снапшоты не расшифровать. Храни его офлайн.
- Не запускай `pg_restore --clean` против рабочей базы.
- Не меняй `BACKUP_RESTIC_REPOSITORY` на старую папку `remnawave-bot`: новый
  сервис рассчитан только на отдельный Restic-репозиторий.
