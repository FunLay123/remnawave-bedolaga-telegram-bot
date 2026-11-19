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

# Bounded POSIX integer check: digits only, no leading zero, bounded length.
# The length bound is deliberate: BusyBox `test` returns status 2 on integers
# outside its supported range, and an `if test` would otherwise fail open.
valid_bounded_uint() {
    value=$1
    min=$2
    max=$3
    max_digits=$4
    case "$value" in
        ''|*[!0-9]*|0*) return 1 ;;
    esac
    [ "${#value}" -le "$max_digits" ] || return 1
    [ "$value" -ge "$min" ] && [ "$value" -le "$max" ] || return 1
    return 0
}

ping_event() {
    event=$1
    [ -n "${PING_URL_FILE:-}" ] || return 0
    if [ ! -e "$PING_URL_FILE" ]; then
        log "WARN: ping URL file is missing: $PING_URL_FILE"
        return 0
    fi
    if [ ! -f "$PING_URL_FILE" ]; then
        # Compose intentionally mounts /dev/null here to disable pings.  Other
        # non-regular paths are reported and disabled.
        if [ ! -c "$PING_URL_FILE" ]; then
            log "WARN: ping URL file is not a regular file: $PING_URL_FILE"
        fi
        return 0
    fi
    if [ ! -s "$PING_URL_FILE" ]; then
        log "WARN: ping URL file is empty: $PING_URL_FILE"
        return 0
    fi
    url=$(sed -n '1p' "$PING_URL_FILE" 2>/dev/null | tr -d '\r\n') || true
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

if ! valid_bounded_uint "$MAX_AGE" 1 604800 6; then
    fail "invalid BACKUP_RESTIC_HEALTHCHECK_MAX_AGE_SECONDS=$MAX_AGE; expected 1..604800"
fi

if [ ! -f "$MARKER" ]; then
    fail "missing freshness marker: $MARKER"
fi

last=$(sed -n '1p' "$MARKER" 2>/dev/null | tr -d '\r') || true
# Epoch seconds are currently 10 digits.  12 digits keeps the arithmetic
# comfortably inside a signed 64-bit range while rejecting absurd values.
if ! valid_bounded_uint "$last" 1 999999999999 12; then
    fail "corrupt freshness marker: $MARKER"
fi

now=$(date +%s)
if ! valid_bounded_uint "$now" 1 999999999999 12; then
    fail "cannot read a bounded current time: $now"
fi
future_limit=$((now + 300))
# Fail closed even if the comparison itself errors.
if ! [ "$last" -le "$future_limit" ]; then
    fail "freshness marker is in the future or unreadable: $last > $future_limit"
fi

age=$((now - last))
# Fail closed even if the comparison itself errors.
if ! [ "$age" -lt "$MAX_AGE" ]; then
    fail "last successful snapshot is too old: ${age}s ago (limit ${MAX_AGE}s)"
fi

ping_event success
exit 0
