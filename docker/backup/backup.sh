#!/bin/sh
set -u
export LC_ALL=C

drop_newest() {
    tail -n +$(($1 + 1))
}

timestamp_name() {
    date +%d_%B_%Y_%H-%M
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

    echo "self-test ok"
}

case "${1:-}" in
    --self-test)
        run_self_test
        exit $?
        ;;
esac

echo "backup loop is not implemented yet" >&2
exit 1
