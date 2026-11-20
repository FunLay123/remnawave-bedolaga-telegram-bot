# Восстановление Restic-бэкапа Remnawave Bot

Один снапшот содержит `remnawave-backup.tar`, внутри — `db.dump` (PostgreSQL
custom format) и `.env` с того же 5-минутного цикла. Если включён
`BACKUP_COMPOSE_FILES`, в том же tar есть `compose/project-name`,
`compose/files.list` и выбранные Compose-файлы в порядке списка.
`BACKUP_COMPOSE_PROJECT` backup получает из `COMPOSE_PROJECT_NAME`; задавай его
явно вместе с `-p`. Restic-репозиторий отдельный от старых папок `5min/`,
`hourly/` и т.д.; старые двухфайловые снапшоты остаются доступны через data-only
restore.

Выполняй шаги 1–3 в одном shell с `set -euo pipefail`: после ошибки нельзя
продолжать. `RESTORE_DIR` — стабильный приватный каталог вне Git и не `/tmp`; не
удаляй его, пока проверка не закончена. Не используй `pg_restore --clean` против
рабочей базы; переключение на проверенную БД делается конфигурацией, без
переименования и удаления старой базы.

`docker/backup/restore-compose.sh` запускается из checkout репозитория и
не копируется в backup-образ: он нужен на host, рядом с repo. Сначала проверка,
потом явное решение оператора, и только затем установка выбранных файлов в
project root.

## 0. Bash helper и общий setup

`docker/backup/compose-recovery.sh` строит Compose command из live Docker label
`com.docker.compose.project.config_files` или из проверенного
`compose/files.list`. Он сравнивает exact ordered arrays, затем bytes через
`cmp -s`, печатает только basenames/status, не eval/source-ит env или manifest и
не создаёт сети. Helper запускается из checkout репозитория и не копируется в
backup-образ.

Все команды runbook — Bash; выполняй их в одном Bash shell.

```bash
set -euo pipefail
PROJECT_DIR=$(pwd -P)
if [ -z "${EXPECTED_COMPOSE_PROJECT:-}" ]; then
    printf 'Expected Compose project: '
    read -r EXPECTED_COMPOSE_PROJECT
fi
export EXPECTED_COMPOSE_PROJECT

# 1 only when this project intentionally uses the shared panel network.
PANEL_NETWORK_REQUIRED=${PANEL_NETWORK_REQUIRED:-0}
case "$PANEL_NETWORK_REQUIRED" in
    0|1) ;;
    *) echo 'PANEL_NETWORK_REQUIRED must be 0 or 1' >&2; exit 1 ;;
esac
# EXPECTED_PANEL_PROJECT='<panel-compose-project>'

PREFLIGHT_ARGS=(--preflight-network)
if [ "$PANEL_NETWORK_REQUIRED" = 1 ]; then
    PREFLIGHT_ARGS+=(--require-panel-network)
fi
if [ -n "${EXPECTED_PANEL_PROJECT:-}" ]; then
    PREFLIGHT_ARGS+=(--panel-project "$EXPECTED_PANEL_PROJECT")
fi

dc_live() {
    bash docker/backup/compose-recovery.sh       --project "$EXPECTED_COMPOSE_PROJECT"       --project-dir "$PROJECT_DIR"       --live "${PREFLIGHT_ARGS[@]}" -- "$@"
}

dc_restored() {
    bash docker/backup/compose-recovery.sh       --project "$EXPECTED_COMPOSE_PROJECT"       --project-dir "$PROJECT_DIR"       --restore-dir "$RESTORE_DIR"       "${PREFLIGHT_ARGS[@]}" -- "$@"
}

dc_trusted() {
    local args=(
      --project "$EXPECTED_COMPOSE_PROJECT"
      --project-dir "$PROJECT_DIR"
      "${PREFLIGHT_ARGS[@]}"
    )
    local name
    [ "${#TRUSTED_COMPOSE_FILES[@]}" -gt 0 ] || {
        echo 'TRUSTED_COMPOSE_FILES is empty' >&2
        return 1
    }
    for name in "${TRUSTED_COMPOSE_FILES[@]}"; do
        args+=(--file "$name")
    done
    bash docker/backup/compose-recovery.sh "${args[@]}" -- "$@"
}

dc_bootstrap() {
    docker compose --env-file /dev/null \
      --project-directory "$PROJECT_DIR/docker/backup" \
      -p "${EXPECTED_COMPOSE_PROJECT}-recovery" \
      -f "$PROJECT_DIR/docker/backup/compose.restore.yml" "$@"
}

dc_recover() {
    if [ -f "$RESTORE_DIR/compose/files.list" ]; then
        dc_restored "$@"
    else
        dc_live "$@"
    fi
}
```

Legacy operator-owned input вводится prompt'ом в legacy branch, а не hardcoded
массивом. Пример: `docker-compose.yml docker-compose.local.yml`; для panel
deployment добавь проверенный override. Если `--live` не может прочитать
`config_files` label, не угадывай: задай prompt `TRUSTED_COMPOSE_FILES` и
используй `dc_trusted`. Для Asteria panel deployment оператор задаёт
`PANEL_NETWORK_REQUIRED=1` и, если есть label, `EXPECTED_PANEL_PROJECT`.

## 1. Прочитать снапшот и проверить live order до остановки

Сначала читаем `snapshots`/`dump` одноразовым Restic, **не останавливая**
bot/backup: dump read-only, snapshot атомарен. `dc_live` проверяет project и
containers до каждой команды, а network — перед `up`. После проверки snapshot
list/bytes можно
останавливать writers.

```bash
RECOVERY_ROOT=${RECOVERY_ROOT:-/var/lib/remnawave-recovery}
install -d -m 700 "$RECOVERY_ROOT"
RESTORE_DIR="$RECOVERY_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 700 "$RESTORE_DIR"
# Если нет root: RECOVERY_ROOT="$HOME/.local/state/remnawave-recovery".

dc_live --profile backup run --rm --no-deps -T --entrypoint restic backup   --no-cache snapshots
if [ -z "${SNAPSHOT_ID:-}" ]; then
    printf 'Snapshot ID: '
    read -r SNAPSHOT_ID
fi
[ -n "$SNAPSHOT_ID" ] || { echo 'SNAPSHOT_ID is required' >&2; exit 1; }

dc_live --profile backup run --rm --no-deps -T --entrypoint restic backup   --no-cache ls "$SNAPSHOT_ID"
dc_live --profile backup run --rm --no-deps -T --entrypoint restic backup   --no-cache dump "$SNAPSHOT_ID" remnawave-backup.tar   > "$RESTORE_DIR/remnawave-backup.tar"

sh docker/backup/restore-compose.sh   --archive "$RESTORE_DIR/remnawave-backup.tar"   --inspect
printf 'Snapshot kind [compose/legacy]: '
read -r SNAPSHOT_KIND
if [ "$SNAPSHOT_KIND" = compose ]; then
    sh docker/backup/restore-compose.sh       --archive "$RESTORE_DIR/remnawave-backup.tar"       --project "$EXPECTED_COMPOSE_PROJECT"       --output "$RESTORE_DIR"
elif [ "$SNAPSHOT_KIND" = legacy ]; then
    sh docker/backup/restore-compose.sh       --archive "$RESTORE_DIR/remnawave-backup.tar"       --legacy-data-only       --output "$RESTORE_DIR"
else
    echo 'SNAPSHOT_KIND must be compose or legacy' >&2
    exit 1
fi

if [ -f "$RESTORE_DIR/compose/files.list" ]; then
    bash docker/backup/compose-recovery.sh       --project "$EXPECTED_COMPOSE_PROJECT"       --project-dir "$PROJECT_DIR"       --restore-dir "$RESTORE_DIR"       --verify-live-match
else
    # Legacy 16e67daf: keep the verified live array; no archived list switch.
    :
fi

dc_recover stop bot backup
dc_recover --profile backup run --rm --no-deps -T --entrypoint restic backup   --no-cache check

if [ -f "$RESTORE_DIR/compose/files.list" ]; then
    cat "$RESTORE_DIR/compose/project-name"
    cat "$RESTORE_DIR/compose/files.list"
fi
file "$RESTORE_DIR/db.dump"
dc_recover exec -T postgres pg_restore --list < "$RESTORE_DIR/db.dump" >/dev/null
```

Helper не использует `tar -xf`: проверяет exact member set/types, стримит только
`db.dump`, `.env`, `compose/*` в приватный scratch и пишет в `$RESTORE_DIR` лишь
после project validation. `files.list` — авторитетный порядок. Каталог
стабильный, вне Git; не удаляй его до окончания проверки.

## 2. Зафиксировать фактическую БД и восстановить в свежую

Целевую БД берём из **preflight effective app Compose config** (bot и backup), а
не из env-файла контейнера `postgres`: после earlier `--no-deps` cutover env
postgres-контейнера может остаться старым. Rollback expected DB — это
`$ORIGINAL_DB`, полученный этим preflight. Если preflight не совпадает с тем,
с чем фактически работал бот, сначала приведи `.env` в соответствие и не начинай
cutover.

```bash
unset POSTGRES_DB

ORIGINAL_BOT_DB=$(dc_recover --profile backup run --rm --no-deps -T \
  --entrypoint sh bot -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
ORIGINAL_BACKUP_DB=$(dc_recover --profile backup run --rm --no-deps -T \
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
  dc_recover exec -T postgres sh -c \
    'psql -v ON_ERROR_STOP=1 -v restore_db="$1" -U "$POSTGRES_USER" -d postgres' \
    sh "$RESTORE_DB"

dc_recover exec -T postgres sh -c \
  'pg_restore --single-transaction --exit-on-error --no-owner --no-privileges \
     -U "$POSTGRES_USER" -d "$1"' sh "$RESTORE_DB" \
  < "$RESTORE_DIR/db.dump"
```

`--single-transaction` откатывает всё при первой ошибке. `--exit-on-error` не
даёт проглотить ошибку. `ORIGINAL_DB` берётся из preflight effective app config.

Проверь:

```bash
dc_recover exec -T postgres sh -c \
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
dc_recover stop bot backup

# Идентифицируемая приватная копия .env вне репозитория, только для rollback.
ROLLBACK_DIR="$RECOVERY_ROOT/rollback-$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 700 "$ROLLBACK_DIR"
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
PRE_BOT_DB=$(dc_recover --profile backup run --rm --no-deps -T \
  --entrypoint sh bot -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
PRE_BACKUP_DB=$(dc_recover --profile backup run --rm --no-deps -T \
  --entrypoint sh backup -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'preflight cutover bot=%s backup=%s\n' "$PRE_BOT_DB" "$PRE_BACKUP_DB"
[ "$PRE_BOT_DB" = "$RESTORE_DB" ] && [ "$PRE_BACKUP_DB" = "$RESTORE_DB" ] || {
  echo 'preflight DB mismatch; fix .env, do not start' >&2
  exit 1
}

dc_recover up -d --no-deps --force-recreate bot backup

BOT_DB=$(dc_recover exec -T bot sh -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
BACKUP_DB=$(dc_recover exec -T backup sh -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'effective bot=%s backup=%s\n' "$BOT_DB" "$BACKUP_DB"
[ "$BOT_DB" = "$RESTORE_DB" ] && [ "$BACKUP_DB" = "$RESTORE_DB" ] || {
  echo 'effective DB mismatch; stop and fix config before continuing' >&2
  exit 1
}
```

### C. Rollback: вернуть исходный `.env` из A и проверить

```bash
dc_recover stop bot backup
[ -f "$ENV_BACKUP" ] || { echo 'missing rollback .env; run block A first' >&2; exit 1; }
unset POSTGRES_DB
install -m 600 "$ENV_BACKUP" .env

PRE_BOT_DB=$(dc_recover --profile backup run --rm --no-deps -T \
  --entrypoint sh bot -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
PRE_BACKUP_DB=$(dc_recover --profile backup run --rm --no-deps -T \
  --entrypoint sh backup -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'preflight rollback bot=%s backup=%s\n' "$PRE_BOT_DB" "$PRE_BACKUP_DB"
[ "$PRE_BOT_DB" = "$ORIGINAL_DB" ] && [ "$PRE_BACKUP_DB" = "$ORIGINAL_DB" ] || {
  echo 'rollback preflight mismatch; fix .env, do not start' >&2
  exit 1
}

dc_recover up -d --no-deps --force-recreate bot backup

BOT_DB=$(dc_recover exec -T bot sh -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
BACKUP_DB=$(dc_recover exec -T backup sh -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
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

Все шаги — Bash в одном shell; сначала выполни setup из раздела 0. Live
containers отсутствуют, поэтому initial Restic read идёт с operator-owned
trusted inputs. Пример (не выполнять вслепую): `docker-compose.yml` и
`docker-compose.local.yml`; actual input вводится prompt'ом.

1. Восстанови из офлайн-копии `rclone.conf` и файл пароля Restic. Не выполняй
   `restic init`.
2. До установки application `.env` используй только standalone backup Compose.
   Он не читает application stack, не подключает panel network и не запускает
   `backup` loop. Собери только backup image и прочитай снапшот:

```bash
set -euo pipefail
umask 077

printf 'Trusted Compose basenames (space-separated): '
read -r -a TRUSTED_COMPOSE_FILES
[ "${#TRUSTED_COMPOSE_FILES[@]}" -gt 0 ] || {
    echo 'at least one trusted Compose basename is required' >&2
    exit 1
}

dc_bootstrap build backup

RECOVERY_ROOT=${RECOVERY_ROOT:-/var/lib/remnawave-recovery}
install -d -m 700 "$RECOVERY_ROOT"
RESTORE_DIR="$RECOVERY_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 700 "$RESTORE_DIR"
# export BACKUP_RESTIC_REPOSITORY='rclone:gdrive:remnawave-restic'
# export BACKUP_RCLONE_CONFIG_DIR='/etc/remnawave-backup/rclone'
# export BACKUP_RESTIC_PASSWORD_FILE='/etc/remnawave-backup/restic-password'

dc_bootstrap run --rm --no-deps -T --entrypoint restic backup   --no-cache snapshots
if [ -z "${SNAPSHOT_ID:-}" ]; then
    printf 'Snapshot ID: '
    read -r SNAPSHOT_ID
fi
[ -n "$SNAPSHOT_ID" ] || { echo 'SNAPSHOT_ID is required' >&2; exit 1; }
dc_bootstrap run --rm --no-deps -T --entrypoint restic backup   --no-cache check
dc_bootstrap run --rm --no-deps -T --entrypoint restic backup   --no-cache dump "$SNAPSHOT_ID" remnawave-backup.tar   > "$RESTORE_DIR/remnawave-backup.tar"

sh docker/backup/restore-compose.sh   --archive "$RESTORE_DIR/remnawave-backup.tar"   --inspect
printf 'Snapshot kind [compose/legacy]: '
read -r SNAPSHOT_KIND
if [ "$SNAPSHOT_KIND" = compose ]; then
    sh docker/backup/restore-compose.sh       --archive "$RESTORE_DIR/remnawave-backup.tar"       --project "$EXPECTED_COMPOSE_PROJECT"       --output "$RESTORE_DIR"
elif [ "$SNAPSHOT_KIND" = legacy ]; then
    sh docker/backup/restore-compose.sh       --archive "$RESTORE_DIR/remnawave-backup.tar"       --legacy-data-only       --output "$RESTORE_DIR"
else
    echo 'SNAPSHOT_KIND must be compose or legacy' >&2
    exit 1
fi

# Branch on validated metadata presence, not on a guessed override set.
if [ -f "$RESTORE_DIR/compose/files.list" ]; then
    dc_recover() { dc_restored "$@"; }
else
    dc_recover() { dc_trusted "$@"; }
fi
```

3. Для snapshot с Compose metadata проверь имена и реши, устанавливать ли exact
   recovered inputs. Содержимое файлов не печатается. Legacy этой ветки не
   касается.

```bash
if [ -f "$RESTORE_DIR/compose/files.list" ]; then
    while IFS= read -r name; do
        if cmp -s "$PROJECT_DIR/$name" "$RESTORE_DIR/compose/$name"; then
            printf 'unchanged: %s\n' "$name"
        else
            printf 'changed: %s\n' "$name"
        fi
    done < "$RESTORE_DIR/compose/files.list"
    printf 'Install exact recovered Compose files into %s? Type YES: ' "$PROJECT_DIR"
    read -r INSTALL_COMPOSE
    if [ "$INSTALL_COMPOSE" = YES ]; then
        while IFS= read -r name; do
            install -m 600 "$RESTORE_DIR/compose/$name" "$PROJECT_DIR/$name"
        done < "$RESTORE_DIR/compose/files.list"
        while IFS= read -r name; do
            cmp -s "$PROJECT_DIR/$name" "$RESTORE_DIR/compose/$name" || {
                echo "root Compose input diverged: $name" >&2
                exit 1
            }
        done < "$RESTORE_DIR/compose/files.list"
    else
        capture=$(bash docker/backup/compose-recovery.sh --capture-status "$RESTORE_DIR/.env")
        case "$capture" in
            disabled) ;;
            enabled)
                echo 'BACKUP_COMPOSE_FILES would capture stale root files; set it empty in the recovered .env before continuing' >&2
                exit 1
                ;;
            *)
                echo 'cannot determine BACKUP_COMPOSE_FILES capture status' >&2
                exit 1
                ;;
        esac
    fi
fi
```

4. Установи восстановленный `.env` только после решения по Compose inputs:

```bash
install -m 600 "$RESTORE_DIR/.env" .env
dc_recover --profile backup build bot backup
```

5. Preflight и запуск базовых сервисов. `dc_recover` already includes
   `PREFLIGHT_ARGS`: no panel network means a no-op for a test/local project; panel
   deployment sets `PANEL_NETWORK_REQUIRED=1`. The helper never creates networks.

```bash
dc_recover up -d --no-deps postgres redis
dc_recover exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d postgres'
dc_recover exec -T redis redis-cli ping
```

6. Дальше выполни разделы 2 и 3: preflight фактической БД, restore в свежую БД,
   cutover A/B/C. На новом хосте `ORIGINAL_DB` — БД из восстановленного `.env`;
   старой населённой БД может не быть, но `.env` и rollback-копию всё равно
   сохраняем.

## Важно

- `BACKUP_COMPOSE_FILES` — явный allowlist. Production-пример:
  `COMPOSE_PROJECT_NAME=bot-asteria` и
  `BACKUP_COMPOSE_FILES=docker-compose.yml:docker-compose.local.yml:docker-compose.asteria.yml`.
  Восстановленные Compose-файлы лежат только в приватном restore-каталоге; не
  коммить private override и не применяй их поверх запущенного stack. Project
  проверяется helper'ом до записи файлов.
- Panel deployment: `remnawave-network` должен быть `external: true` из
  operator-maintained override. Checked-in `docker-compose.local.yml` объявляет
  Compose-managed сеть с тем же именем и не является panel network. Preflight
  проверяет effective config, existence и project label/ручное подтверждение;
  `docker network inspect remnawave-network` не меняет сеть. Если сети нет,
  согласуй её с оператором панели; не создавай shared panel network из runbook.
  Снапшот не содержит Docker network state и не требует Docker socket; не меняй
  network wiring в Compose ради восстановления.
- Healthcheck проверяет свежесть маркера, а не возможность восстановления.
  Периодически делай restore drill.
- Пароль Restic потерян — снапшоты не расшифровать. Храни его офлайн.
- Не запускай `pg_restore --clean` против рабочей базы.
- Не меняй `BACKUP_RESTIC_REPOSITORY` на старую папку `remnawave-bot`: новый
  сервис рассчитан только на отдельный Restic-репозиторий.
