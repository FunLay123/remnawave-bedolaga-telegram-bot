#!/bin/sh
# Read-only inspection and private extraction for remnawave-backup.tar.
#
# The archive is never expanded with `tar -xf`.  The helper first proves the
# exact member set and regular-file types, then streams only those names with
# `tar -xO` into a private scratch directory.  Compose metadata is copied to
# the operator's output directory only after the expected project matches.
set -u
umask 077
export LC_ALL=C

ARCHIVE=
PROJECT=
OUTPUT=
LEGACY=0
INSPECT=0
SCRATCH=

log() {
    printf '%s\n' "$*"
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    if [ -n "${SCRATCH:-}" ] && [ -d "$SCRATCH" ]; then
        rm -rf "$SCRATCH"
    fi
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

usage() {
    cat <<'EOF'
Usage:
  restore-compose.sh --archive FILE --project PROJECT --output DIR
  restore-compose.sh --archive FILE --legacy-data-only --output DIR
  restore-compose.sh --archive FILE --inspect

  --archive FILE       downloaded remnawave-backup.tar (regular file, not a link)
  --project PROJECT    expected lowercase Compose project name
  --output DIR         existing empty directory for db.dump, .env and compose/
  --legacy-data-only   accept an old two-member archive without Compose metadata
  --inspect            print the recorded project and ordered file list only

The helper does not apply Compose settings, start containers, or touch Docker.
EOF
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
    case "${1:-}" in
        [A-Za-z0-9]*) return 0 ;;
        *) return 1 ;;
    esac
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --archive)
            [ "$#" -ge 2 ] || die '--archive requires a value'
            ARCHIVE=$2
            shift 2
            ;;
        --project)
            [ "$#" -ge 2 ] || die '--project requires a value'
            PROJECT=$2
            shift 2
            ;;
        --output)
            [ "$#" -ge 2 ] || die '--output requires a value'
            OUTPUT=$2
            shift 2
            ;;
        --legacy-data-only)
            LEGACY=1
            shift
            ;;
        --inspect)
            INSPECT=1
            shift
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
done

[ -n "$ARCHIVE" ] || die '--archive is required'
[ -f "$ARCHIVE" ] || die "archive is missing or not a regular file: $ARCHIVE"
[ -L "$ARCHIVE" ] && die "archive is a symlink; refusing to follow it: $ARCHIVE"
[ -r "$ARCHIVE" ] || die "archive is unreadable: $ARCHIVE"
[ -s "$ARCHIVE" ] || die "archive is empty: $ARCHIVE"

if [ "$LEGACY" -eq 1 ] && [ -n "$PROJECT" ]; then
    die '--legacy-data-only and --project are mutually exclusive'
fi
if [ "$INSPECT" -eq 0 ]; then
    [ -n "$OUTPUT" ] || die '--output is required for extraction'
    [ -d "$OUTPUT" ] || die "output directory does not exist: $OUTPUT"
    [ -L "$OUTPUT" ] && die "output directory is a symlink; refusing it: $OUTPUT"
    [ -w "$OUTPUT" ] || die "output directory is not writable: $OUTPUT"
fi
if [ -n "$PROJECT" ] && ! valid_compose_project "$PROJECT"; then
    die "invalid expected Compose project '$PROJECT'"
fi

SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/remnawave-compose-restore.XXXXXX") || die 'cannot create private scratch directory'
chmod 700 "$SCRATCH" 2>/dev/null || true

# Read the archive through stdin: GNU tar would otherwise parse a Windows
# drive letter such as E:\... as a remote archive specification.
tar -tf - < "$ARCHIVE" > "$SCRATCH/members" 2>"$SCRATCH/tar-stderr" || {
    cat "$SCRATCH/tar-stderr" >&2
    die "cannot list archive members: $ARCHIVE"
}

member_count() {
    grep -Fxc "$1" "$SCRATCH/members" 2>/dev/null || true
}

require_regular_member() {
    entry=$1
    verbose_file="$SCRATCH/verbose.member"
    if ! tar -tvf - "$entry" < "$ARCHIVE" > "$verbose_file" 2>/dev/null; then
        die "cannot inspect archive member '$entry'"
    fi
    [ -s "$verbose_file" ] || die "archive member '$entry' is missing"
    [ "$(wc -l < "$verbose_file")" -eq 1 ] || die "archive member '$entry' matched multiple entries"
    mode=$(awk 'NR == 1 { print substr($1, 1, 1) }' "$verbose_file")
    case "$mode" in
        -) ;;
        h) die "archive member '$entry' is a hardlink" ;;
        *) die "archive member '$entry' is not a regular file" ;;
    esac
    # BusyBox tar prints a hardlink with a regular-looking mode and ' -> target';
    # GNU tar prints ' link to target'.  Reject both before tar -xO, which would
    # otherwise produce an empty file for the link member.
    if grep -Eq ' -> | link to ' "$verbose_file"; then
        die "archive member '$entry' is a hardlink"
    fi
}

check_exact_members() {
    expected=$1
    sort "$SCRATCH/members" > "$SCRATCH/members.sorted" || die 'cannot sort archive members'
    sort "$expected" > "$SCRATCH/expected.sorted" || die 'cannot sort expected members'
    cmp -s "$SCRATCH/members.sorted" "$SCRATCH/expected.sorted" || {
        die 'archive members do not exactly match the declared data/Compose set'
    }
}

extract_regular_member() {
    entry=$1
    destination="$SCRATCH/extract/$entry"
    mkdir -p "${destination%/*}" || die "cannot create scratch directory for '$entry'"
    tar -xOf - "$entry" < "$ARCHIVE" > "$destination" || die "cannot extract archive member '$entry'"
}

check_output_is_fresh() {
    for target in db.dump .env compose; do
        [ -e "$OUTPUT/$target" ] && die "output already contains '$target'; use a fresh private directory"
        [ -L "$OUTPUT/$target" ] && die "output target '$target' is a symlink; refusing it"
    done
    return 0
}

if [ "$(member_count 'compose/project-name')" -eq 0 ] && [ "$(member_count 'compose/files.list')" -eq 0 ]; then
    if [ "$LEGACY" -ne 1 ] && [ "$INSPECT" -eq 0 ]; then
        die 'archive is a legacy two-member snapshot without Compose metadata; rerun with --legacy-data-only'
    fi
    [ "$(member_count 'db.dump')" -eq 1 ] || die "archive must contain exactly one 'db.dump'"
    [ "$(member_count '.env')" -eq 1 ] || die "archive must contain exactly one '.env'"
    printf 'db.dump\n.env\n' > "$SCRATCH/expected.members"
    check_exact_members "$SCRATCH/expected.members"
    require_regular_member 'db.dump'
    require_regular_member '.env'

    if [ "$INSPECT" -eq 1 ]; then
        log 'project: (none)'
        log 'NOTICE: legacy two-member snapshot has no Compose metadata.'
        log 'files: db.dump .env'
        exit 0
    fi

    check_output_is_fresh
    mkdir -p "$SCRATCH/extract" || die 'cannot create extraction scratch'
    extract_regular_member 'db.dump'
    extract_regular_member '.env'
    [ -s "$SCRATCH/extract/db.dump" ] || die 'db.dump is empty'
    [ -s "$SCRATCH/extract/.env" ] || die '.env is empty'
    install -m 600 "$SCRATCH/extract/db.dump" "$OUTPUT/db.dump" || die 'cannot install db.dump'
    install -m 600 "$SCRATCH/extract/.env" "$OUTPUT/.env" || die 'cannot install .env'
    log 'NOTICE: legacy two-member snapshot has no Compose metadata; restored data files only.'
    exit 0
fi

[ "$(member_count 'compose/project-name')" -eq 1 ] || die "archive must contain exactly one 'compose/project-name'"
[ "$(member_count 'compose/files.list')" -eq 1 ] || die "archive must contain exactly one 'compose/files.list'"
require_regular_member 'compose/project-name'
require_regular_member 'compose/files.list'

mkdir -p "$SCRATCH/compose" || die 'cannot create scratch compose directory'
tar -xOf - 'compose/project-name' < "$ARCHIVE" > "$SCRATCH/compose/project-name" || die 'cannot extract compose/project-name'
tar -xOf - 'compose/files.list' < "$ARCHIVE" > "$SCRATCH/compose/files.list" || die 'cannot extract compose/files.list'

[ "$(wc -l < "$SCRATCH/compose/project-name")" -eq 1 ] || die 'compose/project-name must be exactly one line'
archive_project=$(cat "$SCRATCH/compose/project-name")
valid_compose_project "$archive_project" || die "invalid project name recorded in archive: '$archive_project'"
printf '%s\n' "$archive_project" > "$SCRATCH/expected-project"
cmp -s "$SCRATCH/expected-project" "$SCRATCH/compose/project-name" || die 'compose/project-name is not canonical'
if [ -n "$PROJECT" ] && [ "$PROJECT" != "$archive_project" ]; then
    die "project mismatch: archive belongs to '$archive_project', expected '$PROJECT'; nothing extracted"
fi

: > "$SCRATCH/validated.list" || die 'cannot create validated file list'
seen_names=' '
line_count=0
while IFS= read -r name || [ -n "$name" ]; do
    line_count=$((line_count + 1))
    [ -n "$name" ] || die 'compose/files.list contains an empty line'
    valid_compose_basename "$name" || die "invalid Compose basename in compose/files.list: '$name'"
    case "$seen_names" in
        *" $name "*) die "duplicate Compose basename in compose/files.list: '$name'" ;;
    esac
    seen_names="$seen_names$name "
    printf '%s\n' "$name" >> "$SCRATCH/validated.list" || die 'cannot record validated file list'
done < "$SCRATCH/compose/files.list"
[ "$line_count" -gt 0 ] || die 'compose/files.list is empty'
cmp -s "$SCRATCH/compose/files.list" "$SCRATCH/validated.list" || die 'compose/files.list is not canonical (one basename per line, final newline required)'

printf 'db.dump\n.env\ncompose/project-name\ncompose/files.list\n' > "$SCRATCH/expected.members" || die 'cannot create expected member list'
while IFS= read -r name; do
    printf 'compose/%s\n' "$name" >> "$SCRATCH/expected.members" || die 'cannot record expected Compose member'
done < "$SCRATCH/validated.list"
check_exact_members "$SCRATCH/expected.members"

while IFS= read -r entry; do
    require_regular_member "$entry"
done < "$SCRATCH/expected.members"

if [ "$INSPECT" -eq 1 ]; then
    log "project: $archive_project"
    log 'files:'
    while IFS= read -r name; do
        log "  $name"
    done < "$SCRATCH/validated.list"
    exit 0
fi

[ -n "$PROJECT" ] || die '--project is required for Compose snapshot extraction'
if [ "$LEGACY" -eq 1 ]; then
    die 'archive contains Compose metadata; omit --legacy-data-only'
fi

mkdir -p "$SCRATCH/extract" || die 'cannot create extraction scratch'
while IFS= read -r entry; do
    extract_regular_member "$entry"
done < "$SCRATCH/expected.members"
[ -s "$SCRATCH/extract/db.dump" ] || die 'db.dump is empty'
[ -s "$SCRATCH/extract/.env" ] || die '.env is empty'
[ -s "$SCRATCH/extract/compose/project-name" ] || die 'compose/project-name is empty'
[ -s "$SCRATCH/extract/compose/files.list" ] || die 'compose/files.list is empty'
while IFS= read -r name; do
    [ -s "$SCRATCH/extract/compose/$name" ] || die "compose/$name is empty"
done < "$SCRATCH/validated.list"

check_output_is_fresh
install -m 600 "$SCRATCH/extract/db.dump" "$OUTPUT/db.dump" || die 'cannot install db.dump'
install -m 600 "$SCRATCH/extract/.env" "$OUTPUT/.env" || die 'cannot install .env'
mkdir -m 700 "$OUTPUT/compose" || die 'cannot create output compose directory'
install -m 600 "$SCRATCH/extract/compose/project-name" "$OUTPUT/compose/project-name" || die 'cannot install compose/project-name'
install -m 600 "$SCRATCH/extract/compose/files.list" "$OUTPUT/compose/files.list" || die 'cannot install compose/files.list'
while IFS= read -r name; do
    install -m 600 "$SCRATCH/extract/compose/$name" "$OUTPUT/compose/$name" || die "cannot install compose/$name"
done < "$SCRATCH/validated.list"

log "project: $archive_project"
log 'Compose files (ordered):'
while IFS= read -r name; do
    log "  $name"
done < "$SCRATCH/validated.list"
log "restored data and Compose inputs into $OUTPUT"
log 'Review them, then pass explicit -p and ordered -f flags; this helper did not apply them.'
