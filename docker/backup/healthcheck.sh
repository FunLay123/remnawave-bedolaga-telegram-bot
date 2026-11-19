#!/bin/sh
set -u

file=/backups/state/last_success
[ -f "$file" ] || exit 1

max=${HEALTHCHECK_MAX_AGE_SECONDS:-900}
now=$(date +%s)
last=$(cat "$file" 2>/dev/null || echo 0)

case "$last" in
    ''|*[!0-9]*) exit 1 ;;
esac

age=$((now - last))
[ "$age" -lt "$max" ]
