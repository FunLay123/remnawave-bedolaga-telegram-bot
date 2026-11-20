#!/bin/sh
# Optional PostgreSQL + .env backup sidecar.
#
# One interval = one atomic Restic snapshot.  The dump and the current .env are
# staged together in a private directory and published as a single tar stream,
# so a snapshot can never contain only half of the pair.  Failed intervals are
# not queued: the next interval starts a fresh dump.
set -u
umask 077
export LC_ALL=C

ENV_FILE=${ENV_FILE:-/src/.env}
STAGE_DIR=${BACKUP_RESTIC_STAGE_DIR:-/run/remnawave-backup}
STATE_DIR=${BACKUP_RESTIC_STATE_DIR:-/backups/restic/state}
MARKER="$STATE_DIR/last_success"
MAINTENANCE_DAY_FILE="$STATE_DIR/last_maintenance_day"
LOCK_FILE="$STATE_DIR/operation.lock"
LOOP_LOCK_FILE="$STATE_DIR/loop.lock"

INTERVAL_SECONDS=${BACKUP_RESTIC_INTERVAL_SECONDS:-300}
KEEP_LAST=${BACKUP_RESTIC_KEEP_LAST:-12}
KEEP_HOURLY=${BACKUP_RESTIC_KEEP_HOURLY:-24}
KEEP_DAILY=${BACKUP_RESTIC_KEEP_DAILY:-30}
KEEP_WEEKLY=${BACKUP_RESTIC_KEEP_WEEKLY:-12}
KEEP_MONTHLY=${BACKUP_RESTIC_KEEP_MONTHLY:-12}
MAINTENANCE_ENABLED=${BACKUP_RESTIC_MAINTENANCE_ENABLED:-true}
MAINTENANCE_TIME=${BACKUP_RESTIC_MAINTENANCE_TIME:-03:00}
HEALTHCHECK_MAX_AGE_SECONDS=${BACKUP_RESTIC_HEALTHCHECK_MAX_AGE_SECONDS:-900}
TMPFS_SIZE=${BACKUP_RESTIC_TMPFS_SIZE:-256m}
MIN_FREE_MB=${BACKUP_RESTIC_MIN_FREE_MB:-16}
REQUIRE_TMPFS=${BACKUP_RESTIC_REQUIRE_TMPFS:-true}
COMPOSE_FILES=${BACKUP_COMPOSE_FILES:-}
COMPOSE_PROJECT=${BACKUP_COMPOSE_PROJECT:-}

# Standard Restic overrides win.  The fallback is deliberately a dedicated
# repository, never the legacy remnawave-bot/ GFS folder.
RESTIC_REPOSITORY=${RESTIC_REPOSITORY:-${BACKUP_RESTIC_REPOSITORY:-rclone:gdrive:remnawave-restic}}
export RESTIC_REPOSITORY

RESTIC_HOST=${BACKUP_RESTIC_HOST:-remnawave-backup}
RESTIC_GROUP_BY=${BACKUP_RESTIC_GROUP_BY:-host,tags}
RESTIC_TAG=${BACKUP_RESTIC_TAG:-remnawave-backup}

if [ -n "${RCLONE_CONFIG_DIR:-}" ] && [ -z "${RCLONE_CONFIG:-}" ]; then
    RCLONE_CONFIG="$RCLONE_CONFIG_DIR/rclone.conf"
    export RCLONE_CONFIG
fi

MODE=loop
DRY_RUN=0
SHUTDOWN=0
WORK_DIR=
LOCK_DIR=
LOOP_LOCK_DIR=
OPERATION_LOCK_HELD=0
LOOP_LOCK_HELD=0
SLEEP_PID=
TMPFS_OVERRIDE_WARNED=0
MAINTENANCE_RAN_DAY=

log() {
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

error() {
    printf '[%s] ERROR: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

valid_uint() {
    value=$1
    min=$2
    max=$3
    case "$value" in
        ''|*[!0-9]*|0*) return 1 ;;
    esac
    [ "${#value}" -le 9 ] || return 1
    [ "$value" -ge "$min" ] && [ "$value" -le "$max" ]
}

valid_tmpfs_size() {
    size=$1
    case "$size" in
        [0-9]*[mMgG]) ;;
        *) return 1 ;;
    esac
    number=${size%?}
    unit=${size#"$number"}
    case "$number" in
        ''|*[!0-9]*|0*) return 1 ;;
    esac
    case "$unit" in
        m|M) [ "$number" -ge 16 ] && [ "$number" -le 1024 ] ;;
        g|G) [ "$number" -eq 1 ] ;;
        *) return 1 ;;
    esac
}

valid_time() {
    case "${1:-}" in
        [0-1][0-9]:[0-5][0-9]|2[0-3]:[0-5][0-9]) return 0 ;;
        *) return 1 ;;
    esac
}

valid_bool() {
    case "${1:-}" in
        true|false) return 0 ;;
        *) return 1 ;;
    esac
}

valid_host() {
    case "${1:-}" in
        ''|*[!A-Za-z0-9._-]*) return 1 ;;
    esac
    return 0
}

valid_compose_project() {
    case "${1:-}" in
        ''|*[!a-z0-9_-]*) return 1 ;;
        [a-z0-9]*) return 0 ;;
        *) return 1 ;;
    esac
}

valid_compose_basename() {
    case "${1:-}" in
        ''|.|..|*/*|*[!A-Za-z0-9._-]*) return 1 ;;
        [A-Za-z0-9]*.yml|*.yaml) ;;
        *) return 1 ;;
    esac
    # The first character must not make the name look like a tar option.
    case "${1:-}" in
        [A-Za-z0-9]*) return 0 ;;
        *) return 1 ;;
    esac
}

check_uint() {
    if ! valid_uint "$2" "$3" "$4"; then
        error "invalid $1=$2; expected an integer from $3 to $4"
        return 1
    fi
}

legacy_repository() {
    case "${1:-}" in
        *:remnawave-bot|*:remnawave-bot/|*:remnawave-bot/*) return 0 ;;
        *) return 1 ;;
    esac
}

check_compose_config() {
    [ -n "$COMPOSE_FILES" ] || return 0

    if ! valid_compose_project "$COMPOSE_PROJECT"; then
        error "BACKUP_COMPOSE_FILES is set but BACKUP_COMPOSE_PROJECT='$COMPOSE_PROJECT' is not a valid lowercase Compose project name; set COMPOSE_PROJECT_NAME or pass -p explicitly"
        return 1
    fi

    case "$COMPOSE_FILES" in
        *[!A-Za-z0-9._:-]*)
            error "invalid BACKUP_COMPOSE_FILES='$COMPOSE_FILES'; use colon-separated Compose YAML basenames"
            return 1
            ;;
    esac
    case "$COMPOSE_FILES" in
        :*|*:|*::*)
            error "invalid BACKUP_COMPOSE_FILES='$COMPOSE_FILES'; empty list entry"
            return 1
            ;;
    esac

    env_dir=$(dirname "$ENV_FILE")
    seen=' '
    old_ifs=$IFS
    IFS=:
    set -f
    set -- $COMPOSE_FILES
    set +f
    IFS=$old_ifs

    for name do
        if ! valid_compose_basename "$name"; then
            error "invalid Compose input '$name'; expected a unique .yml/.yaml basename in $env_dir"
            return 1
        fi
        case "$seen" in
            *" $name "*)
                error "duplicate Compose input '$name' in BACKUP_COMPOSE_FILES"
                return 1
                ;;
        esac
        seen="$seen$name "

        source_file="$env_dir/$name"
        if [ -L "$source_file" ]; then
            error "Compose input is a symlink, refusing to follow it: $source_file"
            return 1
        fi
        if [ ! -f "$source_file" ]; then
            error "Compose input is missing or not a regular file: $source_file"
            return 1
        fi
        if [ ! -r "$source_file" ]; then
            error "Compose input is unreadable: $source_file"
            return 1
        fi
        if [ ! -s "$source_file" ]; then
            error "Compose input is empty: $source_file"
            return 1
        fi
    done
}

validate_config() {
    if [ -z "$ENV_FILE" ]; then
        error 'ENV_FILE is empty'
        return 1
    fi
    if [ "$MODE" != maintenance ]; then
        check_compose_config || return 1
    fi
    if [ -z "$RESTIC_REPOSITORY" ]; then
        error 'RESTIC_REPOSITORY / BACKUP_RESTIC_REPOSITORY is empty'
        return 1
    fi
    if legacy_repository "$RESTIC_REPOSITORY"; then
        error "refusing legacy repository path '$RESTIC_REPOSITORY'; use a dedicated restic repository"
        return 1
    fi
    if ! valid_host "$RESTIC_HOST"; then
        error "invalid BACKUP_RESTIC_HOST=$RESTIC_HOST"
        return 1
    fi
    check_uint BACKUP_RESTIC_INTERVAL_SECONDS "$INTERVAL_SECONDS" 1 86400 || return 1
    check_uint BACKUP_RESTIC_KEEP_LAST "$KEEP_LAST" 1 9999 || return 1
    check_uint BACKUP_RESTIC_KEEP_HOURLY "$KEEP_HOURLY" 1 9999 || return 1
    check_uint BACKUP_RESTIC_KEEP_DAILY "$KEEP_DAILY" 1 9999 || return 1
    check_uint BACKUP_RESTIC_KEEP_WEEKLY "$KEEP_WEEKLY" 1 9999 || return 1
    check_uint BACKUP_RESTIC_KEEP_MONTHLY "$KEEP_MONTHLY" 1 9999 || return 1
    check_uint BACKUP_RESTIC_HEALTHCHECK_MAX_AGE_SECONDS "$HEALTHCHECK_MAX_AGE_SECONDS" 1 604800 || return 1
    check_uint BACKUP_RESTIC_MIN_FREE_MB "$MIN_FREE_MB" 1 1024 || return 1
    if ! valid_tmpfs_size "$TMPFS_SIZE"; then
        error "invalid BACKUP_RESTIC_TMPFS_SIZE=$TMPFS_SIZE; use 16m..1024m or 1g"
        return 1
    fi
    if ! valid_time "$MAINTENANCE_TIME"; then
        error "invalid BACKUP_RESTIC_MAINTENANCE_TIME=$MAINTENANCE_TIME; use HH:MM"
        return 1
    fi
    if ! valid_bool "$MAINTENANCE_ENABLED"; then
        error "invalid BACKUP_RESTIC_MAINTENANCE_ENABLED=$MAINTENANCE_ENABLED; use true or false"
        return 1
    fi
    if ! valid_bool "$REQUIRE_TMPFS"; then
        error "invalid BACKUP_RESTIC_REQUIRE_TMPFS=$REQUIRE_TMPFS; use true or false"
        return 1
    fi

    if [ -n "${RESTIC_PASSWORD_FILE:-}" ]; then
        if [ ! -f "$RESTIC_PASSWORD_FILE" ] || [ ! -r "$RESTIC_PASSWORD_FILE" ] || [ ! -s "$RESTIC_PASSWORD_FILE" ]; then
            error "RESTIC_PASSWORD_FILE is missing, unreadable or empty: $RESTIC_PASSWORD_FILE"
            return 1
        fi
    elif [ -z "${RESTIC_PASSWORD:-}" ]; then
        error 'set RESTIC_PASSWORD_FILE (preferred) or RESTIC_PASSWORD'
        return 1
    fi

    case "$RESTIC_REPOSITORY" in
        rclone:*)
            if [ -z "${RCLONE_CONFIG:-}" ]; then
                error 'rclone repository configured but RCLONE_CONFIG / RCLONE_CONFIG_DIR is empty'
                return 1
            fi
            if [ ! -f "$RCLONE_CONFIG" ] || [ ! -r "$RCLONE_CONFIG" ] || [ ! -s "$RCLONE_CONFIG" ]; then
                error "rclone config is missing, unreadable, empty or not a regular file: $RCLONE_CONFIG"
                return 1
            fi
            ;;
    esac

    for command_name in pg_dump restic tar; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            error "required command not found: $command_name"
            return 1
        fi
    done
}

prepare_state() {
    if ! mkdir -p "$STATE_DIR"; then
        error "cannot create state directory: $STATE_DIR"
        return 1
    fi
    chmod 700 "$STATE_DIR" 2>/dev/null || true
}

prepare_stage() {
    if ! mkdir -p "$STAGE_DIR"; then
        error "cannot create stage directory: $STAGE_DIR"
        return 1
    fi
    chmod 700 "$STAGE_DIR" 2>/dev/null || true

    # A SIGKILL can leave a private work directory behind.  A tmpfs normally
    # clears it on container restart; this also covers a long-lived container.
    # The lock is already held, so a concurrently starting process can't remove
    # another process's stage.
    for stale in "$STAGE_DIR"/work.*; do
        [ -e "$stale" ] || continue
        rm -rf "$stale"
    done

    if [ "$REQUIRE_TMPFS" = true ]; then
        if [ ! -r /proc/mounts ]; then
            error "cannot verify that $STAGE_DIR is a tmpfs mount; refusing unbounded staging"
            return 1
        fi
        if ! grep -q "[[:space:]]${STAGE_DIR}[[:space:]]tmpfs[[:space:]]" /proc/mounts 2>/dev/null; then
            error "$STAGE_DIR is not a tmpfs mount; set BACKUP_RESTIC_REQUIRE_TMPFS=false only for synthetic tests"
            return 1
        fi
    elif [ "$TMPFS_OVERRIDE_WARNED" -eq 0 ]; then
        log "WARN: tmpfs enforcement disabled (synthetic/test override); stage=$STAGE_DIR"
        TMPFS_OVERRIDE_WARNED=1
    fi
}

check_free_space() {
    free_kb=$(df -Pk "$STAGE_DIR" 2>/dev/null | awk 'NR == 2 { print $4 }')
    case "${free_kb:-}" in
        ''|*[!0-9]*)
            error "cannot determine free space for $STAGE_DIR"
            return 1
            ;;
    esac
    need_kb=$((MIN_FREE_MB * 1024))
    if [ "$free_kb" -lt "$need_kb" ]; then
        error "not enough free space in $STAGE_DIR: ${free_kb} KiB available, ${need_kb} KiB required"
        return 1
    fi
}

acquire_dir_lock() {
    lock_dir=$1
    wait_mode=$2
    while :; do
        if mkdir "$lock_dir" 2>/dev/null; then
            printf '%s\n' "$$" > "$lock_dir/pid"
            return 0
        fi

        pid=$(cat "$lock_dir/pid" 2>/dev/null || true)
        case "${pid:-}" in
            ''|*[!0-9]*) pid=0 ;;
        esac
        if [ "$pid" -gt 0 ] && kill -0 "$pid" 2>/dev/null; then
            if [ "$wait_mode" = wait ]; then
                sleep 1
                [ "$SHUTDOWN" -eq 0 ] || return 1
                continue
            fi
            error 'another backup process is already running'
            return 1
        fi

        # Stale lock left by SIGKILL / container restart.
        rm -rf "$lock_dir"
        if ! mkdir "$lock_dir" 2>/dev/null; then
            error "cannot acquire lock $lock_dir"
            return 1
        fi
        printf '%s\n' "$$" > "$lock_dir/pid"
        return 0
    done
}

acquire_operation_lock() {
    wait_mode=${1:-nowait}
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$LOCK_FILE" || {
            error "cannot open lock file $LOCK_FILE"
            return 1
        }
        if [ "$wait_mode" = wait ]; then
            if ! flock 9; then
                error "cannot acquire operation lock $LOCK_FILE"
                return 1
            fi
        elif ! flock -n 9; then
            error 'another backup operation is already running'
            return 1
        fi
        OPERATION_LOCK_HELD=1
        return 0
    fi

    # Fallback for hosts without flock (including the shell-test host).
    LOCK_DIR="$LOCK_FILE.d"
    acquire_dir_lock "$LOCK_DIR" "$wait_mode"
}

acquire_loop_lock() {
    if command -v flock >/dev/null 2>&1; then
        exec 8>"$LOOP_LOCK_FILE" || {
            error "cannot open loop lock file $LOOP_LOCK_FILE"
            return 1
        }
        if ! flock -n 8; then
            error 'another backup loop is already running'
            return 1
        fi
        LOOP_LOCK_HELD=1
        return 0
    fi

    LOOP_LOCK_DIR="$LOOP_LOCK_FILE.d"
    acquire_dir_lock "$LOOP_LOCK_DIR" nowait
}

release_operation_lock() {
    if [ "${OPERATION_LOCK_HELD:-0}" -eq 1 ]; then
        flock -u 9 2>/dev/null || true
        OPERATION_LOCK_HELD=0
    fi
    if [ -n "${LOCK_DIR:-}" ]; then
        rm -rf "$LOCK_DIR"
        LOCK_DIR=
    fi
}

release_loop_lock() {
    if [ "${LOOP_LOCK_HELD:-0}" -eq 1 ]; then
        flock -u 8 2>/dev/null || true
        LOOP_LOCK_HELD=0
    fi
    if [ -n "${LOOP_LOCK_DIR:-}" ]; then
        rm -rf "$LOOP_LOCK_DIR"
        LOOP_LOCK_DIR=
    fi
}

cleanup_work() {
    if [ -n "${WORK_DIR:-}" ] && [ -d "$WORK_DIR" ]; then
        rm -rf "$WORK_DIR"
    fi
    WORK_DIR=
}

cleanup() {
    cleanup_work
    release_operation_lock
    release_loop_lock
}

on_signal() {
    SHUTDOWN=1
    if [ -n "${SLEEP_PID:-}" ] && kill -0 "$SLEEP_PID" 2>/dev/null; then
        kill "$SLEEP_PID" 2>/dev/null || true
    fi
    log 'shutting down after current operation'
}

check_env_file() {
    if [ ! -f "$ENV_FILE" ]; then
        error "ENV_FILE $ENV_FILE is missing; refusing to create a snapshot"
        return 1
    fi
    if [ ! -r "$ENV_FILE" ]; then
        error "ENV_FILE $ENV_FILE is unreadable; refusing to create a snapshot"
        return 1
    fi
    if [ ! -s "$ENV_FILE" ]; then
        error "ENV_FILE $ENV_FILE is empty; refusing to create a snapshot"
        return 1
    fi
    if ! grep -q '[^[:space:]]' "$ENV_FILE" 2>/dev/null; then
        error "ENV_FILE $ENV_FILE is blank; refusing to create a snapshot"
        return 1
    fi
}

update_marker() {
    now=$(date +%s)
    tmp="$STATE_DIR/.last_success.$$"
    if printf '%s\n' "$now" > "$tmp" && mv -f "$tmp" "$MARKER"; then
        return 0
    fi
    rm -f "$tmp"
    error "cannot atomically update freshness marker $MARKER"
    return 1
}

backup_failed() {
    code=$1
    message=$2
    error "$message (exit $code); snapshot not accepted, freshness marker unchanged"
    cleanup_work
    return "$code"
}

stage_compose_inputs() {
    [ -n "$COMPOSE_FILES" ] || return 0

    compose_dir="$WORK_DIR/compose"
    if ! mkdir -m 700 "$compose_dir"; then
        error "cannot create private Compose stage: $compose_dir"
        return 1
    fi
    if ! printf '%s\n' "$COMPOSE_PROJECT" > "$compose_dir/project-name"; then
        error 'cannot stage compose/project-name'
        return 1
    fi
    if ! : > "$compose_dir/files.list"; then
        error 'cannot stage compose/files.list'
        return 1
    fi
    if ! printf 'compose/project-name\ncompose/files.list\n' >> "$WORK_DIR/snapshot.members"; then
        error 'cannot append the Compose metadata members'
        return 1
    fi

    env_dir=$(dirname "$ENV_FILE")
    old_ifs=$IFS
    IFS=:
    set -f
    set -- $COMPOSE_FILES
    set +f
    IFS=$old_ifs

    for name do
        source_file="$env_dir/$name"
        # Repeat the allowlist checks immediately before the copy.  The
        # snapshot is refused if the operator swaps in a link or removes a
        # selected file after startup validation.
        if [ -L "$source_file" ] || [ ! -f "$source_file" ] || [ ! -r "$source_file" ] || [ ! -s "$source_file" ]; then
            error "selected Compose input changed or is unsafe: $source_file"
            return 1
        fi
        if ! cp "$source_file" "$compose_dir/$name"; then
            error "cannot copy selected Compose input: $source_file"
            return 1
        fi
        chmod 600 "$compose_dir/$name" 2>/dev/null || true
        if [ ! -s "$compose_dir/$name" ]; then
            error "staged Compose input is empty: $name"
            return 1
        fi
        if ! printf '%s\n' "$name" >> "$compose_dir/files.list"; then
            error 'cannot record compose/files.list'
            return 1
        fi
        if ! printf 'compose/%s\n' "$name" >> "$WORK_DIR/snapshot.members"; then
            error 'cannot record the Compose tar member'
            return 1
        fi
    done
}

run_backup() {
    WORK_DIR="$STAGE_DIR/work.$$"
    rm -rf "$WORK_DIR"
    if ! mkdir -p "$WORK_DIR"; then
        error "cannot create private stage $WORK_DIR"
        return 1
    fi
    chmod 700 "$WORK_DIR" 2>/dev/null || true

    if ! check_env_file; then
        cleanup_work
        return 1
    fi
    if ! check_free_space; then
        cleanup_work
        return 1
    fi

    dump="$WORK_DIR/db.dump"
    log 'dump: pg_dump -Fc --no-owner --no-privileges --no-password'
    pg_dump -Fc --no-owner --no-privileges --no-password --lock-wait-timeout=30s -f "$dump"
    dump_rc=$?
    if [ "$dump_rc" -ne 0 ]; then
        backup_failed "$dump_rc" 'pg_dump failed'
        return "$dump_rc"
    fi
    if [ ! -s "$dump" ]; then
        backup_failed 1 'pg_dump produced an empty dump'
        return 1
    fi

    env_copy="$WORK_DIR/.env"
    if ! cp "$ENV_FILE" "$env_copy"; then
        backup_failed 1 'cannot copy current .env into the private stage'
        return 1
    fi
    chmod 600 "$env_copy" 2>/dev/null || true
    if [ ! -s "$env_copy" ]; then
        backup_failed 1 'staged .env is empty'
        return 1
    fi

    # A single ordered member list keeps GNU tar (dev/tests) and BusyBox tar
    # (the Alpine backup image) in the same member order: BusyBox places -T
    # entries before positional file operands, so all members come from here.
    if ! printf 'db.dump\n.env\n' > "$WORK_DIR/snapshot.members"; then
        backup_failed 1 'cannot stage the tar member list'
        return 1
    fi
    if ! stage_compose_inputs; then
        backup_failed 1 'cannot stage selected Compose inputs'
        return 1
    fi

    log "snapshot: restic backup --stdin-filename remnawave-backup.tar (dry-run=$DRY_RUN)"
    set -- backup
    [ "$DRY_RUN" -eq 1 ] && set -- backup --dry-run
    set -- "$@" --no-cache \
        --host "$RESTIC_HOST" \
        --group-by "$RESTIC_GROUP_BY" \
        --tag "$RESTIC_TAG" \
        --tag postgres \
        --tag env \
        --stdin-filename remnawave-backup.tar \
        --stdin-from-command \
        -- tar -C "$WORK_DIR" -cf - -T "$WORK_DIR/snapshot.members"
    restic "$@"
    restic_rc=$?

    if [ "$restic_rc" -ne 0 ]; then
        case "$restic_rc" in
            3) message='restic backup could not read all source data' ;;
            10) message='restic repository does not exist; run restic init explicitly' ;;
            11) message='restic repository is locked' ;;
            12) message='restic password is incorrect' ;;
            *) message='restic backup failed' ;;
        esac
        backup_failed "$restic_rc" "$message"
        return "$restic_rc"
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        log 'dry-run: snapshot not published; freshness marker unchanged'
        cleanup_work
        return 0
    fi

    if ! update_marker; then
        cleanup_work
        return 1
    fi
    log 'snapshot published; freshness marker updated'
    cleanup_work
    return 0
}

update_maintenance_day() {
    tmp="$STATE_DIR/.last_maintenance_day.$$"
    if printf '%s\n' "$(date +%Y-%m-%d)" > "$tmp" && mv -f "$tmp" "$MAINTENANCE_DAY_FILE"; then
        return 0
    fi
    rm -f "$tmp"
    log "WARN: cannot update $MAINTENANCE_DAY_FILE"
    return 1
}

run_maintenance() {
    log 'maintenance: restic forget/prune with scoped retention policy'
    if [ "$DRY_RUN" -eq 0 ]; then
        # Record the day BEFORE deleting anything.  If the marker cannot be
        # persisted, refuse to prune rather than risk repeating it every 5 min.
        if ! update_maintenance_day; then
            error 'cannot persist maintenance day marker; refusing to run retention/prune'
            return 1
        fi
    fi
    set -- forget --no-cache \
        --host "$RESTIC_HOST" \
        --tag "$RESTIC_TAG" \
        --group-by "$RESTIC_GROUP_BY" \
        --keep-last "$KEEP_LAST" \
        --keep-hourly "$KEEP_HOURLY" \
        --keep-daily "$KEEP_DAILY" \
        --keep-weekly "$KEEP_WEEKLY" \
        --keep-monthly "$KEEP_MONTHLY"

    if [ "$DRY_RUN" -eq 1 ]; then
        restic "$@" --dry-run
        forget_rc=$?
        if [ "$forget_rc" -eq 0 ]; then
            restic prune --no-cache --dry-run
            forget_rc=$?
        fi
    else
        restic "$@" --prune
        forget_rc=$?
    fi

    if [ "$forget_rc" -ne 0 ]; then
        error "restic maintenance failed (exit $forget_rc)"
        return "$forget_rc"
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        log 'maintenance dry-run: nothing was deleted'
    else
        log 'maintenance finished'
    fi
    return 0
}

maintenance_due() {
    [ "$MAINTENANCE_ENABLED" = true ] || return 1
    today=$(date +%Y-%m-%d)
    last=$(cat "$MAINTENANCE_DAY_FILE" 2>/dev/null || true)
    [ "$last" = "$today" ] && return 1
    [ "$MAINTENANCE_RAN_DAY" = "$today" ] && return 1
    current_time=$(date +%H:%M)
    if awk -v now="$current_time" -v at="$MAINTENANCE_TIME" 'BEGIN { exit !(now >= at) }'; then
        return 0
    fi
    return 1
}

sleep_until() {
    target=$1
    while [ "$SHUTDOWN" -eq 0 ]; do
        now=$(date +%s)
        if [ "$now" -ge "$target" ]; then
            return 0
        fi
        sleep "$((target - now))" &
        SLEEP_PID=$!
        wait "$SLEEP_PID" 2>/dev/null || true
        SLEEP_PID=
        [ "$SHUTDOWN" -eq 0 ] || return 0
    done
}

sleep_next_slot() {
    # Compute the next FUTURE slot.  A long run therefore skips missed
    # intervals instead of queuing snapshots back-to-back.
    next_slot=$(( ($(date +%s) / INTERVAL_SECONDS + 1) * INTERVAL_SECONDS ))
    log "sleeping until epoch $next_slot"
    sleep_until "$next_slot"
}

run_loop() {
    log "loop started: interval=${INTERVAL_SECONDS}s repository=$RESTIC_REPOSITORY"
    while [ "$SHUTDOWN" -eq 0 ]; do
        # The operation lock is released while sleeping, so an explicit
        # `--maintenance`/`--once` can run between intervals.  The loop lock
        # still prevents a second loop process.
        if ! acquire_operation_lock wait; then
            [ "$SHUTDOWN" -eq 0 ] || break
            log 'WARN: cannot acquire operation lock; retrying at next slot'
            sleep_next_slot
            continue
        fi
        if ! prepare_stage; then
            release_operation_lock
            [ "$SHUTDOWN" -eq 0 ] || break
            log 'WARN: cannot prepare stage; retrying at next slot'
            sleep_next_slot
            continue
        fi

        run_backup
        backup_rc=$?
        if [ "$backup_rc" -eq 0 ] && [ "$SHUTDOWN" -eq 0 ] && maintenance_due; then
            if ! run_maintenance; then
                log 'WARN: maintenance failed; ordinary backup loop continues'
            fi
            # In-process at-most-once-per-day fallback: even if the persistent
            # day marker is unwritable, do not retry prune every 5 minutes.
            MAINTENANCE_RAN_DAY=$(date +%Y-%m-%d)
        fi
        release_operation_lock
        [ "$SHUTDOWN" -eq 0 ] || break
        sleep_next_slot
    done
    log 'loop stopped'
}

run_self_test() {
    if ! valid_uint 12 1 9999; then
        echo 'self-test FAIL: valid_uint rejected 12' >&2
        return 1
    fi
    if valid_uint 0 1 9999; then
        echo 'self-test FAIL: valid_uint accepted 0' >&2
        return 1
    fi
    if valid_uint 012 1 9999; then
        echo 'self-test FAIL: valid_uint accepted leading zero' >&2
        return 1
    fi
    if valid_uint abc 1 9999; then
        echo 'self-test FAIL: valid_uint accepted letters' >&2
        return 1
    fi
    if ! valid_tmpfs_size 256m; then
        echo 'self-test FAIL: valid_tmpfs_size rejected 256m' >&2
        return 1
    fi
    if ! valid_tmpfs_size 1g; then
        echo 'self-test FAIL: valid_tmpfs_size rejected 1g' >&2
        return 1
    fi
    if valid_tmpfs_size 2g; then
        echo 'self-test FAIL: valid_tmpfs_size accepted 2g' >&2
        return 1
    fi
    if valid_tmpfs_size 8m; then
        echo 'self-test FAIL: valid_tmpfs_size accepted 8m' >&2
        return 1
    fi
    if ! valid_time 03:00; then
        echo 'self-test FAIL: valid_time rejected 03:00' >&2
        return 1
    fi
    if valid_time 25:00; then
        echo 'self-test FAIL: valid_time accepted 25:00' >&2
        return 1
    fi
    if ! legacy_repository 'rclone:gdrive:remnawave-bot/5min'; then
        echo 'self-test FAIL: legacy repository not detected' >&2
        return 1
    fi
    if legacy_repository 'rclone:gdrive:remnawave-restic'; then
        echo 'self-test FAIL: dedicated repository treated as legacy' >&2
        return 1
    fi
    if ! valid_compose_project bot-asteria; then
        echo 'self-test FAIL: valid_compose_project rejected bot-asteria' >&2
        return 1
    fi
    if valid_compose_project Bot-Asteria; then
        echo 'self-test FAIL: valid_compose_project accepted uppercase' >&2
        return 1
    fi
    if valid_compose_project -bot-asteria; then
        echo 'self-test FAIL: valid_compose_project accepted a leading dash' >&2
        return 1
    fi
    if ! valid_compose_basename docker-compose.asteria.yml; then
        echo 'self-test FAIL: valid_compose_basename rejected docker-compose.asteria.yml' >&2
        return 1
    fi
    if valid_compose_basename ../docker-compose.yml; then
        echo 'self-test FAIL: valid_compose_basename accepted a path' >&2
        return 1
    fi
    if valid_compose_basename docker-compose.json; then
        echo 'self-test FAIL: valid_compose_basename accepted a non-YAML name' >&2
        return 1
    fi
    if valid_compose_basename .env; then
        echo 'self-test FAIL: valid_compose_basename accepted .env' >&2
        return 1
    fi
    echo 'self-test ok'
}

usage() {
    cat <<'EOF'
Usage: backup.sh [--once | --maintenance | --loop] [--dry-run]

  --once         run one backup and exit with its status
  --maintenance  run one Restic forget/prune pass and exit
  --loop         run the 300-second backup loop (default)
  --dry-run      with --once or --maintenance, do not write to the repository
  --self-test    run local helper checks
EOF
}

main() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --once) MODE=once ;;
            --maintenance) MODE=maintenance ;;
            --loop) MODE=loop ;;
            --dry-run) DRY_RUN=1 ;;
            --self-test)
                run_self_test
                exit $?
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            *)
                usage >&2
                exit 2
                ;;
        esac
        shift
    done

    if [ "$MODE" = loop ] && [ "$DRY_RUN" -eq 1 ]; then
        error '--dry-run requires --once or --maintenance'
        exit 2
    fi

    validate_config || exit 1
    prepare_state || exit 1
    trap cleanup EXIT
    trap on_signal TERM INT

    case "$MODE" in
        once)
            acquire_operation_lock nowait || exit 1
            prepare_stage || exit 1
            run_backup
            exit $?
            ;;
        maintenance)
            acquire_operation_lock nowait || exit 1
            run_maintenance
            exit $?
            ;;
        loop)
            acquire_loop_lock || exit 1
            run_loop
            exit $?
            ;;
    esac
}

main "$@"
