#!/bin/sh
set -u
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

if [ -n "$RCLONE_PATH" ]; then
    REMOTE_PREFIX="$RCLONE_REMOTE:$RCLONE_PATH"
else
    REMOTE_PREFIX="$RCLONE_REMOTE"
fi
DOCS_PREFIX="$RCLONE_DOCS_REMOTE:$BACKUP_GDRIVE_FOLDER"

valid_keep() {
    case "${1:-}" in
        ''|*[!0-9]*) return 1 ;;
        *[!0]*) return 0 ;;
    esac
    return 1
}

drop_newest() {
    keep=${1:-0}
    if ! valid_keep "$keep"; then
        echo "drop_newest: invalid keep=$keep" >&2
        return 1
    fi
    tail -n +$((keep + 1))
}

select_prune_names() {
    keep=$1
    suffix=$2
    grep "|.*${suffix}$" | sort -r | cut -d'|' -f2- | drop_newest "$keep"
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
        log "invalid $name=$value; must be a positive integer"
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

    for f in "$PENDING"/*.dump "$PENDING"/*.env.bak; do
        [ -e "$f" ] || continue
        name=$(basename "$f")
        if rclone copyto "$f" "$REMOTE_PREFIX/5min/$name"; then
            mv "$f" "$LOCAL/$name"
            uploaded=$((uploaded + 1))
        else
            log "upload FAILED: $name"
            return 1
        fi
    done

    if [ "$uploaded" -gt 0 ]; then
        date +%s > "$STATE/last_success"
    fi
}

prune_remote() {
    dir=$1
    keep=$2
    suffix=$3

    rclone lsf "$REMOTE_PREFIX/$dir" --files-only --format "tp" --separator "|" \
        --time-format "2006-01-02T15:04:05" 2>/dev/null \
        | select_prune_names "$keep" "$suffix" \
        | while IFS= read -r name; do
            [ -n "$name" ] || continue
            rclone deletefile "$REMOTE_PREFIX/$dir/$name" --drive-use-trash=false \
                || log "prune FAILED: $dir/$name"
        done
}

copy_tier() {
    dir=$1
    keep=$2
    marker=$3
    value=$4

    current=$(cat "$STATE/$marker" 2>/dev/null || true)
    [ "$value" = "$current" ] && return 0

    newest=$(ls -1t "$LOCAL"/*.dump 2>/dev/null | head -n 1)
    [ -n "$newest" ] || return 0

    base=$(basename "$newest" .dump)
    env_file="$LOCAL/${base}.env.bak"

    if ! rclone copyto "$newest" "$REMOTE_PREFIX/$dir/$(basename "$newest")"; then
        log "tier copy FAILED: $dir/$(basename "$newest")"
        return 0
    fi

    if [ ! -f "$env_file" ]; then
        log "tier env missing: $dir/$base"
        return 0
    fi

    if ! rclone copyto "$env_file" "$REMOTE_PREFIX/$dir/$(basename "$env_file")"; then
        log "tier env copy FAILED: $dir/$(basename "$env_file")"
        return 0
    fi

    echo "$value" > "$STATE/$marker"
    prune_remote "$dir" "$keep" ".dump"
    prune_remote "$dir" "$keep" ".env.bak"
}

prune_local() {
    ls -1t "$LOCAL"/*.dump 2>/dev/null | drop_newest "$KEEP_5MIN" | while IFS= read -r f; do
        [ -n "$f" ] && rm -f "$f"
    done

    ls -1t "$LOCAL"/*.env.bak 2>/dev/null | drop_newest "$KEEP_5MIN" | while IFS= read -r f; do
        [ -n "$f" ] && rm -f "$f"
    done
}

upload_readme() {
    [ -f "$README_SOURCE" ] || return 0
    if rclone copyto "$README_SOURCE" "$DOCS_PREFIX/README.md"; then
        log "README uploaded"
    else
        log "README upload FAILED"
    fi
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
    if valid_keep 0; then
        echo "self-test FAIL: valid_keep accepted 0" >&2
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
    if valid_keep 00; then
        echo "self-test FAIL: valid_keep accepted 00" >&2
        return 1
    fi

    listing=$(i=1; while [ "$i" -le 13 ]; do
        printf '2026-01-%02dT00:00:00|26_January_2026_00-%02d.dump\n' "$i" "$i"
        i=$((i + 1))
    done)
    out=$(printf '%s\n' "$listing" | select_prune_names 12 ".dump")
    if [ "$out" != "26_January_2026_00-01.dump" ]; then
        echo "self-test FAIL: dump prune expected 00-01, got $out" >&2
        return 1
    fi

    listing=$(i=1; while [ "$i" -le 13 ]; do
        printf '2026-01-%02dT00:00:00|26_January_2026_00-%02d.env.bak\n' "$i" "$i"
        i=$((i + 1))
    done)
    out=$(printf '%s\n' "$listing" | select_prune_names 12 ".env.bak")
    if [ "$out" != "26_January_2026_00-01.env.bak" ]; then
        echo "self-test FAIL: env prune expected 00-01, got $out" >&2
        return 1
    fi

    out=$(printf '2026-01-01T00:00:00|b.dump\n2026-01-01T00:00:00|a.dump\n' | select_prune_names 1 ".dump")
    if [ "$out" != "a.dump" ]; then
        echo "self-test FAIL: tie prune expected a.dump, got $out" >&2
        return 1
    fi

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
    rm -f "$PENDING"/.*.dump.tmp

    if [ -z "${RCLONE_CONFIG_GDRIVE_TOKEN:-}" ] || [ -z "${RCLONE_CONFIG_GCRYPT_PASSWORD:-}" ]; then
        log "RCLONE_CONFIG_GDRIVE_TOKEN / RCLONE_CONFIG_GCRYPT_PASSWORD are empty; configure .env"
        exit 1
    fi

    if [ ! -f "$ENV_FILE" ] || [ ! -r "$ENV_FILE" ]; then
        log "ENV_FILE $ENV_FILE is missing or unreadable; refusing to start"
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
                prune_remote 5min "$KEEP_5MIN" ".dump"
                prune_remote 5min "$KEEP_5MIN" ".env.bak"
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
