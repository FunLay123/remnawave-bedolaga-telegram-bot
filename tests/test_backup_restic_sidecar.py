"""Regression tests for the optional Restic/rclone Compose backup sidecar.

The tests run the real POSIX shell scripts against fake ``pg_dump`` and ``restic``
executables, so they never touch a real database, Google Drive, or repository.
The two-files test inspects the tar stream produced by ``--stdin-from-command``;
that is the artifact Restic stores and the operator restores.
"""

from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'docker' / 'backup' / 'backup.sh'
HEALTHCHECK = ROOT / 'docker' / 'backup' / 'healthcheck.sh'
RESTORE_HELPER = ROOT / 'docker' / 'backup' / 'restore-compose.sh'
COMPOSE_RECOVERY = ROOT / 'docker' / 'backup' / 'compose-recovery.sh'
COMPOSE = ROOT / 'docker-compose.yml'
DOCKERFILE = ROOT / 'docker' / 'backup' / 'Dockerfile'

SH = shutil.which('sh')
BASH = shutil.which('bash')

pytestmark = pytest.mark.skipif(SH is None, reason='POSIX shell is not available')
needs_bash = pytest.mark.skipif(BASH is None, reason='bash is not available')


FAKE_PG_DUMP = """#!/bin/sh
set -u
if [ -n "${FAKE_PG_DUMP_LOG:-}" ]; then
    printf '%s\\n' "$*" >> "$FAKE_PG_DUMP_LOG"
fi
if [ -n "${FAKE_PG_DUMP_FAIL:-}" ]; then
    echo 'synthetic pg_dump failure' >&2
    exit "${FAKE_PG_DUMP_EXIT:-1}"
fi
out=
while [ "$#" -gt 0 ]; do
    case "$1" in
        -f|--file) out=$2; shift 2 ;;
        *) shift ;;
    esac
done
[ -n "$out" ] || { echo 'pg_dump stub: no -f' >&2; exit 2; }
printf 'synthetic-db-dump\\n' > "$out"
"""


FAKE_RESTIC = """#!/bin/sh
set -u
if [ -n "${FAKE_RESTIC_LOG:-}" ]; then
    printf '%s\\n' "$*" >> "$FAKE_RESTIC_LOG"
fi
cmd=${1:-}
[ "$#" -gt 0 ] && shift
case "$cmd" in
    backup)
        out=${FAKE_RESTIC_CAPTURE:-}
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --stdin-from-command)
                    shift
                    [ "${1:-}" = "--" ] && shift
                    break
                    ;;
                *) shift ;;
            esac
        done
        if [ -n "$out" ]; then
            "$@" > "$out"
        else
            "$@" > /dev/null
        fi
        rc=$?
        if [ -n "${FAKE_RESTIC_EXIT:-}" ]; then
            exit "$FAKE_RESTIC_EXIT"
        fi
        exit "$rc"
        ;;
    forget|prune|init|check)
        exit "${FAKE_MAINTENANCE_EXIT:-0}"
        ;;
esac
exit 0
"""


FAKE_CURL = """#!/bin/sh
set -u
printf '%s\\n' "$*" >> "$FAKE_CURL_LOG"
exit 0
"""


FAKE_DOCKER = """#!/bin/bash
set -u
printf '%s\\n' "$*" >> "${FAKE_DOCKER_LOG:?}"
case "${1:-}" in
    ps)
        [ "${FAKE_DOCKER_PROJECT_PRESENT:-1}" = 1 ] && printf 'cid1\\n'
        exit 0
        ;;
    inspect)
        case "$*" in
            *config_files*) printf '%s\\n' "${FAKE_DOCKER_CONFIG_FILES:-}" ;;
            *com.docker.compose.project*) printf '%s\\n' "${FAKE_DOCKER_PROJECT_LABEL:-}" ;;
            *) printf '{}\\n' ;;
        esac
        exit 0
        ;;
    network)
        [ "${FAKE_NETWORK_MISSING:-0}" = 1 ] && exit 1
        case "$*" in
            *'{{json .Labels}}'*) printf '%s\\n' "${FAKE_NETWORK_LABELS:-{}}" ;;
            *com.docker.compose.project*) printf '%s\\n' "${FAKE_PANEL_PROJECT_LABEL:-}" ;;
            *) printf '{}\\n' ;;
        esac
        exit 0
        ;;
    compose)
        if [ "${2:-}" = docker ]; then
            echo 'duplicate docker compose prefix' >&2
            exit 2
        fi
        case " $* " in
            *' config '*)
                [ "${FAKE_COMPOSE_CONFIG_FAIL:-0}" = 1 ] && exit 1
                printf '%s\\n' "${FAKE_COMPOSE_CONFIG:-}"
                ;;
        esac
        exit 0
        ;;
esac
exit 0
"""


@dataclass
class Harness:
    env: dict[str, str]
    app_env: Path
    stage: Path
    state: Path
    marker: Path
    restic_log: Path
    pg_dump_log: Path
    capture: Path

    def run(self, *args: str, timeout: float = 20) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed repository script and test harness
            [SH, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=timeout,
            check=False,
        )

    def run_healthcheck(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed repository script
            [SH, str(HEALTHCHECK)],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
            check=False,
        )

    def restic_calls(self) -> list[str]:
        if not self.restic_log.exists():
            return []
        return [line for line in self.restic_log.read_text(encoding='utf-8').splitlines() if line]

    def pg_dump_calls(self) -> list[str]:
        if not self.pg_dump_log.exists():
            return []
        return [line for line in self.pg_dump_log.read_text(encoding='utf-8').splitlines() if line]

    def stage_entries(self) -> list[Path]:
        if not self.stage.exists():
            return []
        return sorted(self.stage.iterdir())

    def run_helper(self, *args: str, timeout: float = 20) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed repository script and test harness
            [SH, str(RESTORE_HELPER), *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=timeout,
            check=False,
        )


def _safe_environment() -> dict[str, str]:
    # Never inherit a developer's real RESTIC_*/RCLONE_*/BACKUP_* secrets.
    blocked = ('RESTIC_', 'RCLONE_', 'BACKUP_', 'PGPASSWORD')
    return {key: value for key, value in os.environ.items() if not key.upper().startswith(blocked)}


def _compose_recovery_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    bin_dir = tmp_path / 'recovery-bin'
    bin_dir.mkdir(exist_ok=True)
    fake_docker = bin_dir / 'docker'
    fake_docker.write_text(FAKE_DOCKER, encoding='utf-8', newline='\n')
    fake_docker.chmod(0o755)

    env = _safe_environment()
    env.update(
        {
            'PATH': str(bin_dir) + os.pathsep + env.get('PATH', ''),
            'FAKE_DOCKER_LOG': str(tmp_path / 'docker.log'),
            'FAKE_COMPOSE_CONFIG': '',
        }
    )
    env.update(overrides)
    return env


def _run_compose_recovery(env: dict[str, str], *args: str, timeout: float = 20) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(  # noqa: S603 - fixed repository script and test harness
        [BASH, str(COMPOSE_RECOVERY), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def _fake_docker_calls(env: dict[str, str]) -> list[str]:
    log = Path(env['FAKE_DOCKER_LOG'])
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding='utf-8').splitlines() if line]


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, body in (
        ('pg_dump', FAKE_PG_DUMP),
        ('restic', FAKE_RESTIC),
        ('curl', FAKE_CURL),
    ):
        target = bin_dir / name
        target.write_text(body, encoding='utf-8', newline='\n')
        target.chmod(0o755)

    stage = tmp_path / 'stage'
    state = tmp_path / 'state'
    state.mkdir()
    marker = state / 'last_success'
    marker.write_text('100\n', encoding='utf-8')

    app_env = tmp_path / 'app.env'
    app_env.write_text('POSTGRES_PASSWORD=synthetic\n', encoding='utf-8', newline='\n')

    restic_log = tmp_path / 'restic.log'
    pg_dump_log = tmp_path / 'pg_dump.log'
    capture = tmp_path / 'snapshot.tar'

    env = _safe_environment()
    env.update(
        {
            'PATH': str(bin_dir) + os.pathsep + env.get('PATH', ''),
            'TZ': 'UTC',
            'LC_ALL': 'C',
            'ENV_FILE': str(app_env),
            'BACKUP_RESTIC_STAGE_DIR': str(stage),
            'BACKUP_RESTIC_STATE_DIR': str(state),
            'BACKUP_RESTIC_INTERVAL_SECONDS': '300',
            'BACKUP_RESTIC_TMPFS_SIZE': '256m',
            'BACKUP_RESTIC_MIN_FREE_MB': '1',
            # The test stage is a normal temp directory, not a tmpfs.
            'BACKUP_RESTIC_REQUIRE_TMPFS': 'false',
            'BACKUP_RESTIC_KEEP_LAST': '12',
            'BACKUP_RESTIC_KEEP_HOURLY': '24',
            'BACKUP_RESTIC_KEEP_DAILY': '30',
            'BACKUP_RESTIC_KEEP_WEEKLY': '12',
            'BACKUP_RESTIC_KEEP_MONTHLY': '12',
            'RESTIC_REPOSITORY': str(tmp_path / 'repo'),
            'RESTIC_PASSWORD': 'synthetic-password',
            'FAKE_RESTIC_LOG': str(restic_log),
            'FAKE_RESTIC_CAPTURE': str(capture),
            'FAKE_PG_DUMP_LOG': str(pg_dump_log),
            'PGHOST': 'synthetic',
            'PGDATABASE': 'synthetic',
            'PGUSER': 'synthetic',
        }
    )
    return Harness(
        env=env,
        app_env=app_env,
        stage=stage,
        state=state,
        marker=marker,
        restic_log=restic_log,
        pg_dump_log=pg_dump_log,
        capture=capture,
    )


PROD_COMPOSE_INPUTS = {
    'docker-compose.yml': b'services:\n  bot:\n    image: synthetic:base\n',
    'docker-compose.local.yml': b'services:\n  bot:\n    environment:\n      SYNTHETIC_LOCAL: "1"\n',
    'docker-compose.asteria.yml': b'services:\n  bot:\n    networks:\n      - synthetic-external\n',
}


def _write_compose_inputs(harness: Harness, inputs: dict[str, bytes] | None = None) -> dict[str, bytes]:
    payload = dict(PROD_COMPOSE_INPUTS if inputs is None else inputs)
    for name, content in payload.items():
        (harness.app_env.parent / name).write_bytes(content)
    return payload


def _enable_compose_capture(
    harness: Harness,
    names: list[str] | None = None,
    project: str = 'bot-asteria',
) -> None:
    selected = list(PROD_COMPOSE_INPUTS) if names is None else names
    harness.env['BACKUP_COMPOSE_FILES'] = ':'.join(selected)
    harness.env['BACKUP_COMPOSE_PROJECT'] = project


def _add_tar_bytes(archive: tarfile.TarFile, name: str, data: bytes, mode: int = 0o600) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    archive.addfile(info, io.BytesIO(data))


def _add_tar_symlink(archive: tarfile.TarFile, name: str, target: str) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    info.mode = 0o777
    info.mtime = 0
    archive.addfile(info)


def _add_tar_hardlink(archive: tarfile.TarFile, name: str, target: str) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.LNKTYPE
    info.linkname = target
    info.mode = 0o600
    info.mtime = 0
    archive.addfile(info)


def _write_legacy_archive(path: Path) -> None:
    with tarfile.open(path, mode='w:') as archive:
        _add_tar_bytes(archive, 'db.dump', b'legacy-db-dump\n')
        _add_tar_bytes(archive, '.env', b'LEGACY_ENV=1\n')


def _write_compose_archive(
    path: Path,
    *,
    project: str = 'bot-asteria',
    files: tuple[str, ...] = ('docker-compose.yml',),
    manifest: str | None = None,
    symlinks: dict[str, str] | None = None,
    hardlinks: dict[str, str] | None = None,
    contents: dict[str, bytes] | None = None,
    extra: tuple[tuple[str, bytes], ...] = (),
) -> None:
    links = {} if symlinks is None else symlinks
    hard = {} if hardlinks is None else hardlinks
    payload = {} if contents is None else contents
    listed = '' if manifest is None else manifest
    if manifest is None:
        listed = ''.join(f'{name}\n' for name in files)
    with tarfile.open(path, mode='w:') as archive:
        _add_tar_bytes(archive, 'db.dump', b'synthetic-db-dump\n')
        _add_tar_bytes(archive, '.env', b'POSTGRES_PASSWORD=synthetic\n')
        _add_tar_bytes(archive, 'compose/project-name', f'{project}\n'.encode())
        _add_tar_bytes(archive, 'compose/files.list', listed.encode())
        for name in files:
            if name in links:
                _add_tar_symlink(archive, f'compose/{name}', links[name])
            elif name in hard:
                _add_tar_hardlink(archive, f'compose/{name}', hard[name])
            else:
                _add_tar_bytes(
                    archive,
                    f'compose/{name}',
                    payload.get(name, f'content:{name}\n'.encode()),
                )
        for name, content in extra:
            _add_tar_bytes(archive, name, content)


def _write_restore_dir(path: Path, project: str, files: dict[str, bytes]) -> Path:
    compose = path / 'compose'
    compose.mkdir(parents=True)
    (compose / 'project-name').write_bytes(f'{project}\n'.encode())
    (compose / 'files.list').write_bytes(''.join(f'{name}\n' for name in files).encode())
    for name, content in files.items():
        (compose / name).write_bytes(content)
    (path / '.env').write_bytes(b'POSTGRES_PASSWORD=synthetic\n')
    return path


@needs_bash
def test_compose_recovery_accepts_trusted_single_and_multi_files_without_metadata(
    tmp_path: Path,
) -> None:
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'docker-compose.custom.yml').write_bytes(b'services: {}\n')
    (project / 'docker-compose.extra.yaml').write_bytes(b'services: {}\n')
    env = _compose_recovery_env(tmp_path)

    single = _run_compose_recovery(
        env,
        '--project',
        'custom-project',
        '--project-dir',
        str(project),
        '--file',
        'docker-compose.custom.yml',
        '--print-files',
    )
    multi = _run_compose_recovery(
        env,
        '--project',
        'custom-project',
        '--project-dir',
        str(project),
        '--file',
        'docker-compose.custom.yml',
        '--file',
        'docker-compose.extra.yaml',
        '--print-files',
    )

    assert single.returncode == 0, single.stderr
    assert single.stdout.splitlines() == ['docker-compose.custom.yml']
    assert multi.returncode == 0, multi.stderr
    assert multi.stdout.splitlines() == [
        'docker-compose.custom.yml',
        'docker-compose.extra.yaml',
    ]
    assert _fake_docker_calls(env) == []


@needs_bash
@pytest.mark.parametrize(
    ('content', 'expected'),
    [
        (None, 'missing'),
        (b'POSTGRES_PASSWORD=synthetic\n', 'disabled'),
        (b'BACKUP_COMPOSE_FILES=\n', 'disabled'),
        (b'BACKUP_COMPOSE_FILES=secret-value\n', 'enabled'),
    ],
)
def test_compose_recovery_capture_status_accepts_absent_and_empty(
    tmp_path: Path, content: bytes | None, expected: str
) -> None:
    env_file = tmp_path / '.env'
    if content is not None:
        env_file.write_bytes(content)
    env = _compose_recovery_env(tmp_path)

    result = _run_compose_recovery(env, '--capture-status', str(env_file))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
    assert 'secret-value' not in (result.stdout + result.stderr)


@needs_bash
def test_compose_recovery_live_requires_project_containers(tmp_path: Path) -> None:
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'a.yml').write_bytes(b'services: {}\n')
    env = _compose_recovery_env(tmp_path, FAKE_DOCKER_PROJECT_PRESENT='0')

    result = _run_compose_recovery(
        env,
        '--project',
        'custom-project',
        '--project-dir',
        str(project),
        '--live',
        '--print-files',
    )

    assert result.returncode != 0
    assert 'no containers' in result.stderr.lower()
    assert not any(call.startswith('compose ') for call in _fake_docker_calls(env))


@needs_bash
def test_compose_recovery_live_match_requires_exact_order_and_bytes(tmp_path: Path) -> None:
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'a.yml').write_bytes(b'a-live\n')
    (project / 'b.yml').write_bytes(b'b-live\n')
    (project / 'extra.yml').write_bytes(b'extra-live\n')
    restore = _write_restore_dir(
        tmp_path / 'restore',
        'custom-project',
        {'a.yml': b'a-live\n', 'b.yml': b'b-live\n'},
    )
    env = _compose_recovery_env(
        tmp_path,
        FAKE_DOCKER_CONFIG_FILES=f'{project.as_posix()}/a.yml,{project.as_posix()}/b.yml',
    )
    base = (
        '--project',
        'custom-project',
        '--project-dir',
        str(project),
        '--restore-dir',
        str(restore),
        '--verify-live-match',
    )

    ok = _run_compose_recovery(env, *base)
    assert ok.returncode == 0, ok.stderr

    env['FAKE_DOCKER_CONFIG_FILES'] = f'{project.as_posix()}/b.yml,{project.as_posix()}/a.yml'
    wrong_order = _run_compose_recovery(env, *base)
    assert wrong_order.returncode != 0
    assert 'a.yml' in wrong_order.stderr and 'b.yml' in wrong_order.stderr
    assert not any(' up ' in f' {call} ' for call in _fake_docker_calls(env))

    env['FAKE_DOCKER_CONFIG_FILES'] = (
        f'{project.as_posix()}/a.yml,{project.as_posix()}/b.yml,{project.as_posix()}/extra.yml'
    )
    extra = _run_compose_recovery(env, *base)
    assert extra.returncode != 0
    assert 'extra.yml' in extra.stderr
    assert not any(' up ' in f' {call} ' for call in _fake_docker_calls(env))


@needs_bash
def test_compose_recovery_live_match_rejects_byte_drift_without_secret_output(
    tmp_path: Path,
) -> None:
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'a.yml').write_bytes(b'SECRET_LIVE_BYTES\n')
    restore = _write_restore_dir(
        tmp_path / 'restore',
        'custom-project',
        {'a.yml': b'SECRET_RESTORED_BYTES\n'},
    )
    env = _compose_recovery_env(
        tmp_path,
        FAKE_DOCKER_CONFIG_FILES=f'{project.as_posix()}/a.yml',
    )

    result = _run_compose_recovery(
        env,
        '--project',
        'custom-project',
        '--project-dir',
        str(project),
        '--restore-dir',
        str(restore),
        '--verify-live-match',
    )

    assert result.returncode != 0
    assert 'a.yml' in result.stderr
    assert 'SECRET_' not in (result.stdout + result.stderr)
    assert not any(' up ' in f' {call} ' for call in _fake_docker_calls(env))


PANEL_CONFIG_LOCAL = """networks:
  remnawave-network:
    name: remnawave-network
    driver: bridge
"""
PANEL_CONFIG_NONE = """networks:
  bot_network:
    driver: bridge
"""
PANEL_CONFIG_EXTERNAL = """networks:
  remnawave-network:
    name: remnawave-network
    external: true
"""


@needs_bash
def test_compose_recovery_panel_preflight_blocks_up_until_valid(tmp_path: Path) -> None:
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'base.yml').write_bytes(b'services: {}\n')
    env = _compose_recovery_env(tmp_path, FAKE_COMPOSE_CONFIG=PANEL_CONFIG_LOCAL)
    common = (
        '--project',
        'custom-project',
        '--project-dir',
        str(project),
        '--file',
        'base.yml',
        '--preflight-network',
    )

    local = _run_compose_recovery(env, *common, '--require-panel-network', '--', 'up', '-d')
    assert local.returncode != 0
    assert 'external: true' in local.stderr
    assert not any(' up ' in f' {call} ' for call in _fake_docker_calls(env))

    managed_test_project = _run_compose_recovery(env, *common, '--', 'up', '-d')
    assert managed_test_project.returncode == 0, managed_test_project.stderr

    env['FAKE_COMPOSE_CONFIG'] = PANEL_CONFIG_NONE
    no_network_without_requirement = _run_compose_recovery(env, *common, '--', 'up', '-d')
    assert no_network_without_requirement.returncode == 0, no_network_without_requirement.stderr
    assert any(' up ' in f' {call} ' for call in _fake_docker_calls(env))

    missing_required = _run_compose_recovery(env, *common, '--require-panel-network', '--', 'up', '-d')
    assert missing_required.returncode != 0
    assert 'required' in missing_required.stderr.lower()

    env['FAKE_COMPOSE_CONFIG_FAIL'] = '1'
    render_failure = _run_compose_recovery(env, *common, '--', 'up', '-d')
    assert render_failure.returncode != 0
    assert 'render' in render_failure.stderr.lower()

    env.pop('FAKE_COMPOSE_CONFIG_FAIL')
    env['FAKE_COMPOSE_CONFIG'] = PANEL_CONFIG_EXTERNAL
    env['FAKE_PANEL_PROJECT_LABEL'] = 'panel'
    valid = _run_compose_recovery(
        env,
        *common,
        '--require-panel-network',
        '--panel-project',
        'panel',
        '--',
        'up',
        '-d',
    )
    assert valid.returncode == 0, valid.stderr

    env['FAKE_PANEL_PROJECT_LABEL'] = 'other'
    wrong_label = _run_compose_recovery(
        env,
        *common,
        '--require-panel-network',
        '--panel-project',
        'panel',
        '--',
        'up',
        '-d',
    )
    assert wrong_label.returncode != 0
    assert 'label' in wrong_label.stderr.lower()

    env['FAKE_PANEL_PROJECT_LABEL'] = 'panel'
    env['FAKE_NETWORK_MISSING'] = '1'
    missing_network = _run_compose_recovery(
        env,
        *common,
        '--require-panel-network',
        '--panel-project',
        'panel',
        '--',
        'up',
        '-d',
    )
    assert missing_network.returncode != 0
    assert 'missing' in missing_network.stderr.lower()


def test_missing_env_fails_without_snapshot_and_keeps_freshness(harness: Harness) -> None:
    harness.app_env.unlink()
    before = harness.marker.read_bytes()

    result = harness.run('--once')

    assert result.returncode != 0
    assert harness.restic_calls() == []
    assert harness.marker.read_bytes() == before
    assert harness.stage_entries() == []
    assert 'ENV_FILE' in (result.stdout + result.stderr)


@pytest.mark.parametrize('content', ['', '   \n'])
def test_empty_or_blank_env_is_rejected(harness: Harness, content: str) -> None:
    harness.app_env.write_text(content, encoding='utf-8')
    before = harness.marker.read_bytes()

    result = harness.run('--once')

    assert result.returncode != 0
    assert harness.restic_calls() == []
    assert harness.marker.read_bytes() == before
    assert harness.stage_entries() == []


def test_failing_pg_dump_does_not_publish_or_touch_freshness(harness: Harness) -> None:
    harness.env['FAKE_PG_DUMP_FAIL'] = '1'
    harness.env['FAKE_PG_DUMP_EXIT'] = '7'
    before = harness.marker.read_bytes()

    result = harness.run('--once')

    assert result.returncode == 7
    assert len(harness.pg_dump_calls()) == 1
    assert harness.restic_calls() == []
    assert harness.marker.read_bytes() == before
    assert harness.stage_entries() == []


def test_restic_exit_three_is_failure_and_freshness_unchanged(harness: Harness) -> None:
    harness.env['FAKE_RESTIC_EXIT'] = '3'
    before = harness.marker.read_bytes()

    result = harness.run('--once')

    assert result.returncode == 3
    assert any(call.startswith('backup ') for call in harness.restic_calls())
    assert not any(call.startswith('forget ') for call in harness.restic_calls())
    assert harness.marker.read_bytes() == before
    assert harness.stage_entries() == []


def test_snapshot_tar_contains_exactly_dump_and_env(harness: Harness) -> None:
    result = harness.run('--once')

    assert result.returncode == 0, result.stderr
    assert harness.capture.exists()
    with tarfile.open(harness.capture, mode='r:') as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == ['db.dump', '.env']
        assert all(member.isfile() for member in members)
        assert archive.extractfile('db.dump').read() == b'synthetic-db-dump\n'
        assert archive.extractfile('.env').read() == b'POSTGRES_PASSWORD=synthetic\n'
    assert harness.marker.read_text(encoding='utf-8').strip().isdigit()
    assert harness.stage_entries() == []


def test_empty_compose_file_list_keeps_legacy_two_member_snapshot(harness: Harness) -> None:
    harness.env['BACKUP_COMPOSE_PROJECT'] = 'bot-asteria'
    harness.env['BACKUP_COMPOSE_FILES'] = ''

    result = harness.run('--once')

    assert result.returncode == 0, result.stderr
    with tarfile.open(harness.capture, mode='r:') as archive:
        assert [member.name for member in archive.getmembers()] == ['db.dump', '.env']


def test_compose_capture_archives_exact_ordered_prod_inputs(harness: Harness) -> None:
    inputs = _write_compose_inputs(harness)
    names = list(inputs)
    _enable_compose_capture(harness, names)
    (harness.app_env.parent / 'docker-compose.test.yml').write_bytes(b'not selected\n')

    result = harness.run('--once')

    assert result.returncode == 0, result.stderr
    expected_members = [
        'db.dump',
        '.env',
        'compose/project-name',
        'compose/files.list',
        *(f'compose/{name}' for name in names),
    ]
    with tarfile.open(harness.capture, mode='r:') as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == expected_members
        assert all(member.isfile() for member in members)
        assert archive.extractfile('compose/project-name').read() == b'bot-asteria\n'
        assert archive.extractfile('compose/files.list').read() == (
            b'docker-compose.yml\ndocker-compose.local.yml\ndocker-compose.asteria.yml\n'
        )
        for name, content in inputs.items():
            assert archive.extractfile(f'compose/{name}').read() == content
    assert 'docker-compose.test.yml' not in expected_members
    assert harness.marker.read_text(encoding='utf-8').strip().isdigit()
    assert harness.stage_entries() == []


def test_compose_capture_preserves_selected_order(harness: Harness) -> None:
    names = ['docker-compose.asteria.yml', 'docker-compose.yml', 'docker-compose.local.yml']
    inputs = _write_compose_inputs(harness)
    _enable_compose_capture(harness, names)

    result = harness.run('--once')

    assert result.returncode == 0, result.stderr
    with tarfile.open(harness.capture, mode='r:') as archive:
        assert [member.name for member in archive.getmembers()] == [
            'db.dump',
            '.env',
            'compose/project-name',
            'compose/files.list',
            *(f'compose/{name}' for name in names),
        ]
        assert archive.extractfile('compose/files.list').read() == ''.join(f'{name}\n' for name in names).encode()
        for name in names:
            assert archive.extractfile(f'compose/{name}').read() == inputs[name]


def test_backup_self_test_checks_tar_member_list_capability(harness: Harness) -> None:
    result = harness.run('--self-test')

    assert result.returncode == 0, result.stderr
    assert 'self-test ok' in result.stdout


def test_backup_uses_required_pg_dump_and_restic_contract(harness: Harness) -> None:
    result = harness.run('--once')

    assert result.returncode == 0, result.stderr
    dump_call = harness.pg_dump_calls()[0]
    for flag in ('-Fc', '--no-owner', '--no-privileges', '--no-password', '--lock-wait-timeout=30s', '-f'):
        assert flag in dump_call

    backup_call = next(call for call in harness.restic_calls() if call.startswith('backup '))
    assert '--host remnawave-backup' in backup_call
    assert '--group-by host,tags' in backup_call
    assert '--tag remnawave-backup' in backup_call
    assert '--tag postgres' in backup_call
    assert '--tag env' in backup_call
    assert '--stdin-filename remnawave-backup.tar' in backup_call
    assert '--stdin-from-command' in backup_call
    assert '--no-cache' in backup_call
    assert 'tar -C' in backup_call
    assert '-T ' in backup_call
    assert 'snapshot.members' in backup_call


def test_once_never_runs_retention_and_maintenance_is_explicit(harness: Harness) -> None:
    assert harness.run('--once').returncode == 0
    assert harness.run('--once').returncode == 0

    once_calls = harness.restic_calls()
    assert len([call for call in once_calls if call.startswith('backup ')]) == 2
    assert not any(call.startswith(('forget ', 'prune ')) for call in once_calls)

    result = harness.run('--maintenance')

    assert result.returncode == 0, result.stderr
    maintenance_calls = harness.restic_calls()[len(once_calls) :]
    forget = next(call for call in maintenance_calls if call.startswith('forget '))
    assert '--keep-last 12' in forget
    assert '--keep-hourly 24' in forget
    assert '--keep-daily 30' in forget
    assert '--keep-weekly 12' in forget
    assert '--keep-monthly 12' in forget
    assert '--host remnawave-backup' in forget
    assert '--tag remnawave-backup' in forget
    assert '--prune' in forget
    assert '--no-cache' in forget
    assert not any(call.startswith('init') for call in harness.restic_calls())


def test_maintenance_refuses_to_prune_when_day_marker_cannot_persist(harness: Harness, tmp_path: Path) -> None:
    badbin = tmp_path / 'badbin'
    badbin.mkdir()
    fake_mv = badbin / 'mv'
    fake_mv.write_text('#!/bin/sh\necho "synthetic mv failure" >&2\nexit 1\n', encoding='utf-8', newline='\n')
    fake_mv.chmod(0o755)
    harness.env['PATH'] = str(badbin) + os.pathsep + harness.env['PATH']

    result = harness.run('--maintenance')

    assert result.returncode != 0
    assert not any(call.startswith(('forget ', 'prune ')) for call in harness.restic_calls())
    assert 'last_maintenance_day' in (result.stdout + result.stderr)


def test_maintenance_dry_run_never_deletes(harness: Harness) -> None:
    result = harness.run('--maintenance', '--dry-run')

    assert result.returncode == 0, result.stderr
    calls = harness.restic_calls()
    forget = next(call for call in calls if call.startswith('forget '))
    assert '--dry-run' in forget
    if any(call.startswith('prune') for call in calls):
        assert any('--dry-run' in call for call in calls if call.startswith('prune'))


@pytest.mark.parametrize(
    ('name', 'value'),
    [
        ('BACKUP_RESTIC_KEEP_DAILY', '0'),
        ('BACKUP_RESTIC_KEEP_LAST', 'abc'),
        ('BACKUP_RESTIC_INTERVAL_SECONDS', '0'),
        ('BACKUP_RESTIC_TMPFS_SIZE', '2g'),
        ('BACKUP_RESTIC_MAINTENANCE_TIME', '25:00'),
        ('BACKUP_RESTIC_REQUIRE_TMPFS', 'maybe'),
    ],
)
def test_invalid_configuration_is_refused_before_any_work(harness: Harness, name: str, value: str) -> None:
    harness.env[name] = value

    result = harness.run('--once')

    assert result.returncode != 0
    assert harness.pg_dump_calls() == []
    assert harness.restic_calls() == []
    assert harness.marker.read_text(encoding='utf-8') == '100\n'


@pytest.mark.parametrize(
    'project',
    ['', 'Bot-Asteria', '-bot-asteria', '_bot-asteria', 'bot/asteria', 'bot asteria', 'bot.asteria'],
)
def test_invalid_compose_project_prevents_snapshot(harness: Harness, project: str) -> None:
    _write_compose_inputs(harness)
    _enable_compose_capture(harness, project=project)
    before = harness.marker.read_bytes()

    result = harness.run('--once')

    assert result.returncode != 0
    assert harness.pg_dump_calls() == []
    assert harness.restic_calls() == []
    assert harness.marker.read_bytes() == before
    assert harness.stage_entries() == []


@pytest.mark.parametrize(
    'files_value',
    [
        'docker-compose.yml:',
        ':docker-compose.yml',
        'docker-compose.yml::docker-compose.local.yml',
        'docker-compose.yml:docker-compose.yml',
        '../docker-compose.yml',
        'dir/docker-compose.yml',
        'docker-compose.json',
        '.env',
        'docker-compose.yml:*',
        'docker-compose.yml:.env',
    ],
)
def test_invalid_compose_file_allowlist_prevents_snapshot(harness: Harness, files_value: str) -> None:
    _write_compose_inputs(harness)
    harness.env['BACKUP_COMPOSE_FILES'] = files_value
    harness.env['BACKUP_COMPOSE_PROJECT'] = 'bot-asteria'
    before = harness.marker.read_bytes()

    result = harness.run('--once')

    assert result.returncode != 0
    assert harness.pg_dump_calls() == []
    assert harness.restic_calls() == []
    assert harness.marker.read_bytes() == before
    assert harness.stage_entries() == []


def test_missing_selected_compose_file_prevents_snapshot(harness: Harness) -> None:
    _write_compose_inputs(harness)
    missing = harness.app_env.parent / 'docker-compose.missing.yml'
    missing.unlink(missing_ok=True)
    _enable_compose_capture(
        harness,
        ['docker-compose.yml', 'docker-compose.missing.yml'],
    )
    before = harness.marker.read_bytes()

    result = harness.run('--once')

    assert result.returncode != 0
    assert harness.pg_dump_calls() == []
    assert harness.restic_calls() == []
    assert harness.marker.read_bytes() == before
    assert harness.stage_entries() == []
    assert 'docker-compose.missing.yml' in (result.stdout + result.stderr)


def test_symlinked_selected_compose_file_prevents_snapshot(harness: Harness) -> None:
    target = harness.app_env.parent / 'docker-compose.real.yml'
    target.write_bytes(b'services: {}\n')
    link = harness.app_env.parent / 'docker-compose.yml'
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip('symlink creation is not available')
    _enable_compose_capture(harness, ['docker-compose.yml'])
    before = harness.marker.read_bytes()

    result = harness.run('--once')

    assert result.returncode != 0
    assert harness.pg_dump_calls() == []
    assert harness.restic_calls() == []
    assert harness.marker.read_bytes() == before
    assert harness.stage_entries() == []


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permissions are not enforceable on Windows')
def test_unreadable_selected_compose_file_prevents_snapshot(harness: Harness) -> None:
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        pytest.skip('root can read mode-zero files')
    inputs = _write_compose_inputs(harness)
    unreadable = harness.app_env.parent / 'docker-compose.local.yml'
    unreadable.chmod(0o000)
    _enable_compose_capture(harness)
    before = harness.marker.read_bytes()
    try:
        result = harness.run('--once')
    finally:
        unreadable.chmod(0o600)

    assert result.returncode != 0
    assert harness.pg_dump_calls() == []
    assert harness.restic_calls() == []
    assert harness.marker.read_bytes() == before
    assert harness.stage_entries() == []
    assert inputs


def test_restore_helper_extracts_project_checked_inputs(harness: Harness, tmp_path: Path) -> None:
    inputs = _write_compose_inputs(harness)
    _enable_compose_capture(harness)
    assert harness.run('--once').returncode == 0
    output = tmp_path / 'restore-ok'
    output.mkdir()

    result = harness.run_helper('--archive', str(harness.capture), '--project', 'bot-asteria', '--output', str(output))

    assert result.returncode == 0, result.stderr
    assert (output / 'compose' / 'project-name').read_bytes() == b'bot-asteria\n'
    assert (output / 'compose' / 'files.list').read_bytes() == (
        b'docker-compose.yml\ndocker-compose.local.yml\ndocker-compose.asteria.yml\n'
    )
    for name, content in inputs.items():
        assert (output / 'compose' / name).read_bytes() == content
    assert (output / 'db.dump').read_bytes() == b'synthetic-db-dump\n'
    assert (output / '.env').read_bytes() == b'POSTGRES_PASSWORD=synthetic\n'


def test_restore_helper_rejects_project_mismatch_without_side_effects(harness: Harness, tmp_path: Path) -> None:
    _write_compose_inputs(harness)
    _enable_compose_capture(harness)
    assert harness.run('--once').returncode == 0
    output = tmp_path / 'restore-mismatch'
    output.mkdir()
    sentinel = output / 'sentinel'
    sentinel.write_text('keep\n', encoding='utf-8')

    result = harness.run_helper('--archive', str(harness.capture), '--project', 'bot-other', '--output', str(output))

    assert result.returncode != 0
    assert 'project' in (result.stdout + result.stderr).lower()
    assert sentinel.read_text(encoding='utf-8') == 'keep\n'
    assert sorted(path.name for path in output.iterdir()) == ['sentinel']
    assert not (output / 'compose').exists()
    assert not (output / '.env').exists()
    assert not (output / 'db.dump').exists()


def test_restore_helper_legacy_path_extracts_data_only(harness: Harness, tmp_path: Path) -> None:
    archive = tmp_path / 'legacy.tar'
    _write_legacy_archive(archive)
    output = tmp_path / 'legacy-out'
    output.mkdir()

    result = harness.run_helper('--archive', str(archive), '--legacy-data-only', '--output', str(output))

    assert result.returncode == 0, result.stderr
    assert 'legacy' in (result.stdout + result.stderr).lower()
    assert (output / 'db.dump').read_bytes() == b'legacy-db-dump\n'
    assert (output / '.env').read_bytes() == b'LEGACY_ENV=1\n'
    assert sorted(path.name for path in output.iterdir()) == ['.env', 'db.dump']
    assert not (output / 'compose').exists()


def test_restore_helper_requires_explicit_legacy_mode(harness: Harness, tmp_path: Path) -> None:
    archive = tmp_path / 'legacy.tar'
    _write_legacy_archive(archive)
    output = tmp_path / 'legacy-required'
    output.mkdir()

    result = harness.run_helper('--archive', str(archive), '--project', 'bot-asteria', '--output', str(output))

    assert result.returncode != 0
    assert 'legacy' in (result.stdout + result.stderr).lower()
    assert list(output.iterdir()) == []


def test_restore_helper_rejects_legacy_flag_for_compose_archive(harness: Harness, tmp_path: Path) -> None:
    _write_compose_inputs(harness)
    _enable_compose_capture(harness)
    assert harness.run('--once').returncode == 0
    output = tmp_path / 'legacy-flag'
    output.mkdir()

    result = harness.run_helper('--archive', str(harness.capture), '--legacy-data-only', '--output', str(output))

    assert result.returncode != 0
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(
    'manifest',
    [
        'docker-compose.yml\ndocker-compose.yml\n',
        '../docker-compose.yml\n',
        'docker-compose.yml\n\n',
        'docker-compose.yml\nextra.json\n',
        'docker-compose.yml\r\n',
        '',
    ],
)
def test_restore_helper_rejects_invalid_files_list(harness: Harness, tmp_path: Path, manifest: str) -> None:
    archive = tmp_path / 'invalid-list.tar'
    _write_compose_archive(archive, manifest=manifest)
    output = tmp_path / 'invalid-list-out'
    output.mkdir()

    result = harness.run_helper('--archive', str(archive), '--project', 'bot-asteria', '--output', str(output))

    assert result.returncode != 0
    assert list(output.iterdir()) == []


def test_restore_helper_rejects_unsafe_member_types(harness: Harness, tmp_path: Path) -> None:
    archive = tmp_path / 'symlink-member.tar'
    _write_compose_archive(
        archive,
        symlinks={'docker-compose.yml': '/etc/passwd'},
    )
    output = tmp_path / 'symlink-member-out'
    output.mkdir()

    result = harness.run_helper('--archive', str(archive), '--project', 'bot-asteria', '--output', str(output))

    assert result.returncode != 0
    assert 'regular' in (result.stdout + result.stderr).lower()
    assert list(output.iterdir()) == []


def test_restore_helper_rejects_hardlink_members(harness: Harness, tmp_path: Path) -> None:
    archive = tmp_path / 'hardlink-member.tar'
    _write_compose_archive(
        archive,
        files=('docker-compose.yml', 'docker-compose.local.yml'),
        hardlinks={'docker-compose.yml': 'compose/docker-compose.local.yml'},
    )
    output = tmp_path / 'hardlink-member-out'
    output.mkdir()

    result = harness.run_helper('--archive', str(archive), '--project', 'bot-asteria', '--output', str(output))

    assert result.returncode != 0
    assert 'hardlink' in (result.stdout + result.stderr).lower()
    assert list(output.iterdir()) == []


def test_restore_helper_rejects_zero_length_compose_file(harness: Harness, tmp_path: Path) -> None:
    archive = tmp_path / 'empty-compose.tar'
    _write_compose_archive(
        archive,
        files=('docker-compose.yml',),
        contents={'docker-compose.yml': b''},
    )
    output = tmp_path / 'empty-compose-out'
    output.mkdir()

    result = harness.run_helper('--archive', str(archive), '--project', 'bot-asteria', '--output', str(output))

    assert result.returncode != 0
    assert 'empty' in (result.stdout + result.stderr).lower()
    assert list(output.iterdir()) == []


def test_restore_helper_supports_one_file_and_custom_project(harness: Harness, tmp_path: Path) -> None:
    archive = tmp_path / 'one-file.tar'
    _write_compose_archive(
        archive,
        project='remnawave-bot-test',
        files=('docker-compose.custom.yml',),
    )
    output = tmp_path / 'one-file-out'
    output.mkdir()

    result = harness.run_helper(
        '--archive',
        str(archive),
        '--project',
        'remnawave-bot-test',
        '--output',
        str(output),
    )

    assert result.returncode == 0, result.stderr
    assert (output / 'compose' / 'project-name').read_bytes() == b'remnawave-bot-test\n'
    assert (output / 'compose' / 'files.list').read_bytes() == b'docker-compose.custom.yml\n'
    assert sorted(path.name for path in (output / 'compose').iterdir()) == [
        'docker-compose.custom.yml',
        'files.list',
        'project-name',
    ]


def test_restore_helper_rejects_extra_members_without_writing(harness: Harness, tmp_path: Path) -> None:
    archive = tmp_path / 'extra-member.tar'
    _write_compose_archive(
        archive,
        extra=(('../escape', b'escape\n'),),
    )
    output = tmp_path / 'extra-member-out'
    output.mkdir()

    result = harness.run_helper('--archive', str(archive), '--project', 'bot-asteria', '--output', str(output))

    assert result.returncode != 0
    assert list(output.iterdir()) == []
    assert not (tmp_path / 'escape').exists()


def test_restore_helper_rejects_archive_project_content_mismatch(harness: Harness, tmp_path: Path) -> None:
    archive = tmp_path / 'project-content.tar'
    _write_compose_archive(archive, project='bot-other')
    output = tmp_path / 'project-content-out'
    output.mkdir()

    result = harness.run_helper('--archive', str(archive), '--project', 'bot-asteria', '--output', str(output))

    assert result.returncode != 0
    assert list(output.iterdir()) == []


def test_restore_helper_rejects_symlinked_archive(harness: Harness, tmp_path: Path) -> None:
    archive = tmp_path / 'legacy.tar'
    _write_legacy_archive(archive)
    link = tmp_path / 'legacy-link.tar'
    try:
        link.symlink_to(archive)
    except (OSError, NotImplementedError):
        pytest.skip('symlink creation is not available')
    output = tmp_path / 'symlink-archive-out'
    output.mkdir()

    result = harness.run_helper('--archive', str(link), '--legacy-data-only', '--output', str(output))

    assert result.returncode != 0
    assert list(output.iterdir()) == []


def test_restore_helper_inspects_ordered_inputs(harness: Harness, tmp_path: Path) -> None:
    _write_compose_inputs(harness)
    _enable_compose_capture(harness)
    assert harness.run('--once').returncode == 0

    result = harness.run_helper('--archive', str(harness.capture), '--inspect')

    assert result.returncode == 0, result.stderr
    assert 'project: bot-asteria' in result.stdout
    positions = [
        result.stdout.index(name)
        for name in ('docker-compose.yml', 'docker-compose.local.yml', 'docker-compose.asteria.yml')
    ]
    assert positions == sorted(positions)


def test_healthcheck_rejects_missing_corrupt_future_and_stale_markers(harness: Harness) -> None:
    fresh = str(int(time.time()))
    harness.env['BACKUP_RESTIC_HEALTHCHECK_MAX_AGE_SECONDS'] = '900'

    harness.marker.unlink()
    assert harness.run_healthcheck().returncode != 0

    harness.marker.write_text('not-a-number\n', encoding='utf-8')
    assert harness.run_healthcheck().returncode != 0

    harness.marker.write_text(f'{int(fresh) + 10000}\n', encoding='utf-8')
    assert harness.run_healthcheck().returncode != 0

    harness.marker.write_text(f'{int(fresh) - 10000}\n', encoding='utf-8')
    assert harness.run_healthcheck().returncode != 0

    harness.marker.write_text(f'{fresh}\n', encoding='utf-8')
    assert harness.run_healthcheck().returncode == 0


@pytest.mark.parametrize('marker', ['9' * 40, '0' + str(int(time.time()) - 1000), '0'])
def test_healthcheck_rejects_unbounded_or_leading_zero_markers(harness: Harness, marker: str) -> None:
    harness.marker.write_text(f'{marker}\n', encoding='utf-8')

    result = harness.run_healthcheck()

    assert result.returncode != 0
    assert 'corrupt' in result.stderr


@pytest.mark.parametrize('value', ['0', '604801', '9' * 40, '0900', 'abc'])
def test_healthcheck_rejects_out_of_range_max_age(harness: Harness, value: str) -> None:
    harness.env['BACKUP_RESTIC_HEALTHCHECK_MAX_AGE_SECONDS'] = value
    harness.marker.write_text(f'{int(time.time())}\n', encoding='utf-8')

    result = harness.run_healthcheck()

    assert result.returncode != 0
    assert 'invalid' in result.stderr


def test_healthcheck_accepts_boundary_max_age(harness: Harness) -> None:
    harness.env['BACKUP_RESTIC_HEALTHCHECK_MAX_AGE_SECONDS'] = '604800'
    harness.marker.write_text(f'{int(time.time())}\n', encoding='utf-8')

    result = harness.run_healthcheck()

    assert result.returncode == 0, result.stderr


def test_healthcheck_sends_optional_ping_from_file_without_logging_url(harness: Harness, tmp_path: Path) -> None:
    ping_file = tmp_path / 'ping-url'
    ping_file.write_text('https://ping.example.invalid/token\n', encoding='utf-8')
    curl_log = tmp_path / 'curl.log'
    harness.env['BACKUP_PING_URL_FILE'] = str(ping_file)
    harness.env['FAKE_CURL_LOG'] = str(curl_log)
    fresh = str(int(time.time()))

    harness.marker.write_text(f'{fresh}\n', encoding='utf-8')
    result = harness.run_healthcheck()

    assert result.returncode == 0, result.stderr
    assert 'https://ping.example.invalid/token' in curl_log.read_text(encoding='utf-8')
    assert 'ping.example.invalid' not in (result.stdout + result.stderr)

    harness.marker.write_text(f'{int(fresh) - 10000}\n', encoding='utf-8')
    result = harness.run_healthcheck()

    assert result.returncode != 0
    assert 'https://ping.example.invalid/token/fail' in curl_log.read_text(encoding='utf-8')
    assert 'ping.example.invalid' not in (result.stdout + result.stderr)


def test_healthcheck_warns_about_missing_optional_ping_file(harness: Harness, tmp_path: Path) -> None:
    harness.env['BACKUP_PING_URL_FILE'] = str(tmp_path / 'missing-ping-url')
    harness.marker.write_text(f'{int(time.time())}\n', encoding='utf-8')

    result = harness.run_healthcheck()

    assert result.returncode == 0, result.stderr
    assert 'missing' in result.stderr.lower() or 'not found' in result.stderr.lower()


def test_production_stage_requires_tmpfs(harness: Harness) -> None:
    harness.env.pop('BACKUP_RESTIC_REQUIRE_TMPFS')
    before = harness.marker.read_bytes()

    result = harness.run('--once')

    assert result.returncode != 0
    assert 'tmpfs' in (result.stdout + result.stderr)
    assert harness.pg_dump_calls() == []
    assert harness.restic_calls() == []
    assert harness.marker.read_bytes() == before


def test_rclone_repository_requires_readable_regular_nonempty_config(harness: Harness, tmp_path: Path) -> None:
    config_dir = tmp_path / 'rclone-config-is-a-directory'
    config_dir.mkdir()
    harness.env['RESTIC_REPOSITORY'] = 'rclone:test:/tmp/synthetic-repo'
    harness.env['RCLONE_CONFIG'] = str(config_dir)

    result = harness.run('--once')

    assert result.returncode != 0
    assert harness.pg_dump_calls() == []
    assert harness.restic_calls() == []
    assert 'rclone config' in (result.stdout + result.stderr)


def test_sigterm_stops_loop_cleanly_and_cleans_stage(harness: Harness, tmp_path: Path) -> None:
    harness.env['BACKUP_RESTIC_MAINTENANCE_ENABLED'] = 'false'
    harness.marker.unlink()
    state_dir = str(harness.state).replace(chr(92), '/')
    wrapper = tmp_path / 'signal.sh'
    wrapper.write_text(
        '#!/bin/sh\n'
        f'"{SH}" "{SCRIPT}" --loop &\n'
        'pid=$!\n'
        'i=0\n'
        f'while [ ! -f "{state_dir}/last_success" ] && [ "$i" -lt 50 ]; do\n'
        '    sleep 0.2\n'
        '    i=$((i + 1))\n'
        'done\n'
        f'[ -f "{state_dir}/last_success" ] || {{ kill -TERM "$pid" 2>/dev/null; wait "$pid"; exit 8; }}\n'
        'sleep 1\n'
        'status=0\n'
        'tries=0\n'
        f'while ! "{SH}" "{SCRIPT}" --maintenance; do\n'
        '    tries=$((tries + 1))\n'
        '    if [ "$tries" -ge 5 ]; then status=9; break; fi\n'
        '    sleep 1\n'
        'done\n'
        'kill -TERM "$pid"\n'
        'wait "$pid"\n'
        'exit "$status"\n',
        encoding='utf-8',
        newline='\n',
    )
    wrapper.chmod(0o755)

    result = subprocess.run(  # noqa: S603 - fixed repository script
        [SH, str(wrapper)],
        capture_output=True,
        text=True,
        env=harness.env,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert 'shutting down' in (result.stdout + result.stderr)
    assert any(call.startswith('forget ') for call in harness.restic_calls())
    assert harness.stage_entries() == []


def test_compose_keeps_backup_optional_and_secrets_out_of_env() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding='utf-8'))
    backup = compose['services']['backup']

    assert backup['profiles'] == ['backup']
    assert backup['build']['context'] == './docker/backup'
    environment = backup['environment']
    assert environment['RESTIC_PASSWORD_FILE'] == '/run/secrets/restic-password'
    assert environment['RCLONE_CONFIG'] == '/config/rclone/rclone.conf'
    assert environment['RCLONE_TIMEOUT'] == '${RCLONE_TIMEOUT:-2m}'
    assert environment['RCLONE_CONTIMEOUT'] == '${RCLONE_CONTIMEOUT:-10s}'
    assert environment['RCLONE_RETRIES'] == '${RCLONE_RETRIES:-3}'
    assert environment['BACKUP_RESTIC_REQUIRE_TMPFS'] == '${BACKUP_RESTIC_REQUIRE_TMPFS:-true}'
    assert environment['BACKUP_COMPOSE_FILES'] == '${BACKUP_COMPOSE_FILES:-}'
    assert environment['BACKUP_COMPOSE_PROJECT'] == '${COMPOSE_PROJECT_NAME:-}'
    assert 'RCLONE_CONFIG_GDRIVE_TOKEN' not in environment
    assert 'RCLONE_CONFIG_GCRYPT_PASSWORD' not in environment
    assert 'RESTIC_PASSWORD' not in environment
    assert any('/run/remnawave-backup' in str(item) and 'size=' in str(item) for item in backup.get('tmpfs', []))
    assert any(':ro' in str(item) and 'restic-password' in str(item) for item in backup['volumes'])
    assert any(':rw' in str(item) and 'rclone' in str(item) for item in backup['volumes'])


def _bash_blocks(path: Path) -> list[str]:
    text = path.read_text(encoding='utf-8')
    return re.findall(r'```bash\n(.*?)```', text, flags=re.DOTALL)


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_restore_bash_blocks_are_syntactically_valid() -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        for index, block in enumerate(_bash_blocks(path), start=1):
            result = subprocess.run(  # noqa: S603 - fixed bash binary and repository docs
                [BASH, '-n'],
                input=block,
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, f'{path.name} bash block {index}:\n{result.stderr}\n{block}'


def _section(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    return text[start : text.index(end_marker, start)]


def _restore_db_expression(path: Path) -> str:
    match = re.search(r'^RESTORE_DB=(.+)$', path.read_text(encoding='utf-8'), flags=re.MULTILINE)
    assert match, f'RESTORE_DB assignment missing in {path}'
    return match.group(0)


@pytest.mark.skipif(BASH is None, reason='bash is not available')
@pytest.mark.parametrize('original', ['a' * 63, 'ж' * 63, 'x' * 200])
def test_restore_db_name_is_short_ascii_and_independent(original: str) -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        expression = _restore_db_expression(path)
        script = f'ORIGINAL_DB={shlex.quote(original)}\n{expression}\nprintf "%s" "$RESTORE_DB"'
        result = subprocess.run(  # noqa: S603 - fixed bash binary and repository docs
            [BASH, '-c', script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        name = result.stdout
        assert re.fullmatch(r'remnawave_restore_[0-9]{14}_[0-9]+', name), name
        assert len(name.encode('utf-8')) <= 63
        assert original not in name


def test_restore_runbooks_use_snapshot_variable_and_container_user() -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        text = path.read_text(encoding='utf-8')
        assert '<snapshot-id>' not in text
        assert 'SNAPSHOT_ID' in text
        assert 'read -r SNAPSHOT_ID' in text
        assert '--entrypoint restic backup' in text
        assert '--no-cache snapshots' in text
        assert '--no-cache check' in text
        assert '--no-cache ls "$SNAPSHOT_ID"' in text
        assert '--no-cache dump "$SNAPSHOT_ID"' in text
        assert '$DC exec backup restic snapshots' not in text
        assert '$DC exec backup restic dump' not in text
        assert 'exec -T postgres sh -c' in text
        assert 'restore_db="$1" -U "$POSTGRES_USER"' in text
        assert 'restore_db="$RESTORE_DB" -U "$POSTGRES_USER"' not in text
        assert '--entrypoint sh bot' in text
        assert '--entrypoint sh backup' in text
        assert 'exec -T postgres sh -c \'printf "%s" "$POSTGRES_DB"\'' not in text


@pytest.mark.skipif(BASH is None, reason='bash is not available')
def test_cutover_ordering_and_rollback_file_restore(tmp_path: Path) -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        text = path.read_text(encoding='utf-8')
        if path.name == 'backups-google-drive.md':
            text = _section(text, '### 8.3.', '### 8.4.')
        else:
            text = _section(text, '## 3.', '## 4.')
        copy_index = text.index('install -m 600 .env "$ENV_BACKUP"')
        edit_index = text.index('Set exactly in .env: POSTGRES_DB=')
        pause_index = text.index('read -r _')
        preflight_index = text.index('PRE_BOT_DB=')
        start_index = text.index('dc_recover up -d --no-deps --force-recreate bot backup')
        rollback_index = text.index('install -m 600 "$ENV_BACKUP" .env')

        assert copy_index < edit_index < pause_index < preflight_index < start_index
        assert start_index < rollback_index

        original = 'ORIGINAL_DB=old-db\n'
        (tmp_path / '.env').write_text(original, encoding='utf-8')
        rollback = tmp_path / 'rollback.env'
        script = (
            f'ENV_BACKUP={shlex.quote(str(rollback))}\n'
            'install -m 600 .env "$ENV_BACKUP"\n'
            'printf "NEW_DB=new-db\\n" > .env\n'
            'install -m 600 "$ENV_BACKUP" .env\n'
        )
        result = subprocess.run(  # noqa: S603 - fixed bash binary
            [BASH, '-c', script],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert (tmp_path / '.env').read_text(encoding='utf-8') == original


def test_new_host_reads_snapshot_before_installing_env() -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        full_text = path.read_text(encoding='utf-8')
        text = full_text
        if path.name == 'backups-google-drive.md':
            text = _section(text, '### 8.4.', '## 9.')
        else:
            text = _section(text, '## 4.', '## Важно')
        read_index = text.index('--no-cache snapshots')
        install_env_index = text.index('install -m 600 "$RESTORE_DIR/.env" .env')
        assert read_index < install_env_index
        bootstrap = text[:install_env_index]
        assert 'compose.restore.yml' in full_text
        assert 'dc_bootstrap()' in full_text
        assert 'dc_trusted --profile backup' not in bootstrap
        assert '--entrypoint restic backup' in bootstrap
        assert text.index('dc_recover --profile backup build bot backup') > install_env_index
        assert 'up -d --no-deps postgres redis' in text
        assert 'pg_isready -U "$POSTGRES_USER" -d postgres' in text
        assert 'redis-cli ping' in text


def test_restore_bootstrap_does_not_depend_on_the_application() -> None:
    path = ROOT / 'docker' / 'backup' / 'compose.restore.yml'
    assert path.is_file(), 'fresh-host restore needs a standalone Compose file'
    config = yaml.safe_load(path.read_text(encoding='utf-8'))
    assert set(config['services']) == {'backup'}
    backup = config['services']['backup']
    assert backup['build']['context'] == '.'
    assert not {'env_file', 'depends_on', 'networks', 'ports', 'network_mode'} & backup.keys()
    assert set(backup['environment']) == {'RESTIC_REPOSITORY', 'RESTIC_PASSWORD_FILE', 'RCLONE_CONFIG'}
    assert len(backup['volumes']) == 2
    assert 'networks' not in config and 'volumes' not in config


@pytest.mark.skipif(BASH is None, reason='Bash is required for runbook tests')
def test_runbook_network_preflight_only_gates_up(tmp_path: Path) -> None:
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'base.yml').write_text('services: {}\n', encoding='utf-8')
    env = _compose_recovery_env(tmp_path, FAKE_COMPOSE_CONFIG_FAIL='1', PANEL_NETWORK_REQUIRED='1')
    env['EXPECTED_COMPOSE_PROJECT'] = 'custom'
    for doc in (ROOT / 'docs/backups-google-drive.md', ROOT / 'docker/backup/RESTORE.md'):
        wrappers = next(block for block in _bash_blocks(doc) if 'dc_trusted()' in block)
        for command in ('stop bot backup', '--profile backup run --rm backup snapshots', 'exec -T postgres true'):
            result = subprocess.run(  # noqa: S603 - fixed Bash, repository code, synthetic Docker
                [
                    BASH,
                    '-c',
                    f'{wrappers}\nPROJECT_DIR=$1\nTRUSTED_COMPOSE_FILES=(base.yml)\ndc_trusted {command}',
                    'bash',
                    str(project),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stderr
        result = subprocess.run(  # noqa: S603 - fixed Bash, repository code, synthetic Docker
            [
                BASH,
                '-c',
                f'{wrappers}\nPROJECT_DIR=$1\nTRUSTED_COMPOSE_FILES=(base.yml)\ndc_trusted up -d backup',
                'bash',
                str(project),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert 'cannot render effective Compose config' in result.stderr
        assert not any(call.endswith('up -d backup') for call in _fake_docker_calls(env))


def test_cutover_and_rollback_unset_host_db_and_verify_effective_config() -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        text = path.read_text(encoding='utf-8')
        assert text.count('unset POSTGRES_DB') >= 2
        assert text.count('up -d --no-deps --force-recreate bot backup') >= 2
        assert 'BOT_DB=' in text
        assert 'BACKUP_DB=' in text
        assert '$RESTORE_DB' in text
        assert '$ORIGINAL_DB' in text
        assert 'ROLLBACK_DIR=' in text
        assert 'rollback-$(date -u +%Y%m%dT%H%M%SZ)-$$' in text
        assert '/tmp/remnawave-env-rollback' not in text  # noqa: S108 - forbidden old path
        assert '${TMPDIR' not in text


def test_restore_runbooks_clarify_tar_members_and_check_lock() -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        text = path.read_text(encoding='utf-8')
        assert 'В каталоге должны быть ровно `db.dump` и `.env`' not in text
        assert 'tar' in text.lower()
        assert '--no-cache check' in text
        assert 'lock' in text.lower()
        assert 'одноразовый бот' not in text


def test_restore_runbooks_keep_a_stable_private_restore_directory() -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        text = path.read_text(encoding='utf-8')
        assert 'ALTER DATABASE' not in text
        assert 'POSTGRES_DB' in text
        assert '/var/lib/remnawave-recovery' in text
        assert 'install -d -m 700 "$RESTORE_DIR"' in text
        assert 'umask 077' in text
        assert '< "$RESTORE_DIR/db.dump"' in text
        assert 'trap \'rm -rf "$RESTORE_DIR"\' EXIT' not in text
        assert 'mktemp -d /tmp/remnawave-restore' not in text


def test_docs_document_compose_capture_contract_and_legacy_path() -> None:
    backup_doc = (ROOT / 'docs' / 'backups-google-drive.md').read_text(encoding='utf-8')
    restore_doc = (ROOT / 'docker' / 'backup' / 'RESTORE.md').read_text(encoding='utf-8')

    for text in (backup_doc, restore_doc):
        assert 'BACKUP_COMPOSE_FILES' in text
        assert 'BACKUP_COMPOSE_PROJECT' in text
        assert 'restore-compose.sh' in text
        assert '--legacy-data-only' in text
        assert 'compose/project-name' in text
        assert 'compose/files.list' in text
        assert '16e67daf' in text
        assert ('BACKUP_COMPOSE_FILES=docker-compose.yml:docker-compose.local.yml:docker-compose.asteria.yml') in text
        assert 'EXPECTED_COMPOSE_PROJECT' in text
        assert 'compose-recovery.sh' in text

    assert 'EXPECTED_COMPOSE_PROJECT=bot-asteria' in backup_doc
    assert 'BACKUP_RESTIC_REPOSITORY=rclone:gdrive:remnawave-restic' in backup_doc
    assert 'remnawave-restic-test' in backup_doc
    assert 'remnawave-bot-test' in backup_doc
    assert 'BOT_NETWORK_SUBNET=172.31.0.0/24' in backup_doc
    assert 'remnawave-network' in backup_doc


def test_recovery_runbooks_use_the_central_bash_helper() -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        text = path.read_text(encoding='utf-8')
        assert 'Bash' in text
        assert 'одном Bash shell' in text
        assert 'compose-recovery.sh' in text
        assert 'dc_live()' in text
        assert 'dc_restored()' in text
        assert 'dc_trusted()' in text
        assert 'dc_recover()' in text
        assert '--verify-live-match' in text
        assert '--capture-status' in text
        assert '--preflight-network' in text
        assert 'EXPECTED_COMPOSE_PROJECT' in text
        assert 'read -r -a TRUSTED_COMPOSE_FILES' in text
        assert 'eval "$(' not in text
        assert 'eval $(cat' not in text
        assert 'source "$restore_dir' not in text
        assert 'DC+=(' not in text
        assert 'REPO_COMPOSE_FILES=(' not in text
        assert 'TRUSTED_COMPOSE_FILES=(docker-compose' not in text
        assert 'diff -u' not in text
        assert "grep -q '^BACKUP_COMPOSE_FILES=$'" not in text


def test_recovery_sections_do_not_hardcode_project_or_compose_files() -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        text = path.read_text(encoding='utf-8')
        if path.name == 'backups-google-drive.md':
            recovery = _section(text, '### 8.1.', '## 9.')
        else:
            recovery = _section(text, '## 1.', '## Важно')
        assert 'DC="docker compose' not in recovery
        assert '$DC ' not in recovery
        assert '-p bot-asteria' not in recovery
        assert 'dc_recover' in recovery
        assert 'dc_live' in recovery


def test_recovery_runbooks_preflight_operator_managed_panel_network() -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        text = path.read_text(encoding='utf-8')
        assert '--preflight-network' in text
        assert '--require-panel-network' in text
        assert '--panel-project' in text
        assert 'PANEL_NETWORK_REQUIRED' in text
        assert 'external: true' in text
        assert 'docker network inspect remnawave-network' in text
        assert 'docker network create' not in text
        assert 'docker-compose.local.yml' in text
        assert 'legacy' in text.lower()


def test_recovery_runbooks_document_next_backup_and_test_isolation() -> None:
    backup_doc = (ROOT / 'docs' / 'backups-google-drive.md').read_text(encoding='utf-8')
    restore_doc = (ROOT / 'docker' / 'backup' / 'RESTORE.md').read_text(encoding='utf-8')

    for text in (backup_doc, restore_doc):
        assert 'запускается из checkout репозитория' in text
        assert 'не копируется в backup-образ' in text
        assert 'stale' in text.lower()
        assert 'BACKUP_COMPOSE_FILES=' in text
        assert '--capture-status' in text
        assert 'cmp -s' in text

    assert 'container_name' in backup_doc
    assert 'WEB_API_PORT=18080' in backup_doc
    assert '172.31.0.0/24' in backup_doc
    assert '172.31.0.1' in backup_doc
    assert 'не переименовывай live project' in backup_doc.lower()
    assert 'local override' in backup_doc.lower()
    assert 'external: true' in backup_doc
    assert 'docker-compose.local.yml' in backup_doc


def test_existing_host_verifies_snapshot_before_stop() -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        text = path.read_text(encoding='utf-8')
        if path.name == 'backups-google-drive.md':
            section = _section(text, '### 8.1.', '### 8.2.')
        else:
            section = _section(text, '## 1.', '## 2.')
        verify = section.index('--verify-live-match')
        stop = section.index('dc_recover stop bot backup')
        assert verify < stop
        assert 'Install exact recovered Compose files' not in section
        assert 'files.list' in section


def test_fresh_host_install_prompt_is_metadata_only() -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        text = path.read_text(encoding='utf-8')
        if path.name == 'backups-google-drive.md':
            section = _section(text, '### 8.4.', '## 9.')
        else:
            section = _section(text, '## 4.', '## Важно')
        install_prompt = section.index('Install exact recovered Compose files')
        metadata_guard = section.rfind(
            'if [ -f "$RESTORE_DIR/compose/files.list" ]',
            0,
            install_prompt,
        )
        capture_check = section.index('--capture-status')
        assert metadata_guard != -1
        assert install_prompt < capture_check
        assert section.index('dc_recover() { dc_trusted "$@"; }') < install_prompt
        assert 'INSTALL_COMPOSE=legacy' not in section
        assert "grep -q '^BACKUP_COMPOSE_FILES=$'" not in section


def test_local_override_network_is_managed_not_external() -> None:
    local_compose = yaml.safe_load((ROOT / 'docker-compose.local.yml').read_text(encoding='utf-8'))
    network = local_compose['networks']['remnawave-network']

    assert network['name'] == 'remnawave-network'
    assert network.get('external') is not True
    assert network.get('driver') == 'bridge'


def test_checked_in_config_keeps_compose_capture_opt_in() -> None:
    env_example = (ROOT / '.env.example').read_text(encoding='utf-8')
    compose_text = COMPOSE.read_text(encoding='utf-8')

    assert '\nBACKUP_COMPOSE_FILES=' not in env_example
    assert '\nCOMPOSE_PROJECT_NAME=bot-asteria' not in env_example
    assert 'docker-compose.asteria.yml' not in env_example
    assert 'bot-asteria' not in compose_text
    assert 'docker-compose.asteria.yml' not in compose_text


def test_image_installs_and_verifies_restic_stdin_command_support() -> None:
    dockerfile = DOCKERFILE.read_text(encoding='utf-8')

    assert 'restic' in dockerfile
    assert 'rclone' in dockerfile
    assert '--stdin-from-command' in dockerfile
