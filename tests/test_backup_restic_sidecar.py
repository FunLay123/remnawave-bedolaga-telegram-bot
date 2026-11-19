"""Regression tests for the optional Restic/rclone Compose backup sidecar.

The tests run the real POSIX shell scripts against fake ``pg_dump`` and ``restic``
executables, so they never touch a real database, Google Drive, or repository.
The two-files test inspects the tar stream produced by ``--stdin-from-command``;
that is the artifact Restic stores and the operator restores.
"""

from __future__ import annotations

import os
import re
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
COMPOSE = ROOT / 'docker-compose.yml'
DOCKERFILE = ROOT / 'docker' / 'backup' / 'Dockerfile'

SH = shutil.which('sh')
BASH = shutil.which('bash')

pytestmark = pytest.mark.skipif(SH is None, reason='POSIX shell is not available')


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


def _safe_environment() -> dict[str, str]:
    # Never inherit a developer's real RESTIC_*/RCLONE_*/BACKUP_* secrets.
    blocked = ('RESTIC_', 'RCLONE_', 'BACKUP_', 'PGPASSWORD')
    return {key: value for key, value in os.environ.items() if not key.upper().startswith(blocked)}


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
    assert 'db.dump .env' in backup_call


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


def test_restore_runbooks_use_snapshot_variable_and_container_user() -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        text = path.read_text(encoding='utf-8')
        assert '<snapshot-id>' not in text
        assert 'SNAPSHOT_ID' in text
        assert 'read -r SNAPSHOT_ID' in text
        assert 'restic dump "$SNAPSHOT_ID"' in text
        assert 'restic ls "$SNAPSHOT_ID"' in text
        assert 'exec -T postgres sh -c' in text
        assert 'restore_db="$1" -U "$POSTGRES_USER"' in text
        assert 'restore_db="$RESTORE_DB" -U "$POSTGRES_USER"' not in text


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
        assert '/tmp/remnawave-env-rollback' in text  # noqa: S108 - documented path
        assert '${TMPDIR' not in text


def test_restore_runbooks_clarify_tar_members_and_check_lock() -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        text = path.read_text(encoding='utf-8')
        assert 'В каталоге должны быть ровно `db.dump` и `.env`' not in text
        assert 'tar' in text.lower()
        assert 'restic check' in text
        assert 'lock' in text.lower()
        assert 'одноразовый бот' not in text


def test_restore_runbooks_avoid_hardcoded_rename_and_use_private_tmpdir() -> None:
    for path in (ROOT / 'docs' / 'backups-google-drive.md', ROOT / 'docker' / 'backup' / 'RESTORE.md'):
        text = path.read_text(encoding='utf-8')
        assert 'ALTER DATABASE' not in text
        assert 'POSTGRES_DB' in text
        assert 'mktemp -d' in text
        assert 'umask 077' in text
        assert '< "$RESTORE_DIR/db.dump"' in text


def test_image_installs_and_verifies_restic_stdin_command_support() -> None:
    dockerfile = DOCKERFILE.read_text(encoding='utf-8')

    assert 'restic' in dockerfile
    assert 'rclone' in dockerfile
    assert '--stdin-from-command' in dockerfile
