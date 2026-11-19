#!/bin/sh
set -u
umask 077
export LC_ALL=C

BACKUP_DIR=${BACKUP_DIR:-/backups}
ENV_FILE=${ENV_FILE:-/src/.env}
README_SOURCE=${README_SOURCE:-/usr/local/share/remnawave-backup/RESTORE.md}
DUMP_INTERVAL_SECONDS=${DUMP_INTERVAL_SECONDS:-300}
UPLOAD_INTERVAL_SECONDS=${UPLOAD_INTERVAL_SECONDS:-300}
KEEP_5MIN=${KEEP_5MIN:-12}
KEEP_HOURLY=${KEEP_HOURLY:-24}
KEEP_DAILY=${KEEP_DAILY:-30}
KEEP_WEEKLY=${KEEP_WEEKLY:-12}
KEEP_MONTHLY=${KEEP_MONTHLY:-12}
RCLONE_REMOTE=${RCLONE_REMOTE:-gcrypt}
RCLONE_PATH=${RCLONE_PATH:-}
RCLONE_DOCS_REMOTE=${RCLONE_DOCS_REMOTE:-gdrive}
BACKUP_GDRIVE_FOLDER=${BACKUP_GDRIVE_FOLDER:-remnawave-bot}

PENDING="$BACKUP_DIR/pending"
LOCAL="$BACKUP_DIR/local"
STATE="$BACKUP_DIR/state"

DOCS_PREFIX="$RCLONE_DOCS_REMOTE:$BACKUP_GDRIVE_FOLDER"

remote_path() {
    if [ -n "$RCLONE_PATH" ]; then
        printf '%s:%s/%s' "$RCLONE_REMOTE" "$RCLONE_PATH" "$1"
    else
        printf '%s:%s' "$RCLONE_REMOTE" "$1"
    fi
}

valid_keep() {
    case "${1:-}" in
        ''|*[!0-9]*) return 1 ;;
        0*) return 1 ;;
    esac
    [ "${#1}" -le 4 ] || return 1
    return 0
}

drop_newest() {
    keep=${1:-0}
    if ! valid_keep "$keep"; then
        echo "drop_newest: invalid keep=$keep" >&2
        return 1
    fi
    tail -n +$((keep + 1))
}

timestamp_name() {
    date +%d_%B_%Y_%H-%M
}

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

validate_keep() {
    name=$1
    value=$2
    if ! valid_keep "$value"; then
        log "invalid $name=$value; must be an integer from 1 to 9999"
        exit 1
    fi
}

unique_base() {
    base=$(timestamp_name)
    candidate=$base
    n=2
    while [ -e "$PENDING/${candidate}.dump" ] || [ -e "$LOCAL/${candidate}.dump" ]; do
        candidate="${base}_${n}"
        n=$((n + 1))
    done
    echo "$candidate"
}

dump_database() {
    base=$(unique_base)
    dump_tmp="$PENDING/.${base}.dump.tmp"
    dump_final="$PENDING/${base}.dump"
    env_tmp="$PENDING/.${base}.env.tmp"
    env_final="$PENDING/${base}.env.bak"

    if ! pg_dump --no-owner --no-privileges --no-password -Fc -f "$dump_tmp"; then
        rm -f "$dump_tmp"
        log "dump FAILED"
        return 1
    fi

    if [ ! -s "$dump_tmp" ]; then
        rm -f "$dump_tmp"
        log "dump FAILED: empty file"
        return 1
    fi

    if ! cp "$ENV_FILE" "$env_tmp"; then
        rm -f "$dump_tmp" "$env_tmp"
        log "dump FAILED: env snapshot"
        return 1
    fi

    if ! mv "$dump_tmp" "$dump_final" || ! mv "$env_tmp" "$env_final"; then
        rm -f "$dump_tmp" "$env_tmp" "$dump_final" "$env_final"
        log "dump FAILED: cannot finalize pair"
        return 1
    fi

    log "dump: ${base}.dump + .env"
}

upload_pending() {
    uploaded=0

    for dump in "$PENDING"/*.dump; do
        [ -e "$dump" ] || continue
        base=$(basename "$dump" .dump)
        env_file="$PENDING/${base}.env.bak"

        if [ ! -f "$env_file" ]; then
            log "pending pair missing env: ${base}.dump"
            return 1
        fi

        if rclone copyto "$env_file" "$(remote_path "5min/$(basename "$env_file")")" \
            && rclone copyto "$dump" "$(remote_path "5min/$(basename "$dump")")"; then
            if mv "$env_file" "$LOCAL/" && mv "$dump" "$LOCAL/"; then
                uploaded=$((uploaded + 1))
            else
                log "local move FAILED: ${base}"
                return 1
            fi
        else
            log "upload FAILED: ${base}"
            return 1
        fi
    done

    if [ "$uploaded" -gt 0 ]; then
        date +%s > "$STATE/last_success"
    fi
}

prune_is_safe() {
    [ "${1:-0}" -gt 0 ] && [ "${2:-0}" -gt 0 ]
}

select_prune_pair_names() {
    dumps=$1
    envs=$2
    keep=$3
    keep_file="$dumps.keep"

    if ! valid_keep "$keep"; then
        echo "select_prune_pair_names: invalid keep=$keep" >&2
        return 1
    fi

    : > "$keep_file"
    kept=0

    while IFS= read -r name; do
        [ -n "$name" ] || continue
        base=${name%.dump}
        if grep -qxF "${base}.env.bak" "$envs"; then
            printf '%s\n' "$name" >> "$keep_file"
            printf '%s\n' "${base}.env.bak" >> "$keep_file"
            kept=$((kept + 1))
            [ "$kept" -ge "$keep" ] && break
        fi
    done < "$dumps"

    while IFS= read -r name; do
        [ -n "$name" ] || continue
        grep -qxF "$name" "$keep_file" || printf '%s\n' "$name"
    done < "$dumps"

    while IFS= read -r name; do
        [ -n "$name" ] || continue
        grep -qxF "$name" "$keep_file" || printf '%s\n' "$name"
    done < "$envs"

    rm -f "$keep_file"
}

prune_remote() {
    dir=$1
    keep=$2
    tmp=$(mktemp -d)
    remote_dir=$(remote_path "$dir")

    if ! rclone lsf "$remote_dir" --files-only --include '*.dump' \
        --format "tp" --separator "|" --time-format unix > "$tmp/dumps.raw" 2>/dev/null; then
        log "prune listing FAILED: $dir/dumps"
        rm -rf "$tmp"
        return 0
    fi

    if ! rclone lsf "$remote_dir" --files-only --include '*.env.bak' \
        --format "tp" --separator "|" --time-format unix > "$tmp/envs.raw" 2>/dev/null; then
        log "prune listing FAILED: $dir/envs"
        rm -rf "$tmp"
        return 0
    fi

    sort -rn "$tmp/dumps.raw" | cut -d'|' -f2- > "$tmp/dumps"
    sort -rn "$tmp/envs.raw" | cut -d'|' -f2- > "$tmp/envs"

    dumps_count=$(wc -l < "$tmp/dumps" | tr -d ' ')
    envs_count=$(wc -l < "$tmp/envs" | tr -d ' ')

    if [ "$dumps_count" -eq 0 ] && [ "$envs_count" -eq 0 ]; then
        rm -rf "$tmp"
        return 0
    fi

    if ! prune_is_safe "$dumps_count" "$envs_count"; then
        log "prune skipped: orphan-only tier $dir (dumps=$dumps_count envs=$envs_count)"
        rm -rf "$tmp"
        return 0
    fi

    select_prune_pair_names "$tmp/dumps" "$tmp/envs" "$keep" \
        | while IFS= read -r name; do
            [ -n "$name" ] || continue
            rclone deletefile "$(remote_path "$dir/$name")" --drive-use-trash=false \
                || log "prune FAILED: $dir/$name"
        done

    rm -rf "$tmp"
}

copy_tier() {
    dir=$1
    keep=$2
    marker=$3
    value=$4

    current=$(cat "$STATE/$marker" 2>/dev/null || true)
    [ "$value" = "$current" ] && return 0

    pending_marker="$STATE/tier-$dir.pending"
    base=$(cat "$pending_marker" 2>/dev/null || true)

    if [ -z "$base" ]; then
        for dump in $(ls -1t "$LOCAL"/*.dump 2>/dev/null); do
            candidate=$(basename "$dump" .dump)
            if [ -f "$LOCAL/${candidate}.env.bak" ]; then
                base=$candidate
                break
            fi
        done
    fi
    [ -n "$base" ] || return 0

    dump="$LOCAL/${base}.dump"
    env_file="$LOCAL/${base}.env.bak"
    if [ ! -f "$dump" ] || [ ! -f "$env_file" ]; then
        log "tier pair missing: $dir/$base"
        rm -f "$pending_marker"
        return 0
    fi

    max_age_min=$((DUMP_INTERVAL_SECONDS / 60 * 3))
    [ "$max_age_min" -ge 5 ] || max_age_min=5
    if ! find "$dump" -mmin "-$max_age_min" 2>/dev/null | grep -q .; then
        log "tier skip: selected dump is stale ($dir/$base)"
        rm -f "$pending_marker"
        return 0
    fi

    echo "$base" > "$pending_marker"

    if ! rclone copyto "$env_file" "$(remote_path "$dir/$(basename "$env_file")")"; then
        log "tier env copy FAILED: $dir/${base}.env.bak"
        return 0
    fi

    if ! rclone copyto "$dump" "$(remote_path "$dir/$(basename "$dump")")"; then
        log "tier copy FAILED: $dir/${base}.dump"
        return 0
    fi

    rm -f "$pending_marker"
    echo "$value" > "$STATE/$marker"
    prune_remote "$dir" "$keep"
}

prune_local() {
    for dump in "$LOCAL"/*.dump; do
        [ -e "$dump" ] || continue
        base=$(basename "$dump" .dump)
        if [ ! -f "$LOCAL/${base}.env.bak" ]; then
            log "remove orphan local dump: ${base}.dump"
            rm -f "$dump"
        fi
    done

    for env_file in "$LOCAL"/*.env.bak; do
        [ -e "$env_file" ] || continue
        base=$(basename "$env_file" .env.bak)
        if [ ! -f "$LOCAL/${base}.dump" ]; then
            log "remove orphan local env: ${base}.env.bak"
            rm -f "$env_file"
        fi
    done

    ls -1t "$LOCAL"/*.dump 2>/dev/null | drop_newest "$KEEP_5MIN" | while IFS= read -r dump; do
        [ -n "$dump" ] || continue
        base=$(basename "$dump" .dump)
        rm -f "$dump" "$LOCAL/${base}.env.bak"
    done
}

upload_readme() {
    [ -f "$README_SOURCE" ] || return 0
    if rclone copyto "$README_SOURCE" "$DOCS_PREFIX/README.md"; then
        log "README uploaded"
        return 0
    fi
    log "README upload FAILED"
    return 1
}

upload_readme_once() {
    value=$(date +%Y%m%d)
    current=$(cat "$STATE/readme" 2>/dev/null || true)
    [ "$value" = "$current" ] && return 0
    if upload_readme; then
        echo "$value" > "$STATE/readme"
    fi
}

run_self_test() {
    out=$(printf '%s\n' f5 f4 f3 f2 f1 | drop_newest 2)
    count=$(printf '%s\n' "$out" | wc -l)
    if [ "$count" -ne 3 ]; then
        echo "self-test FAIL: drop_newest expected 3, got $count" >&2
        return 1
    fi

    first=$(printf '%s\n' "$out" | head -n 1)
    if [ "$first" != "f3" ]; then
        echo "self-test FAIL: drop_newest expected first f3, got $first" >&2
        return 1
    fi

    empty=$(printf '' | drop_newest 2)
    if [ -n "$empty" ]; then
        echo "self-test FAIL: drop_newest expected empty output" >&2
        return 1
    fi

    name=$(timestamp_name)
    if ! printf '%s\n' "$name" | grep -Eq '^[0-9]{2}_[A-Z][a-z]+_[0-9]{4}_[0-9]{2}-[0-9]{2}$'; then
        echo "self-test FAIL: bad timestamp name: $name" >&2
        return 1
    fi

    if ! valid_keep 12; then
        echo "self-test FAIL: valid_keep rejected 12" >&2
        return 1
    fi
    if ! valid_keep 9999; then
        echo "self-test FAIL: valid_keep rejected 9999" >&2
        return 1
    fi
    if valid_keep 0; then
        echo "self-test FAIL: valid_keep accepted 0" >&2
        return 1
    fi
    if valid_keep 00; then
        echo "self-test FAIL: valid_keep accepted 00" >&2
        return 1
    fi
    if valid_keep 012; then
        echo "self-test FAIL: valid_keep accepted 012" >&2
        return 1
    fi
    if valid_keep 10000; then
        echo "self-test FAIL: valid_keep accepted 10000" >&2
        return 1
    fi
    if valid_keep abc; then
        echo "self-test FAIL: valid_keep accepted abc" >&2
        return 1
    fi
    if valid_keep ''; then
        echo "self-test FAIL: valid_keep accepted empty" >&2
        return 1
    fi

    if ! prune_is_safe 12 12; then
        echo "self-test FAIL: prune_is_safe rejected 12/12" >&2
        return 1
    fi
    if prune_is_safe 12 0; then
        echo "self-test FAIL: prune_is_safe accepted 12/0" >&2
        return 1
    fi
    if prune_is_safe 0 12; then
        echo "self-test FAIL: prune_is_safe accepted 0/12" >&2
        return 1
    fi

    old_remote=$RCLONE_REMOTE
    old_path=$RCLONE_PATH
    RCLONE_REMOTE=gcrypt
    RCLONE_PATH=
    if [ "$(remote_path "5min/x.dump")" != "gcrypt:5min/x.dump" ]; then
        echo "self-test FAIL: remote_path without path" >&2
        return 1
    fi
    RCLONE_PATH=backups
    if [ "$(remote_path "5min/x.dump")" != "gcrypt:backups/5min/x.dump" ]; then
        echo "self-test FAIL: remote_path with path" >&2
        return 1
    fi
    RCLONE_REMOTE=$old_remote
    RCLONE_PATH=$old_path

    tmp=$(mktemp -d)
    i=13
    while [ "$i" -ge 1 ]; do
        printf '26_January_2026_00-%02d.dump\n' "$i" >> "$tmp/dumps"
        printf '26_January_2026_00-%02d.env.bak\n' "$i" >> "$tmp/envs"
        i=$((i - 1))
    done
    out=$(select_prune_pair_names "$tmp/dumps" "$tmp/envs" 12)
    count=$(printf '%s\n' "$out" | wc -l)
    if [ "$count" -ne 2 ]; then
        echo "self-test FAIL: pair prune expected 2, got $count" >&2
        rm -rf "$tmp"
        return 1
    fi
    first=$(printf '%s\n' "$out" | head -n 1)
    if [ "$first" != "26_January_2026_00-01.dump" ]; then
        echo "self-test FAIL: pair prune expected 00-01.dump, got $first" >&2
        rm -rf "$tmp"
        return 1
    fi
    rm -rf "$tmp"

    tmp=$(mktemp -d)
    printf '26_January_2026_00-99.dump\n' > "$tmp/dumps"
    i=12
    while [ "$i" -ge 1 ]; do
        printf '26_January_2026_00-%02d.dump\n' "$i" >> "$tmp/dumps"
        printf '26_January_2026_00-%02d.env.bak\n' "$i" >> "$tmp/envs"
        i=$((i - 1))
    done
    out=$(select_prune_pair_names "$tmp/dumps" "$tmp/envs" 12)
    if [ "$out" != "26_January_2026_00-99.dump" ]; then
        echo "self-test FAIL: orphan dump prune got $out" >&2
        rm -rf "$tmp"
        return 1
    fi
    rm -rf "$tmp"

    tmp=$(mktemp -d)
    printf '26_January_2026_00-99.env.bak\n' > "$tmp/envs"
    i=12
    while [ "$i" -ge 1 ]; do
        printf '26_January_2026_00-%02d.dump\n' "$i" >> "$tmp/dumps"
        printf '26_January_2026_00-%02d.env.bak\n' "$i" >> "$tmp/envs"
        i=$((i - 1))
    done
    out=$(select_prune_pair_names "$tmp/dumps" "$tmp/envs" 12)
    if [ "$out" != "26_January_2026_00-99.env.bak" ]; then
        echo "self-test FAIL: orphan env prune got $out" >&2
        rm -rf "$tmp"
        return 1
    fi
    rm -rf "$tmp"

    tmp=$(mktemp -d)
    : > "$tmp/dumps"
    : > "$tmp/envs"
    out=$(select_prune_pair_names "$tmp/dumps" "$tmp/envs" 0 2>/dev/null)
    if [ -n "$out" ]; then
        echo "self-test FAIL: invalid keep produced output" >&2
        rm -rf "$tmp"
        return 1
    fi
    rm -rf "$tmp"

    tmp=$(mktemp -d)
    : > "$tmp/01_January_2026_00-00.dump"
    collision=$(
        timestamp_name() { echo "01_January_2026_00-00"; }
        PENDING=$tmp
        LOCAL=$tmp
        unique_base
    )
    rm -rf "$tmp"
    if [ "$collision" != "01_January_2026_00-00_2" ]; then
        echo "self-test FAIL: unique_base collision: $collision" >&2
        return 1
    fi

    echo "self-test ok"
}

main() {
    mkdir -p "$PENDING" "$LOCAL" "$STATE"
    rm -f "$PENDING"/.*.dump.tmp "$PENDING"/.*.env.tmp

    for dump in "$PENDING"/*.dump; do
        [ -e "$dump" ] || continue
        base=$(basename "$dump" .dump)
        if [ ! -f "$PENDING/${base}.env.bak" ]; then
            log "remove orphan pending dump: ${base}.dump"
            rm -f "$dump"
        fi
    done

    for env_file in "$PENDING"/*.env.bak; do
        [ -e "$env_file" ] || continue
        base=$(basename "$env_file" .env.bak)
        if [ ! -f "$PENDING/${base}.dump" ]; then
            log "remove orphan pending env: ${base}.env.bak"
            rm -f "$env_file"
        fi
    done

    if [ -z "${RCLONE_CONFIG_GDRIVE_TOKEN:-}" ] || [ -z "${RCLONE_CONFIG_GCRYPT_PASSWORD:-}" ]; then
        log "RCLONE_CONFIG_GDRIVE_TOKEN / RCLONE_CONFIG_GCRYPT_PASSWORD are empty; configure .env"
        exit 1
    fi

    if [ ! -s "$ENV_FILE" ] || [ ! -r "$ENV_FILE" ]; then
        log "ENV_FILE $ENV_FILE is missing, empty or unreadable; refusing to start"
        exit 1
    fi

    validate_keep KEEP_5MIN "$KEEP_5MIN"
    validate_keep KEEP_HOURLY "$KEEP_HOURLY"
    validate_keep KEEP_DAILY "$KEEP_DAILY"
    validate_keep KEEP_WEEKLY "$KEEP_WEEKLY"
    validate_keep KEEP_MONTHLY "$KEEP_MONTHLY"

    [ "$DUMP_INTERVAL_SECONDS" -gt 0 ] 2>/dev/null || DUMP_INTERVAL_SECONDS=300
    [ "$UPLOAD_INTERVAL_SECONDS" -gt 0 ] 2>/dev/null || UPLOAD_INTERVAL_SECONDS=300

    upload_readme
    last_upload=0

    while true; do
        dump_database || true

        now=$(date +%s)
        if [ $((now - last_upload)) -ge "$UPLOAD_INTERVAL_SECONDS" ]; then
            if upload_pending; then
                last_upload=$now
                prune_remote 5min "$KEEP_5MIN"
                copy_tier hourly "$KEEP_HOURLY" hourly "$(date +%Y%m%d%H)"
                copy_tier daily "$KEEP_DAILY" daily "$(date +%Y%m%d)"
                copy_tier weekly "$KEEP_WEEKLY" weekly "$(( $(date +%s) / 604800 ))"
                copy_tier monthly "$KEEP_MONTHLY" monthly "$(date +%Y%m)"
                upload_readme_once
            fi
        fi

        prune_local

        now=$(date +%s)
        next=$(( (now / DUMP_INTERVAL_SECONDS + 1) * DUMP_INTERVAL_SECONDS ))
        sleep $((next - now))
    done
}

trap 'log "shutting down"; exit 0' TERM INT

case "${1:-}" in
    --self-test)
        run_self_test
        exit $?
        ;;
esac

main
