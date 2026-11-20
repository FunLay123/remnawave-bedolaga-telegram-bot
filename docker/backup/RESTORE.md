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

## 0. Общие функции восстановления

Функции ниже читают проверенный `compose/files.list` построчно в Bash-массив.
Никакого `eval`, `source` manifest'а, `$(cat ...)` и неявного word splitting:
только `read -r` и явные элементы `DC`. Это работает для одного файла и для
любого корректного project name.

```bash
validate_compose_project() {
    [[ ${1:-} =~ ^[a-z0-9][a-z0-9_-]*$ ]]
}

valid_compose_name() {
    [[ ${1:-} =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml$ ]]
}

compose_from_repo() {
    local project_dir=$1 project_name=$2
    local name seen=' ' count=0
    shift 2
    validate_compose_project "$project_name" || {
        echo "invalid project: $project_name" >&2
        return 1
    }
    DC=(docker compose --project-directory "$project_dir" -p "$project_name")
    for name in "$@"; do
        valid_compose_name "$name" || {
            echo "invalid Compose input: $name" >&2
            return 1
        }
        case "$seen" in
            *" $name "*)
                echo "duplicate Compose input: $name" >&2
                return 1
                ;;
        esac
        seen="$seen$name "
        if [ ! -f "$project_dir/$name" ] || [ -L "$project_dir/$name" ] || [ ! -s "$project_dir/$name" ]; then
            echo "missing, empty or symlinked Compose input: $name" >&2
            return 1
        fi
        DC+=(-f "$project_dir/$name")
        count=$((count + 1))
    done
    [ "$count" -gt 0 ] || {
        echo 'no Compose inputs selected' >&2
        return 1
    }
}

compose_from_restore() {
    local restore_dir=$1 project_name=$2 project_dir=$3
    local recorded name seen=' ' count=0
    validate_compose_project "$project_name" || {
        echo "invalid project: $project_name" >&2
        return 1
    }
    IFS= read -r recorded < "$restore_dir/compose/project-name" || {
        echo 'missing compose/project-name' >&2
        return 1
    }
    if [ "$recorded" != "$project_name" ]; then
        echo "project mismatch: archive '$recorded', expected '$project_name'" >&2
        return 1
    fi
    DC=(docker compose --project-directory "$project_dir" -p "$project_name")
    while IFS= read -r name || [ -n "$name" ]; do
        valid_compose_name "$name" || {
            echo "invalid Compose input: $name" >&2
            return 1
        }
        case "$seen" in
            *" $name "*)
                echo "duplicate Compose input: $name" >&2
                return 1
                ;;
        esac
        seen="$seen$name "
        if [ ! -f "$restore_dir/compose/$name" ] || [ -L "$restore_dir/compose/$name" ] || [ ! -s "$restore_dir/compose/$name" ]; then
            echo "missing, empty or symlinked restored Compose input: $name" >&2
            return 1
        fi
        DC+=(-f "$restore_dir/compose/$name")
        count=$((count + 1))
    done < "$restore_dir/compose/files.list"
    [ "$count" -gt 0 ] || {
        echo 'restored Compose file list is empty' >&2
        return 1
    }
}

preflight_panel_network() {
    local config network_labels project_label PANEL_NETWORK_CONFIRM
    config=$("${DC[@]}" config) || {
        echo 'cannot render effective Compose config' >&2
        return 1
    }
    if ! printf '%s\n' "$config" | awk '
        BEGIN { in_networks = 0; in_target = 0; external = 0; found = 0; bad_name = 0 }
        /^networks:[[:space:]]*$/ { in_networks = 1; next }
        in_networks && /^[^[:space:]]/ { in_networks = 0; in_target = 0 }
        in_networks && /^  remnawave-network:[[:space:]]*$/ { in_target = 1; found = 1; next }
        in_target && /^  [^[:space:]]/ { in_target = 0 }
        in_target && /^    external:[[:space:]]*true[[:space:]]*$/ { external = 1 }
        in_target && /^    name:/ {
            name = $0
            sub(/^    name:[[:space:]]*/, "", name)
            gsub(/["[:space:]]/, "", name)
            if (name != "remnawave-network") bad_name = 1
        }
        END { exit !(found && external && !bad_name) }
    '; then
        echo 'remnawave-network must be external: true via an operator-maintained override' >&2
        return 1
    fi
    network_labels=$(docker network inspect remnawave-network --format '{{json .Labels}}' 2>/dev/null) || {
        echo 'shared remnawave-network is missing; verify it with the panel operator; do not create it here' >&2
        return 1
    }
    project_label=$(docker network inspect remnawave-network \
        --format '{{index .Labels "com.docker.compose.project"}}' 2>/dev/null || true)
    if [ -n "${EXPECTED_PANEL_PROJECT:-}" ]; then
        if [ "$project_label" != "$EXPECTED_PANEL_PROJECT" ]; then
            echo "network project label '$project_label' != '$EXPECTED_PANEL_PROJECT'; stop" >&2
            return 1
        fi
    else
        printf 'network labels: %s\n' "$network_labels"
        printf 'Type YES to confirm this is the panel-managed remnawave-network: '
        read -r PANEL_NETWORK_CONFIRM
        if [ "$PANEL_NETWORK_CONFIRM" != YES ]; then
            return 1
        fi
    fi
}
```

`compose_from_repo "$PROJECT_DIR" "$EXPECTED_COMPOSE_PROJECT" "${TRUSTED_COMPOSE_FILES[@]}"`
используется только для legacy/trusted файлов; после обычного restore порядок
берётся из `compose/files.list`. `--project-directory "$PROJECT_DIR"` задаёт
base path для `.env`, bind mounts и `build: .`.

## 1. Остановить writers и прочитать снапшот

Чтение — одноразовым контейнером с entrypoint `restic`, поэтому backup loop и
prune не запускаются. Это работает и без уже запущенного backup-контейнера.

```bash
set -euo pipefail
umask 077

PROJECT_DIR=$(pwd -P)
if [ -z "${EXPECTED_COMPOSE_PROJECT:-}" ]; then
    printf 'Expected Compose project: '
    read -r EXPECTED_COMPOSE_PROJECT
fi
export EXPECTED_COMPOSE_PROJECT

# Set exactly the ordered inputs of the existing live project. Do not add a
# file merely because it exists; do not rename the live project.
REPO_COMPOSE_FILES=(docker-compose.yml docker-compose.local.yml docker-compose.asteria.yml)

RECOVERY_ROOT=${RECOVERY_ROOT:-/var/lib/remnawave-recovery}
install -d -m 700 "$RECOVERY_ROOT"
RESTORE_DIR="$RECOVERY_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 700 "$RESTORE_DIR"

# Never create or target a second project by accident.
if [ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$EXPECTED_COMPOSE_PROJECT")" ]; then
    echo "no containers for project '$EXPECTED_COMPOSE_PROJECT'; verify the live project name" >&2
    exit 1
fi

compose_from_repo "$PROJECT_DIR" "$EXPECTED_COMPOSE_PROJECT" "${REPO_COMPOSE_FILES[@]}"
"${DC[@]}" stop bot backup

"${DC[@]}" --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache snapshots
# check запускай, когда нет активного backup-writer. При locked сначала
# убедись, что writer не работает; unlock вручную.
"${DC[@]}" --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache check
if [ -z "${SNAPSHOT_ID:-}" ]; then
    printf 'Snapshot ID: '
    read -r SNAPSHOT_ID
fi
[ -n "$SNAPSHOT_ID" ] || { echo 'SNAPSHOT_ID is required' >&2; exit 1; }

"${DC[@]}" --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache ls "$SNAPSHOT_ID"
"${DC[@]}" --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache dump "$SNAPSHOT_ID" remnawave-backup.tar \
  > "$RESTORE_DIR/remnawave-backup.tar"

# Read-only: покажет project/order или явный legacy notice.
sh docker/backup/restore-compose.sh \
  --archive "$RESTORE_DIR/remnawave-backup.tar" \
  --inspect

# Выбери по выводу inspect. Legacy 16e67daf не содержит Compose metadata и
# допускается только через явный data-only путь.
printf 'Snapshot kind [compose/legacy]: '
read -r SNAPSHOT_KIND
if [ "$SNAPSHOT_KIND" = compose ]; then
    sh docker/backup/restore-compose.sh \
      --archive "$RESTORE_DIR/remnawave-backup.tar" \
      --project "$EXPECTED_COMPOSE_PROJECT" \
      --output "$RESTORE_DIR"
elif [ "$SNAPSHOT_KIND" = legacy ]; then
    sh docker/backup/restore-compose.sh \
      --archive "$RESTORE_DIR/remnawave-backup.tar" \
      --legacy-data-only \
      --output "$RESTORE_DIR"
else
    echo 'SNAPSHOT_KIND must be compose or legacy' >&2
    exit 1
fi
# Для legacy оставь DC из compose_from_repo: проверенные live-файлы. Не
# подставляй override "на глаз"; automatic fallback отсутствует.
if [ -f "$RESTORE_DIR/compose/files.list" ]; then
    compose_from_restore "$RESTORE_DIR" "$EXPECTED_COMPOSE_PROJECT" "$PROJECT_DIR"
    # Existing host: recovered inputs must match the reviewed live files. Stop
    # instead of silently reconfiguring the running site.
    while IFS= read -r name; do
        cmp -s "$PROJECT_DIR/$name" "$RESTORE_DIR/compose/$name" || {
            echo "recovered Compose input differs from live: $name; stop" >&2
            exit 1
        }
    done < "$RESTORE_DIR/compose/files.list"
fi

cat "$RESTORE_DIR/compose/project-name" 2>/dev/null || true
cat "$RESTORE_DIR/compose/files.list" 2>/dev/null || true
file "$RESTORE_DIR/db.dump"
"${DC[@]}" exec -T postgres pg_restore --list < "$RESTORE_DIR/db.dump" >/dev/null
```

Helper не использует `tar -xf`: он проверяет точный набор членов и типы файлов,
стримит только `db.dump`, `.env` и `compose/*` в приватный scratch, а результат
кладёт в `$RESTORE_DIR` лишь после совпадения `compose/project-name` с ожидаемым
project. При mismatch `$RESTORE_DIR` не получает ни данных, ни Compose-файлов.
Для снапшота с Compose metadata `compose/files.list` задаёт авторитетный порядок,
`compose/project-name` — project ID. Стабильный `$RESTORE_DIR` хранит и сам
`remnawave-backup.tar`; не удаляй его до проверки. Плейнтекст tar/dump не
кладётся в репозиторий. Просмотри входы и применяй их только через `DC`.

## 2. Зафиксировать фактическую БД и восстановить в свежую

Целевую БД берём из **preflight effective app Compose config** (bot и backup), а
не из env-файла контейнера `postgres`: после earlier `--no-deps` cutover env
postgres-контейнера может остаться старым. Rollback expected DB — это
`$ORIGINAL_DB`, полученный этим preflight. Если preflight не совпадает с тем,
с чем фактически работал бот, сначала приведи `.env` в соответствие и не начинай
cutover.

```bash
unset POSTGRES_DB

ORIGINAL_BOT_DB=$("${DC[@]}" --profile backup run --rm --no-deps -T \
  --entrypoint sh bot -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
ORIGINAL_BACKUP_DB=$("${DC[@]}" --profile backup run --rm --no-deps -T \
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
  "${DC[@]}" exec -T postgres sh -c \
    'psql -v ON_ERROR_STOP=1 -v restore_db="$1" -U "$POSTGRES_USER" -d postgres' \
    sh "$RESTORE_DB"

"${DC[@]}" exec -T postgres sh -c \
  'pg_restore --single-transaction --exit-on-error --no-owner --no-privileges \
     -U "$POSTGRES_USER" -d "$1"' sh "$RESTORE_DB" \
  < "$RESTORE_DIR/db.dump"
```

`--single-transaction` откатывает всё при первой ошибке. `--exit-on-error` не
даёт проглотить ошибку. `ORIGINAL_DB` берётся из preflight effective app config.

Проверь:

```bash
"${DC[@]}" exec -T postgres sh -c \
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
"${DC[@]}" stop bot backup

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
PRE_BOT_DB=$("${DC[@]}" --profile backup run --rm --no-deps -T \
  --entrypoint sh bot -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
PRE_BACKUP_DB=$("${DC[@]}" --profile backup run --rm --no-deps -T \
  --entrypoint sh backup -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'preflight cutover bot=%s backup=%s\n' "$PRE_BOT_DB" "$PRE_BACKUP_DB"
[ "$PRE_BOT_DB" = "$RESTORE_DB" ] && [ "$PRE_BACKUP_DB" = "$RESTORE_DB" ] || {
  echo 'preflight DB mismatch; fix .env, do not start' >&2
  exit 1
}

"${DC[@]}" up -d --no-deps --force-recreate bot backup

BOT_DB=$("${DC[@]}" exec -T bot sh -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
BACKUP_DB=$("${DC[@]}" exec -T backup sh -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'effective bot=%s backup=%s\n' "$BOT_DB" "$BACKUP_DB"
[ "$BOT_DB" = "$RESTORE_DB" ] && [ "$BACKUP_DB" = "$RESTORE_DB" ] || {
  echo 'effective DB mismatch; stop and fix config before continuing' >&2
  exit 1
}
```

### C. Rollback: вернуть исходный `.env` из A и проверить

```bash
"${DC[@]}" stop bot backup
[ -f "$ENV_BACKUP" ] || { echo 'missing rollback .env; run block A first' >&2; exit 1; }
unset POSTGRES_DB
install -m 600 "$ENV_BACKUP" .env

PRE_BOT_DB=$("${DC[@]}" --profile backup run --rm --no-deps -T \
  --entrypoint sh bot -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
PRE_BACKUP_DB=$("${DC[@]}" --profile backup run --rm --no-deps -T \
  --entrypoint sh backup -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'preflight rollback bot=%s backup=%s\n' "$PRE_BOT_DB" "$PRE_BACKUP_DB"
[ "$PRE_BOT_DB" = "$ORIGINAL_DB" ] && [ "$PRE_BACKUP_DB" = "$ORIGINAL_DB" ] || {
  echo 'rollback preflight mismatch; fix .env, do not start' >&2
  exit 1
}

"${DC[@]}" up -d --no-deps --force-recreate bot backup

BOT_DB=$("${DC[@]}" exec -T bot sh -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
BACKUP_DB=$("${DC[@]}" exec -T backup sh -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
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
2. Собери/скачай образы `bot` и `backup`. `EXPECTED_COMPOSE_PROJECT` — выбор
   оператора; `REPO_COMPOSE_FILES` — ровно те файлы, которые уже лежат в project
   root и используются только для чтения Restic:

```bash
set -euo pipefail
umask 077

PROJECT_DIR=$(pwd -P)
if [ -z "${EXPECTED_COMPOSE_PROJECT:-}" ]; then
    printf 'Expected Compose project: '
    read -r EXPECTED_COMPOSE_PROJECT
fi
export EXPECTED_COMPOSE_PROJECT
REPO_COMPOSE_FILES=(docker-compose.yml docker-compose.local.yml)
# REPO_COMPOSE_FILES+=(docker-compose.panel-network.yml)

compose_from_repo "$PROJECT_DIR" "$EXPECTED_COMPOSE_PROJECT" "${REPO_COMPOSE_FILES[@]}"
"${DC[@]}" --profile backup build bot backup
```

3. Пока app `.env` ещё не установлен, прочитай снапшот. Выполняй шаги 3–6 в
   одном shell: `RESTORE_DIR`, `RESTORE_DB` и `ORIGINAL_DB` должны сохраниться.
   При кастомных repo/путях экспортируй их для этого shell; иначе действуют
   defaults из compose:

```bash
RECOVERY_ROOT=${RECOVERY_ROOT:-/var/lib/remnawave-recovery}
install -d -m 700 "$RECOVERY_ROOT"
RESTORE_DIR="$RECOVERY_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 700 "$RESTORE_DIR"
# export BACKUP_RESTIC_REPOSITORY='rclone:gdrive:remnawave-restic'
# export BACKUP_RCLONE_CONFIG_DIR='/etc/remnawave-backup/rclone'
# export BACKUP_RESTIC_PASSWORD_FILE='/etc/remnawave-backup/restic-password'

"${DC[@]}" --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache snapshots
if [ -z "${SNAPSHOT_ID:-}" ]; then
    printf 'Snapshot ID: '
    read -r SNAPSHOT_ID
fi
[ -n "$SNAPSHOT_ID" ] || { echo 'SNAPSHOT_ID is required' >&2; exit 1; }

"${DC[@]}" --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache check
"${DC[@]}" --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache dump "$SNAPSHOT_ID" remnawave-backup.tar \
  > "$RESTORE_DIR/remnawave-backup.tar"

# Read-only: покажет project/order или явный legacy notice.
sh docker/backup/restore-compose.sh \
  --archive "$RESTORE_DIR/remnawave-backup.tar" \
  --inspect

# Выбери по выводу inspect. Legacy 16e67daf не содержит Compose metadata и
# допускается только через явный data-only путь.
printf 'Snapshot kind [compose/legacy]: '
read -r SNAPSHOT_KIND
if [ "$SNAPSHOT_KIND" = compose ]; then
    sh docker/backup/restore-compose.sh \
      --archive "$RESTORE_DIR/remnawave-backup.tar" \
      --project "$EXPECTED_COMPOSE_PROJECT" \
      --output "$RESTORE_DIR"
elif [ "$SNAPSHOT_KIND" = legacy ]; then
    sh docker/backup/restore-compose.sh \
      --archive "$RESTORE_DIR/remnawave-backup.tar" \
      --legacy-data-only \
      --output "$RESTORE_DIR"
else
    echo 'SNAPSHOT_KIND must be compose or legacy' >&2
    exit 1
fi

file "$RESTORE_DIR/db.dump"

if [ -f "$RESTORE_DIR/compose/files.list" ]; then
    compose_from_restore "$RESTORE_DIR" "$EXPECTED_COMPOSE_PROJECT" "$PROJECT_DIR"
else
    # Legacy 16e67daf: оператор явно выбирает доверенные файлы. Не подставляй
    # override "на глаз" и не считай наличие файла разрешением его использовать.
    TRUSTED_COMPOSE_FILES=(docker-compose.yml docker-compose.local.yml)
    # TRUSTED_COMPOSE_FILES+=(docker-compose.panel-network.yml)
    compose_from_repo "$PROJECT_DIR" "$EXPECTED_COMPOSE_PROJECT" "${TRUSTED_COMPOSE_FILES[@]}"
fi
```

4. До установки `.env` проверь recovered Compose inputs. Не перезаписывай
   существующие файлы project root слепо. Если не устанавливаешь exact-файлы,
   оставь capture выключенным (`BACKUP_COMPOSE_FILES=`) до ручного восстановления
   root inputs; иначе следующий backup молча снимет stale root Compose files.

```bash
if [ -d "$RESTORE_DIR/compose" ]; then
    while IFS= read -r name; do
        diff -u "$PROJECT_DIR/$name" "$RESTORE_DIR/compose/$name" || true
    done < "$RESTORE_DIR/compose/files.list"
fi
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
    echo 'Compose files were not installed; keep BACKUP_COMPOSE_FILES= empty in the recovered .env.' >&2
fi
```

Проверь и установи восстановленный `.env` только после этого решения:

```bash
if [ "$INSTALL_COMPOSE" != YES ]; then
    grep -q '^BACKUP_COMPOSE_FILES=$' "$RESTORE_DIR/.env" || {
        echo 'Set BACKUP_COMPOSE_FILES= in the recovered .env before installing it' >&2
        exit 1
    }
fi
install -m 600 "$RESTORE_DIR/.env" .env
```

5. Preflight effective Compose config и existing shared network **до** `up`.
   Функция не создаёт и не меняет сети. `external: true` должен приходить из
   operator-maintained override, а не из checked-in `docker-compose.local.yml`:
   локальный override создаёт Compose-managed сеть с тем же именем. Если сети
   нет, остановись и согласуй её с оператором панели. Частое предупреждение на
   текущем deployment не даёт разрешения менять сеть.

```bash
# EXPECTED_PANEL_PROJECT='<panel-compose-project>'
preflight_panel_network

"${DC[@]}" up -d --no-deps postgres redis
"${DC[@]}" exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d postgres'
"${DC[@]}" exec -T redis redis-cli ping
```

6. Дальше выполни шаги 2 и 3: preflight фактической БД, restore в свежую БД,
   cutover A/B/C. На новом хосте `$ORIGINAL_DB` — БД из восстановленного `.env`;
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
