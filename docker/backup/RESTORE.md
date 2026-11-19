# Восстановление Restic-бэкапа Remnawave Bot

Один снапшот содержит `remnawave-backup.tar`, внутри — `db.dump` (PostgreSQL
custom format) и `.env` с того же 5-минутного цикла. Restic-репозиторий отдельный
от старых папок `5min/`, `hourly/` и т.д.

Выполняй шаги 1–3 в одном shell с `set -eu`: после ошибки нельзя продолжать, а
`trap` удалит приватные артефакты при выходе. Не используй `pg_restore --clean`
против рабочей базы; переключение на проверенную БД делается конфигурацией, без
переименования и удаления старой базы.

## 1. Остановить writers и прочитать снапшот

Чтение — одноразовым контейнером с entrypoint `restic`, поэтому backup loop и
prune не запускаются. Это работает и без уже запущенного backup-контейнера.

```bash
set -eu
umask 077

DC="docker compose -f docker-compose.yml -f docker-compose.local.yml"
# + -f docker-compose.asteria.yml, если он есть

$DC stop bot backup

RESTORE_DIR=$(mktemp -d /tmp/remnawave-restore.XXXXXX)
chmod 700 "$RESTORE_DIR"
trap 'rm -rf "$RESTORE_DIR"' EXIT

$DC --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache snapshots
# check запускай, когда нет активного backup-writer. При locked сначала
# убедись, что writer не работает; unlock вручную.
$DC --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache check
if [ -z "${SNAPSHOT_ID:-}" ]; then
    printf 'Snapshot ID: '
    read -r SNAPSHOT_ID
fi
[ -n "$SNAPSHOT_ID" ] || { echo 'SNAPSHOT_ID is required' >&2; exit 1; }

$DC --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache ls "$SNAPSHOT_ID"
$DC --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache dump "$SNAPSHOT_ID" remnawave-backup.tar \
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
репозиторий; каталог создаётся через `mktemp` вне его.

## 2. Зафиксировать фактическую БД и восстановить в свежую

Целевую БД берём из **preflight effective app Compose config** (bot и backup), а
не из env-файла контейнера `postgres`: после earlier `--no-deps` cutover env
postgres-контейнера может остаться старым. Rollback expected DB — это
`$ORIGINAL_DB`, полученный этим preflight. Если preflight не совпадает с тем,
с чем фактически работал бот, сначала приведи `.env` в соответствие и не начинай
cutover.

```bash
unset POSTGRES_DB

ORIGINAL_BOT_DB=$($DC --profile backup run --rm --no-deps -T \
  --entrypoint sh bot -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
ORIGINAL_BACKUP_DB=$($DC --profile backup run --rm --no-deps -T \
  --entrypoint sh backup -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'preflight bot=%s backup=%s\n' "$ORIGINAL_BOT_DB" "$ORIGINAL_BACKUP_DB"
[ -n "$ORIGINAL_BOT_DB" ] && [ "$ORIGINAL_BOT_DB" = "$ORIGINAL_BACKUP_DB" ] || {
  echo 'preflight DB mismatch; stop and fix Compose config' >&2
  exit 1
}
ORIGINAL_DB="$ORIGINAL_BOT_DB"

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
даёт проглотить ошибку. `ORIGINAL_DB` берётся из preflight effective app config.

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

## 3. Cutover A/B/C с rollback

### A. Остановить и сохранить исходный live `.env` — до любых правок

```bash
$DC stop bot backup

# Идентифицируемая приватная копия .env вне репозитория, только для rollback.
ROLLBACK_DIR=$(mktemp -d /tmp/remnawave-env-rollback.XXXXXX)
chmod 700 "$ROLLBACK_DIR"
ENV_BACKUP="$ROLLBACK_DIR/.env.before-restore"
install -m 600 .env "$ENV_BACKUP"
[ -s "$ENV_BACKUP" ] || { echo 'rollback .env copy is empty' >&2; exit 1; }
echo "rollback env: $ENV_BACKUP"
```

Блок A не меняет `.env`. Не редактируй `.env` до его выполнения.

### B. Явно изменить `.env`, дождаться Enter, затем preflight и запуск

Блок напечатает **готовое literal-значение** `RESTORE_DB`. Впиши в `.env` ровно
строку `POSTGRES_DB=<это значение>` (не `$RESTORE_DB` буквально), сохрани файл и
нажми Enter. `read` останавливает выполнение, если команды вставлены одним куском;
без Enter дальше не пойдёт.

```bash
printf 'Set exactly in .env: POSTGRES_DB=%s\n' "$RESTORE_DB"
printf 'Press Enter after saving .env... '
read -r _
[ -f "$ENV_BACKUP" ] || { echo 'missing rollback .env; run block A first' >&2; exit 1; }

# Host export has precedence over .env in Compose interpolation.
unset POSTGRES_DB
PRE_BOT_DB=$($DC --profile backup run --rm --no-deps -T \
  --entrypoint sh bot -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
PRE_BACKUP_DB=$($DC --profile backup run --rm --no-deps -T \
  --entrypoint sh backup -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'preflight cutover bot=%s backup=%s\n' "$PRE_BOT_DB" "$PRE_BACKUP_DB"
[ "$PRE_BOT_DB" = "$RESTORE_DB" ] && [ "$PRE_BACKUP_DB" = "$RESTORE_DB" ] || {
  echo 'preflight DB mismatch; fix .env, do not start' >&2
  exit 1
}

$DC up -d --no-deps --force-recreate bot backup

BOT_DB=$($DC exec -T bot sh -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
BACKUP_DB=$($DC exec -T backup sh -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'effective bot=%s backup=%s\n' "$BOT_DB" "$BACKUP_DB"
[ "$BOT_DB" = "$RESTORE_DB" ] && [ "$BACKUP_DB" = "$RESTORE_DB" ] || {
  echo 'effective DB mismatch; stop and fix config before continuing' >&2
  exit 1
}
```

### C. Rollback: вернуть исходный `.env` из A и проверить

```bash
$DC stop bot backup
[ -f "$ENV_BACKUP" ] || { echo 'missing rollback .env; run block A first' >&2; exit 1; }
unset POSTGRES_DB
install -m 600 "$ENV_BACKUP" .env

PRE_BOT_DB=$($DC --profile backup run --rm --no-deps -T \
  --entrypoint sh bot -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
PRE_BACKUP_DB=$($DC --profile backup run --rm --no-deps -T \
  --entrypoint sh backup -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'preflight rollback bot=%s backup=%s\n' "$PRE_BOT_DB" "$PRE_BACKUP_DB"
[ "$PRE_BOT_DB" = "$ORIGINAL_DB" ] && [ "$PRE_BACKUP_DB" = "$ORIGINAL_DB" ] || {
  echo 'rollback preflight mismatch; fix .env, do not start' >&2
  exit 1
}

$DC up -d --no-deps --force-recreate bot backup

BOT_DB=$($DC exec -T bot sh -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
BACKUP_DB=$($DC exec -T backup sh -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'rollback effective bot=%s backup=%s\n' "$BOT_DB" "$BACKUP_DB"
[ "$BOT_DB" = "$ORIGINAL_DB" ] && [ "$BACKUP_DB" = "$ORIGINAL_DB" ] || {
  echo 'rollback effective DB mismatch; stop and fix config' >&2
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

## 4. Новый хост без running backup

Порядок: сначала читаем удалённый снапшот одноразовым Restic, **не** устанавливая
app `.env`; затем ставим `.env`, поднимаем postgres/redis, восстанавливаем БД и
переключаем конфигурацию. `compose run` с entrypoint `restic` не запускает
автоматический backup/prune.

1. Восстанови из офлайн-копии `rclone.conf` и файл пароля Restic. Не выполняй
   `restic init`.
2. Собери/скачай образы `bot` и `backup`:

```bash
DC="docker compose -f docker-compose.yml -f docker-compose.local.yml"
# + -f docker-compose.asteria.yml, если он есть
$DC --profile backup build bot backup
```

3. Пока app `.env` ещё не установлен, прочитай снапшот. Выполняй шаги 3–6 в
   одном shell: `RESTORE_DIR`, `RESTORE_DB` и `ORIGINAL_DB` должны сохраниться.
   При кастомных repo/путях экспортируй их для этого shell; иначе действуют
   defaults из compose:

```bash
set -eu
umask 077

DC="docker compose -f docker-compose.yml -f docker-compose.local.yml"
# + -f docker-compose.asteria.yml, если он есть
# export BACKUP_RESTIC_REPOSITORY='rclone:gdrive:remnawave-restic'
# export BACKUP_RCLONE_CONFIG_DIR='/etc/remnawave-backup/rclone'
# export BACKUP_RESTIC_PASSWORD_FILE='/etc/remnawave-backup/restic-password'

RESTORE_DIR=$(mktemp -d /tmp/remnawave-restore.XXXXXX)
chmod 700 "$RESTORE_DIR"
trap 'rm -rf "$RESTORE_DIR"' EXIT

$DC --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache snapshots
if [ -z "${SNAPSHOT_ID:-}" ]; then
    printf 'Snapshot ID: '
    read -r SNAPSHOT_ID
fi
[ -n "$SNAPSHOT_ID" ] || { echo 'SNAPSHOT_ID is required' >&2; exit 1; }

$DC --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache check
$DC --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache dump "$SNAPSHOT_ID" remnawave-backup.tar \
  > "$RESTORE_DIR/remnawave-backup.tar"
tar -C "$RESTORE_DIR" -xf "$RESTORE_DIR/remnawave-backup.tar"
chmod 600 "$RESTORE_DIR/db.dump" "$RESTORE_DIR/.env"
tar -tvf "$RESTORE_DIR/remnawave-backup.tar"
file "$RESTORE_DIR/db.dump"
```

4. После проверки пары установи восстановленный `.env` как live-конфигурацию:

```bash
install -m 600 "$RESTORE_DIR/.env" .env
```

5. Подними и проверь только базовые сервисы:

```bash
$DC up -d --no-deps postgres redis
$DC exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d postgres'
$DC exec -T redis redis-cli ping
```

6. Дальше выполни шаги 2 и 3: preflight фактической БД, restore в свежую БД,
   cutover A/B/C. На новом хосте `$ORIGINAL_DB` — БД из восстановленного `.env`;
   старой населённой БД может не быть, но `.env` и rollback-копию всё равно
   сохраняем.

## Важно

- Healthcheck проверяет свежесть маркера, а не возможность восстановления.
  Периодически делай restore drill.
- Пароль Restic потерян — снапшоты не расшифровать. Храни его офлайн.
- Не запускай `pg_restore --clean` против рабочей базы.
- Не меняй `BACKUP_RESTIC_REPOSITORY` на старую папку `remnawave-bot`: новый
  сервис рассчитан только на отдельный Restic-репозиторий.
