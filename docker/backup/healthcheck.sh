#!/bin/sh
# Freshness-only check.  It deliberately does NOT claim that a snapshot is
# restorable: run a restore drill for that.  A marker is written atomically by
# backup.sh only after Restic reports a fully completed snapshot.
#
# This healthcheck is also the optional external heartbeat: it reads the ping
# URL from a file (never from the application .env) and sends success/failure
# only when that file is present.
set -u
export LC_ALL=C

STATE_DIR=${BACKUP_RESTIC_STATE_DIR:-/backups/restic/state}
MARKER="$STATE_DIR/last_success"
MAX_AGE=${BACKUP_RESTIC_HEALTHCHECK_MAX_AGE_SECONDS:-900}
PING_URL_FILE=${BACKUP_PING_URL_FILE:-}

log() {
    printf '[healthcheck] %s\n' "$*" >&2
}

ping_event() {
    event=$1
    [ -n "${PING_URL_FILE:-}" ] || return 0
    [ -f "$PING_URL_FILE" ] || return 0
    url=$(sed -n '1p' "$PING_URL_FILE" 2>/dev/null | tr -d '\r\n')
    [ -n "$url" ] || return 0
    case "$url" in
        http://*|https://*) ;;
        *)
            log 'WARN: ping URL is not http(s); ignoring'
            return 0
            ;;
    esac
    if [ "$event" = failure ]; then
        case "$url" in
            */) url="${url}fail" ;;
            *) url="$url/fail" ;;
        esac
    fi
    if command -v curl >/dev/null 2>&1; then
        if ! curl -fsS --max-time 3 -o /dev/null "$url" >/dev/null 2>&1; then
            log "WARN: $event ping failed"
        fi
    else
        log 'WARN: curl is not installed; external ping skipped'
    fi
}

fail() {
    log "$*"
    ping_event failure
    exit 1
}

case "$MAX_AGE" in
    ''|*[!0-9]*|0*)
        fail "invalid BACKUP_RESTIC_HEALTHCHECK_MAX_AGE_SECONDS=$MAX_AGE"
        ;;
esac

if [ ! -f "$MARKER" ]; then
    fail "missing freshness marker: $MARKER"
fi

last=$(sed -n '1p' "$MARKER" 2>/dev/null | tr -d '\r')
case "${last:-}" in
    ''|*[!0-9]*)
        fail "corrupt freshness marker: $MARKER"
        ;;
esac

now=$(date +%s)
if [ "$last" -gt $((now + 300)) ]; then
    fail "freshness marker is in the future: $last > $now"
fi

age=$((now - last))
if [ "$age" -ge "$MAX_AGE" ]; then
    fail "last successful snapshot is too old: ${age}s ago (limit ${MAX_AGE}s)"
fi

ping_event success
exit 0
