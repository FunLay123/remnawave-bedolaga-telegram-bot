#!/usr/bin/env bash
# Build a project-checked Docker Compose command from an ordered, validated
# Compose file list, optionally preflighting the shared panel network.
#
# This helper is intentionally host-side and Bash-only. It never sources an
# env file or recovered manifest, never evals a file list, and never prints
# Compose file contents.
set -u
umask 077
export LC_ALL=C

PROJECT=
PROJECT_DIR=$PWD
MODE=
RESTORE_DIR=
VERIFY_LIVE_MATCH=0
PREFLIGHT=0
REQUIRE_PANEL=0
PANEL_PROJECT=
PRINT_FILES=0
CAPTURE_STATUS_FILE=
COMPOSE_ARGS=()
OPERATOR_FILES=()
RESOLVED_FILES=()
LIVE_FILES=()
RESTORE_FILES=()
SEEN_NAMES=' '

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage:
  compose-recovery.sh --project NAME [--project-dir DIR] --live \
      [--preflight-network [--require-panel-network] [--panel-project NAME]] \
      -- <docker compose args...>
  compose-recovery.sh --project NAME [--project-dir DIR] --restore-dir DIR \
      [--preflight-network ...] -- <docker compose args...>
  compose-recovery.sh --project NAME [--project-dir DIR] --file NAME \
      [--file NAME...] [--preflight-network ...] -- <docker compose args...>
  compose-recovery.sh --project NAME --project-dir DIR --restore-dir DIR --verify-live-match
  compose-recovery.sh --project NAME --project-dir DIR --restore-dir DIR --print-files
  compose-recovery.sh --capture-status ENV_FILE

  --live                 derive ordered inputs from the Compose project's
                         com.docker.compose.project.config_files label
  --restore-dir DIR      use DIR/compose/files.list and DIR/compose/<basename>
  --file NAME            use a project-root basename selected by the operator
  --verify-live-match    require the restored files.list and bytes to match the
                         live project's ordered config_files; no Docker command
  --preflight-network    render effective config before the command; no-op when
                         remnawave-network is absent or Compose-managed, unless
                         --require-panel-network is set
  --require-panel-network  fail unless remnawave-network is external: true
  --panel-project NAME   require the external network's
                         com.docker.compose.project label to equal NAME
  --print-files          print the selected ordered basenames and exit
  --capture-status FILE  print disabled/enabled/missing for BACKUP_COMPOSE_FILES
                         in FILE without sourcing it
EOF
}

valid_project() {
    [[ ${1:-} =~ ^[a-z0-9][a-z0-9_-]*$ ]]
}

valid_name() {
    [[ ${1:-} =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml$ ]]
}

add_named_file() {
    local name=$1 base=$2
    valid_name "$name" || die "invalid Compose basename: $name"
    case "$SEEN_NAMES" in
        *" $name "*) die "duplicate Compose basename: $name" ;;
    esac
    SEEN_NAMES="$SEEN_NAMES$name "
    [ -f "$base/$name" ] || die "missing Compose file: $name"
    [ -L "$base/$name" ] && die "Compose input is a symlink: $name"
    [ -s "$base/$name" ] || die "Compose input is empty: $name"
    RESOLVED_FILES+=("$name")
}

capture_status() {
    local file=$1 value
    [ -f "$file" ] || {
        printf 'missing\n'
        return 0
    }
    value=$(awk -F= '
        $1 == "BACKUP_COMPOSE_FILES" {
            value = $0
            sub(/^[^=]*=/, "", value)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
            sub(/[[:space:]]+#.*$/, "", value)
            gsub(/^["\047]|["\047]$/, "", value)
            last = value
        }
        END { print last }
    ' "$file")
    if [ -n "${value:-}" ]; then
        printf 'enabled\n'
    else
        printf 'disabled\n'
    fi
}

resolve_live_files() {
    local ids id current label paths path name path_dir
    ids=$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT") ||
        die "cannot query containers for project '$PROJECT'"
    [ -n "$ids" ] || die "no containers for project '$PROJECT'; verify the project name"

    label=
    for id in $ids; do
        current=$(docker inspect \
            --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' \
            "$id") || die "cannot inspect project container '$id'"
        case "$current" in
            ''|'<no value>')
                die "project container '$id' has no config_files label; use --file explicitly"
                ;;
        esac
        if [ -z "$label" ]; then
            label=$current
        elif [ "$current" != "$label" ]; then
            die "project containers disagree on config_files; refusing"
        fi
    done

    IFS=',' read -r -a paths <<< "$label"
    [ "${#paths[@]}" -gt 0 ] || die 'live config_files label is empty'

    RESOLVED_FILES=()
    SEEN_NAMES=' '
    for path in "${paths[@]}"; do
        case "$path" in
            /*|[A-Za-z]:[\\/]*) ;;
            *) path="$PROJECT_DIR/$path" ;;
        esac
        name=${path##*/}
        name=${name//[[:space:]]/}
        [ -n "$name" ] || die 'live config_files label contains an empty basename'
        path_dir=${path%/*}
        [ "$path_dir" != "$path" ] || die "live Compose input has no directory: $name"
        path_dir=$(cd "$path_dir" 2>/dev/null && pwd -P) ||
            die "live Compose input directory is missing: $name"
        [ "$path_dir" = "$PROJECT_DIR" ] ||
            die "live Compose input '$name' is outside project dir"
        add_named_file "$name" "$PROJECT_DIR"
    done
}

resolve_restore_files() {
    local project_file="$RESTORE_DIR/compose/project-name"
    local list_file="$RESTORE_DIR/compose/files.list"
    local recorded name

    [ -f "$project_file" ] || die "missing $project_file"
    [ ! -L "$project_file" ] || die "symlinked $project_file"
    [ -f "$list_file" ] || die "missing $list_file"
    [ ! -L "$list_file" ] || die "symlinked $list_file"
    IFS= read -r recorded < "$project_file" || die "cannot read $project_file"
    [ "$recorded" = "$PROJECT" ] || die "project mismatch: restored '$recorded', expected '$PROJECT'"

    RESOLVED_FILES=()
    SEEN_NAMES=' '
    while IFS= read -r name || [ -n "$name" ]; do
        [ -n "$name" ] || die 'compose/files.list contains an empty line'
        add_named_file "$name" "$RESTORE_DIR/compose"
    done < "$list_file"
    [ "${#RESOLVED_FILES[@]}" -gt 0 ] || die 'compose/files.list is empty'
}

resolve_operator_files() {
    local name
    [ "${#OPERATOR_FILES[@]}" -gt 0 ] || die 'at least one --file is required'
    RESOLVED_FILES=()
    SEEN_NAMES=' '
    for name in "${OPERATOR_FILES[@]}"; do
        add_named_file "$name" "$PROJECT_DIR"
    done
}

build_dc() {
    local name
    DC=(docker compose --project-directory "$PROJECT_DIR" -p "$PROJECT")
    for name in "${RESOLVED_FILES[@]}"; do
        if [ "$MODE" = restore ]; then
            DC+=(-f "$RESTORE_DIR/compose/$name")
        else
            DC+=(-f "$PROJECT_DIR/$name")
        fi
    done
}

verify_live_match() {
    local i name live_joined restored_joined

    resolve_live_files
    LIVE_FILES=("${RESOLVED_FILES[@]}")
    resolve_restore_files
    RESTORE_FILES=("${RESOLVED_FILES[@]}")

    if [ "${#LIVE_FILES[@]}" -ne "${#RESTORE_FILES[@]}" ]; then
        live_joined=$(IFS=,; printf '%s' "${LIVE_FILES[*]}")
        restored_joined=$(IFS=,; printf '%s' "${RESTORE_FILES[*]}")
        die "restored files.list differs from live config_files: live='$live_joined' restored='$restored_joined'"
    fi
    for i in "${!LIVE_FILES[@]}"; do
        if [ "${LIVE_FILES[$i]}" != "${RESTORE_FILES[$i]}" ]; then
            die "restored files.list differs from live at position $((i + 1)): live='${LIVE_FILES[$i]}' restored='${RESTORE_FILES[$i]}'"
        fi
    done
    for name in "${RESTORE_FILES[@]}"; do
        cmp -s "$PROJECT_DIR/$name" "$RESTORE_DIR/compose/$name" ||
            die "restored Compose bytes differ from live: $name"
    done
    printf 'restored files.list and bytes match live ordered inputs\n'
}

preflight_network() {
    local config mode project_label confirm

    config=$("${DC[@]}" config) || die 'cannot render effective Compose config'
    mode=$(printf '%s\n' "$config" | awk '
        BEGIN { in_networks = 0; in_target = 0; found = 0; external = 0; bad_name = 0 }
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
        END {
            if (!found) print "none";
            else if (bad_name) print "invalid";
            else if (external) print "external";
            else print "managed";
        }
    ')

    case "$mode" in
        none)
            [ "$REQUIRE_PANEL" -eq 0 ] || die 'panel network is required but remnawave-network is not configured'
            return 0
            ;;
        managed)
            [ "$REQUIRE_PANEL" -eq 0 ] || die 'remnawave-network is Compose-managed; an operator override with external: true is required'
            return 0
            ;;
        external) ;;
        *)
            die 'remnawave-network effective config is invalid or ambiguous'
            ;;
    esac

    docker network inspect remnawave-network >/dev/null 2>&1 ||
        die 'shared remnawave-network is missing; verify it with the panel operator; it was not created'
    project_label=$(docker network inspect remnawave-network \
        --format '{{index .Labels "com.docker.compose.project"}}') ||
        die 'cannot inspect remnawave-network project label'
    case "$project_label" in
        '<no value>') project_label= ;;
    esac

    if [ -n "$PANEL_PROJECT" ]; then
        [ "$project_label" = "$PANEL_PROJECT" ] ||
            die "network project label '$project_label' != '$PANEL_PROJECT'"
    else
        printf 'network project label: %s\n' "${project_label:-<unlabeled>}" >&2
        printf 'Type YES to confirm this is the panel-managed remnawave-network: ' >&2
        IFS= read -r confirm || die 'network confirmation is required'
        [ "$confirm" = YES ] || die 'shared panel network was not confirmed'
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --project)
            [ "$#" -ge 2 ] || die '--project requires a value'
            PROJECT=$2
            shift 2
            ;;
        --project-dir)
            [ "$#" -ge 2 ] || die '--project-dir requires a value'
            PROJECT_DIR=$2
            shift 2
            ;;
        --live)
            [ -z "$MODE" ] || [ "$MODE" = live ] || die 'choose exactly one of --live, --restore-dir, --file'
            MODE=live
            shift
            ;;
        --restore-dir)
            [ "$#" -ge 2 ] || die '--restore-dir requires a value'
            [ -z "$MODE" ] || [ "$MODE" = restore ] || die 'choose exactly one of --live, --restore-dir, --file'
            MODE=restore
            RESTORE_DIR=$2
            shift 2
            ;;
        --file)
            [ "$#" -ge 2 ] || die '--file requires a value'
            [ -z "$MODE" ] || [ "$MODE" = files ] || die 'choose exactly one of --live, --restore-dir, --file'
            MODE=files
            OPERATOR_FILES+=("$2")
            shift 2
            ;;
        --verify-live-match)
            VERIFY_LIVE_MATCH=1
            shift
            ;;
        --preflight-network)
            PREFLIGHT=1
            shift
            ;;
        --require-panel-network)
            REQUIRE_PANEL=1
            shift
            ;;
        --panel-project)
            [ "$#" -ge 2 ] || die '--panel-project requires a value'
            PANEL_PROJECT=$2
            shift 2
            ;;
        --print-files)
            PRINT_FILES=1
            shift
            ;;
        --capture-status)
            [ "$#" -ge 2 ] || die '--capture-status requires a value'
            CAPTURE_STATUS_FILE=$2
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --)
            shift
            COMPOSE_ARGS=("$@")
            break
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
done

if [ -n "$CAPTURE_STATUS_FILE" ]; then
    capture_status "$CAPTURE_STATUS_FILE"
    exit 0
fi

valid_project "$PROJECT" || die "invalid Compose project: '$PROJECT'"
[ -d "$PROJECT_DIR" ] || die "project directory does not exist: $PROJECT_DIR"
PROJECT_DIR=$(cd "$PROJECT_DIR" && pwd -P) || die "cannot resolve project directory: $PROJECT_DIR"
[ -n "$MODE" ] || die 'choose --live, --restore-dir, or --file'
[ "$REQUIRE_PANEL" -eq 0 ] || [ "$PREFLIGHT" -eq 1 ] || die '--require-panel-network requires --preflight-network'
[ -z "$PANEL_PROJECT" ] || [ "$PREFLIGHT" -eq 1 ] || die '--panel-project requires --preflight-network'

case "$MODE" in
    live)
        [ "$VERIFY_LIVE_MATCH" -eq 0 ] || die '--verify-live-match requires --restore-dir'
        resolve_live_files
        ;;
    restore)
        [ -n "$RESTORE_DIR" ] || die '--restore-dir is required'
        if [ "$VERIFY_LIVE_MATCH" -eq 1 ]; then
            [ "${#COMPOSE_ARGS[@]}" -eq 0 ] || die '--verify-live-match does not accept a Docker command'
            verify_live_match
            exit 0
        fi
        resolve_restore_files
        ;;
    files)
        [ "$VERIFY_LIVE_MATCH" -eq 0 ] || die '--verify-live-match requires --restore-dir'
        resolve_operator_files
        ;;
esac

build_dc

if [ "$PRINT_FILES" -eq 1 ]; then
    [ "${#COMPOSE_ARGS[@]}" -eq 0 ] || die '--print-files does not accept a Docker command'
    for name in "${RESOLVED_FILES[@]}"; do
        printf '%s\n' "$name"
    done
    exit 0
fi

[ "$PREFLIGHT" -eq 1 ] && preflight_network
if [ "${#COMPOSE_ARGS[@]}" -eq 0 ]; then
    exit 0
fi
exec docker compose "${DC[@]}" "${COMPOSE_ARGS[@]}"
