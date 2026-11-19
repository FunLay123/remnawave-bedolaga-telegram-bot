# Google Drive Backups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `backup` Compose service that dumps PostgreSQL every 5 minutes, uploads encrypted dumps plus a paired `.env` to Google Drive, and keeps a fixed GFS retention.

**Architecture:** A small sidecar (`postgres:18-alpine` + rclone) runs a POSIX shell loop. Dumps and `.env` copies go to a local pending queue, uploads go to a crypt remote with plaintext filenames, and count-based pruning runs by file modification time. The service lives in `docker-compose.yml` under the `backup` profile and is configured entirely through `.env`. A plaintext `README.md` with restore instructions is uploaded to the root of the Drive folder.

**Tech Stack:** Docker Compose, PostgreSQL 18, `pg_dump`/`pg_restore`, rclone (drive + crypt), POSIX sh/BusyBox.

**Spec:** `docs/superpowers/specs/2026-11-19-google-drive-backups-design.md`

## Global Constraints

- PostgreSQL 18, database `remnawave_bot`, user `remnawave_user` (values come from existing `POSTGRES_*`).
- `filename_encryption=off`, `directory_name_encryption=false` — plaintext names, encrypted contents.
- Retention: `5min=12`, `hourly=24`, `daily=30`, `weekly=12`, `monthly=12`; every tier holds dump + `.env` pairs.
- Dump interval 300 s, upload interval 300 s (configurable via `BACKUP_GDRIVE_*`).
- Default `TZ=Europe/Moscow`; file names use local time, logs use UTC. `LC_ALL=C` guarantees English month names.
- Dump names: `<DD>_<Month>_<YYYY>_<HH>-<MM>`; no seconds; collision suffix `_2`, `_3`, ...
- Root Drive folder: `remnawave-bot` (configurable via `BACKUP_GDRIVE_FOLDER`).
- `README.md` is plaintext at the folder root; all other files are encrypted.
- No changes to bot Python code, tests, or the bot image.
- Secrets only in `.env`; `.env.example` gets empty placeholders.
- The `backup` service must not start unless the `backup` Compose profile is enabled.
- Remote deletes must use `--drive-use-trash=false`.
- Dumps use `pg_dump -Fc --no-owner --no-privileges --no-password`.
- Pruning is by modification time, not by name (month names do not sort chronologically).

## Review Focus

- A failed `pg_dump` or `rclone` call must not kill the loop or leave a zero-byte/partial dump.
- A Drive outage must leave the dump/`.env` pair in the pending queue and retry it on the next cycle.
- Pruning must delete the oldest files and never the newest N, in every tier and for both file kinds.
- Without `COMPOSE_PROFILES=backup`, the service must not start and `docker compose` must not require `RCLONE_*`.
- Two dumps inside the same minute must not overwrite each other.
- Restoring a dump must not depend on the bot's own backup code or `BackupService`.

---

### Task 1: Naming and retention helpers

**Files:**
- Create: `docker/backup/backup.sh`

**Interfaces:**
- `drop_newest KEEP` — reads a newest-first list from stdin and writes all but the first `KEEP` lines to stdout.
- `timestamp_name` — prints the current local time as `<DD>_<Month>_<YYYY>_<HH>-<MM>` with English month names.
- `docker/backup/backup.sh --self-test` — runs assertions and exits `0`/`1`; must not read environment variables or call `pg_dump`/`rclone`.

- [ ] **Step 1: Write the failing test for `drop_newest`**

Create `docker/backup/backup.sh`:

```sh
#!/bin/sh
set -u
export LC_ALL=C

drop_newest() {
    :
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
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `sh docker/backup/backup.sh --self-test`
Expected: exit code 1 and `self-test FAIL: drop_newest expected 3, got 1`.

- [ ] **Step 3: Implement `drop_newest`**

```sh
drop_newest() {
    tail -n +$(($1 + 1))
}
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `sh docker/backup/backup.sh --self-test`
Expected: `self-test ok`, exit code 0.

- [ ] **Step 5: Write the failing test for `timestamp_name`**

Add before the final `echo "self-test ok"`:

```sh
    name=$(timestamp_name)
    if ! printf '%s\n' "$name" | grep -Eq '^[0-9]{2}_[A-Z][a-z]+_[0-9]{4}_[0-9]{2}-[0-9]{2}$'; then
        echo "self-test FAIL: bad timestamp name: $name" >&2
        return 1
    fi
```

Add the stub next to `drop_newest`:

```sh
timestamp_name() {
    echo "bad"
}
```

- [ ] **Step 6: Run the test and confirm it fails**

Run: `sh docker/backup/backup.sh --self-test`
Expected: exit code 1 and `self-test FAIL: bad timestamp name: bad`.

- [ ] **Step 7: Implement `timestamp_name`**

```sh
timestamp_name() {
    date +%d_%B_%Y_%H-%M
}
```

- [ ] **Step 8: Run the test and confirm it passes**

Run: `sh docker/backup/backup.sh --self-test`
Expected: `self-test ok`, exit code 0.

- [ ] **Step 9: Commit**

```bash
git add docker/backup/backup.sh docs/superpowers/specs/2026-11-19-google-drive-backups-design.md docs/superpowers/plans/2026-11-19-google-drive-backups.md
git commit -m "docs: add Google Drive backup design and naming helpers"
```

---

### Task 2: Backup loop, paired `.env`, uploads, tiers and pruning

**Files:**
- Modify: `docker/backup/backup.sh`

**Interfaces:**
- Environment variables (defaults in the script):
  - `BACKUP_DIR=/backups`, `ENV_FILE=/src/.env`, `README_SOURCE=/usr/local/share/remnawave-backup/RESTORE.md`
  - `DUMP_INTERVAL_SECONDS=300`, `UPLOAD_INTERVAL_SECONDS=300`
  - `KEEP_5MIN=12`, `KEEP_HOURLY=24`, `KEEP_DAILY=30`, `KEEP_WEEKLY=12`, `KEEP_MONTHLY=12`
  - `RCLONE_REMOTE=gcrypt`, `RCLONE_PATH=`, `RCLONE_DOCS_REMOTE=gdrive`, `BACKUP_GDRIVE_FOLDER=remnawave-bot`
- Functions:
  - `unique_base` — returns `<timestamp>` or `<timestamp>_N` if a dump with that name already exists in `pending`/`local`.
  - `dump_database` — writes `pending/<base>.dump` and `pending/<base>.env.bak`; removes partial files; returns non-zero on dump failure.
  - `upload_pending` — copies every `pending/*.dump` and `pending/*.env.bak` to `$REMOTE_PREFIX/5min/`, moves successful files to `local/`, writes `state/last_success`; returns non-zero on the first failure.
  - `copy_tier DIR KEEP MARKER VALUE` — copies the newest dump and its paired `.env` to `$REMOTE_PREFIX/$DIR/` once per marker period, then prunes that tier.
  - `prune_remote DIR KEEP SUFFIX` — lists `$REMOTE_PREFIX/$DIR`, filters by suffix, sorts by modification time, deletes all but the newest `KEEP` with `--drive-use-trash=false`.
  - `prune_local` — keeps the newest `KEEP_5MIN` dumps and the newest `KEEP_5MIN` `.env` files in `local/`.
  - `upload_readme` / `upload_readme_once` — uploads `RESTORE.md` to `gdrive:$BACKUP_GDRIVE_FOLDER/README.md` at startup and once per day.
  - `main` — the aligned loop.

- [ ] **Step 1: Add defaults and path setup**

After `export LC_ALL=C`, add the defaults and derived paths:

```sh
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
```

- [ ] **Step 2: Implement `log`, `unique_base` and `dump_database`**

```sh
log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
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
    tmp="$PENDING/.${base}.dump.tmp"
    final="$PENDING/${base}.dump"

    if ! pg_dump --no-owner --no-privileges --no-password -Fc -f "$tmp"; then
        rm -f "$tmp"
        log "dump FAILED"
        return 1
    fi

    if [ ! -s "$tmp" ]; then
        rm -f "$tmp"
        log "dump FAILED: empty file"
        return 1
    fi

    mv "$tmp" "$final"

    if [ -f "$ENV_FILE" ] && cp "$ENV_FILE" "$PENDING/${base}.env.bak"; then
        log "dump: ${base}.dump + .env"
    else
        log "dump: ${base}.dump (env copy FAILED)"
    fi
}
```

- [ ] **Step 3: Implement `upload_pending`**

```sh
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
```

- [ ] **Step 4: Implement `prune_remote`, `copy_tier` and `prune_local`**

```sh
prune_remote() {
    dir=$1
    keep=$2
    suffix=$3

    rclone lsf "$REMOTE_PREFIX/$dir" --files-only --format "tp" --separator "|" \
        --time-format "2006-01-02T15:04:05" 2>/dev/null \
        | grep "|.*${suffix}$" \
        | sort -r \
        | cut -d'|' -f2- \
        | drop_newest "$keep" \
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

    if rclone copyto "$newest" "$REMOTE_PREFIX/$dir/$(basename "$newest")"; then
        if [ -f "$env_file" ]; then
            rclone copyto "$env_file" "$REMOTE_PREFIX/$dir/$(basename "$env_file")" \
                || log "tier env copy FAILED: $dir/$(basename "$env_file")"
        fi
        echo "$value" > "$STATE/$marker"
        prune_remote "$dir" "$keep" ".dump"
        prune_remote "$dir" "$keep" ".env.bak"
    else
        log "tier copy FAILED: $dir/$(basename "$newest")"
    fi
}

prune_local() {
    ls -1t "$LOCAL"/*.dump 2>/dev/null | drop_newest "$KEEP_5MIN" | while IFS= read -r f; do
        [ -n "$f" ] && rm -f "$f"
    done

    ls -1t "$LOCAL"/*.env.bak 2>/dev/null | drop_newest "$KEEP_5MIN" | while IFS= read -r f; do
        [ -n "$f" ] && rm -f "$f"
    done
}
```

- [ ] **Step 5: Implement README upload**

```sh
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
```

- [ ] **Step 6: Implement `main` and the dispatcher**

```sh
main() {
    mkdir -p "$PENDING" "$LOCAL" "$STATE"
    rm -f "$PENDING"/.*.dump.tmp

    if [ -z "${RCLONE_CONFIG_GDRIVE_TOKEN:-}" ] || [ -z "${RCLONE_CONFIG_GCRYPT_PASSWORD:-}" ]; then
        log "RCLONE_CONFIG_GDRIVE_TOKEN / RCLONE_CONFIG_GCRYPT_PASSWORD are empty; configure .env"
        exit 1
    fi

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
```

- [ ] **Step 7: Verify syntax and self-test**

Run: `sh -n docker/backup/backup.sh && sh docker/backup/backup.sh --self-test`
Expected: no syntax errors, `self-test ok`, exit code 0.

- [ ] **Step 8: Commit**

```bash
git add docker/backup/backup.sh
git commit -m "feat: add Google Drive backup loop with paired env and GFS retention"
```

---

### Task 3: Dockerfile, healthcheck, Compose profile and env example

**Files:**
- Create: `docker/backup/Dockerfile`
- Create: `docker/backup/healthcheck.sh`
- Create: `docker/backup/RESTORE.md`
- Modify: `docker-compose.yml`
- Modify: `.env.example`

**Interfaces:**
- `healthcheck.sh` — exits `0` when `/backups/state/last_success` is younger than `HEALTHCHECK_MAX_AGE_SECONDS` (default 900), otherwise `1`.
- Compose service `backup` — build context `./docker/backup`, profile `backup`, volume `backup_data:/backups`.

- [ ] **Step 1: Create `healthcheck.sh`**

```sh
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
```

- [ ] **Step 2: Create `RESTORE.md`**

Plaintext instructions uploaded to the Drive root. It must cover: folder structure, retention counts, the fact that contents are encrypted and names are open, the need for the rclone config and crypt password, how to download a dump and its `.env`, how to restore with `pg_restore`, how to restore `.env`, and a note that the `.env` backup contains secrets. No actual secrets in the file.

- [ ] **Step 3: Create the Dockerfile**

```dockerfile
FROM postgres:18-alpine

RUN apk add --no-cache rclone tzdata

COPY backup.sh /usr/local/bin/backup.sh
COPY healthcheck.sh /usr/local/bin/healthcheck.sh
COPY RESTORE.md /usr/local/share/remnawave-backup/RESTORE.md

RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/healthcheck.sh \
    && /usr/local/bin/backup.sh --self-test

ENTRYPOINT ["/usr/local/bin/backup.sh"]
```

- [ ] **Step 4: Add the `backup` service to `docker-compose.yml`**

Insert after the `bot` service, before the top-level `volumes:` block:

```yaml
  backup:
    profiles: ['backup']
    build:
      context: ./docker/backup
    container_name: remnawave_bot_backup
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      PGHOST: postgres
      PGPORT: '5432'
      PGDATABASE: '${POSTGRES_DB:-remnawave_bot}'
      PGUSER: '${POSTGRES_USER:-remnawave_user}'
      PGPASSWORD: '${POSTGRES_PASSWORD:-secure_password_123}'
      PGCONNECT_TIMEOUT: '10'
      TZ: '${TZ:-Europe/Moscow}'
      BACKUP_DIR: /backups
      ENV_FILE: /src/.env
      README_SOURCE: /usr/local/share/remnawave-backup/RESTORE.md
      DUMP_INTERVAL_SECONDS: '${BACKUP_GDRIVE_DUMP_INTERVAL_SECONDS:-300}'
      UPLOAD_INTERVAL_SECONDS: '${BACKUP_GDRIVE_UPLOAD_INTERVAL_SECONDS:-300}'
      KEEP_5MIN: '${BACKUP_GDRIVE_KEEP_5MIN:-12}'
      KEEP_HOURLY: '${BACKUP_GDRIVE_KEEP_HOURLY:-24}'
      KEEP_DAILY: '${BACKUP_GDRIVE_KEEP_DAILY:-30}'
      KEEP_WEEKLY: '${BACKUP_GDRIVE_KEEP_WEEKLY:-12}'
      KEEP_MONTHLY: '${BACKUP_GDRIVE_KEEP_MONTHLY:-12}'
      HEALTHCHECK_MAX_AGE_SECONDS: '${BACKUP_GDRIVE_HEALTHCHECK_MAX_AGE_SECONDS:-900}'
      RCLONE_REMOTE: gcrypt
      RCLONE_PATH: ''
      RCLONE_DOCS_REMOTE: gdrive
      BACKUP_GDRIVE_FOLDER: '${BACKUP_GDRIVE_FOLDER:-remnawave-bot}'
      RCLONE_CONFIG_GDRIVE_TYPE: drive
      RCLONE_CONFIG_GDRIVE_SCOPE: drive
      RCLONE_CONFIG_GDRIVE_TOKEN: '${RCLONE_CONFIG_GDRIVE_TOKEN:-}'
      RCLONE_CONFIG_GCRYPT_TYPE: crypt
      RCLONE_CONFIG_GCRYPT_REMOTE: 'gdrive:${BACKUP_GDRIVE_FOLDER:-remnawave-bot}'
      RCLONE_CONFIG_GCRYPT_FILENAME_ENCRYPTION: 'off'
      RCLONE_CONFIG_GCRYPT_DIRECTORY_NAME_ENCRYPTION: 'false'
      RCLONE_CONFIG_GCRYPT_PASSWORD: '${RCLONE_CONFIG_GCRYPT_PASSWORD:-}'
    volumes:
      - ./.env:/src/.env:ro
      - backup_data:/backups
    networks:
      - bot_network
    healthcheck:
      test: ['CMD', '/usr/local/bin/healthcheck.sh']
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 120s
```

Add under the top-level `volumes:` block:

```yaml
  backup_data:
    driver: local
```

- [ ] **Step 5: Add the env block to `.env.example`**

Insert after the existing backup section, before `# ===== ПРОВЕРКА ОБНОВЛЕНИЙ БОТА =====`:

```dotenv
# ===== БЭКАПЫ В GOOGLE DRIVE (ОПЦИОНАЛЬНО) =====
# Включается профилем compose: раскомментируй строку ниже и заполни RCLONE_*.
# COMPOSE_PROFILES=backup

BACKUP_GDRIVE_FOLDER=remnawave-bot
BACKUP_GDRIVE_DUMP_INTERVAL_SECONDS=300
BACKUP_GDRIVE_UPLOAD_INTERVAL_SECONDS=300
BACKUP_GDRIVE_KEEP_5MIN=12
BACKUP_GDRIVE_KEEP_HOURLY=24
BACKUP_GDRIVE_KEEP_DAILY=30
BACKUP_GDRIVE_KEEP_WEEKLY=12
BACKUP_GDRIVE_KEEP_MONTHLY=12
BACKUP_GDRIVE_HEALTHCHECK_MAX_AGE_SECONDS=900

# OAuth-токен rclone для Google Drive.
# Получить: rclone authorize "drive" (на машине с браузером), вставить JSON сюда.
RCLONE_CONFIG_GDRIVE_TOKEN=

# Пароль шифрования (obscured). Получить: rclone obscure 'твой-пароль'
# Обязательно сохрани сам пароль в менеджере паролей.
RCLONE_CONFIG_GCRYPT_PASSWORD=
```

- [ ] **Step 6: Verify syntax and self-test**

Run: `sh -n docker/backup/healthcheck.sh && sh -n docker/backup/backup.sh && sh docker/backup/backup.sh --self-test`
Expected: no syntax errors, `self-test ok`.

- [ ] **Step 7: Commit**

```bash
git add docker/backup/Dockerfile docker/backup/healthcheck.sh docker/backup/RESTORE.md docker-compose.yml .env.example
git commit -m "feat: add opt-in backup service with healthcheck and env config"
```

---

### Task 4: Documentation and changelog

**Files:**
- Create: `docs/backups-google-drive.md`
- Modify: `../claude-changes.md` (outside the repository; not committed)

- [ ] **Step 1: Write the setup and restore guide**

`docs/backups-google-drive.md` must cover:

1. What it does: 5-minute `pg_dump`, paired `.env`, upload to `gcrypt:5min/`, GFS tiers, plaintext `README.md` at the root, healthcheck.
2. Google OAuth token: `rclone authorize "drive"` on a machine with a browser; paste JSON into `RCLONE_CONFIG_GDRIVE_TOKEN`. If a custom OAuth client is used, put it in production mode to avoid 7-day refresh-token expiry.
3. Crypt password: `rclone obscure '<password>'`; put the output into `RCLONE_CONFIG_GCRYPT_PASSWORD`; save the plain password and the rclone config off-server.
4. Enable: set `COMPOSE_PROFILES=backup` and `BACKUP_GDRIVE_FOLDER=remnawave-bot` in `.env`; run the existing compose command with `up -d --build`.
5. Verify: `ps`, `logs -f backup`, `rclone lsf gcrypt:5min`, count after 13 uploads, `docker inspect` health.
6. Restore the database:
   ```bash
   DC="docker compose -f docker-compose.yml -f docker-compose.local.yml -f docker-compose.asteria.yml"
   $DC stop bot
   $DC exec backup rclone copyto gcrypt:5min/26_January_2026_22-30.dump /backups/restore.dump
   $DC exec -T backup sh -c 'pg_restore -U "$PGUSER" -d "$PGDATABASE" --clean --if-exists --no-owner --no-privileges /backups/restore.dump'
   $DC start bot
   ```
7. Restore `.env`:
   ```bash
   $DC stop bot
   $DC exec backup rclone copyto gcrypt:5min/26_January_2026_22-30.env.bak /backups/env.restore
   $DC cp backup:/backups/env.restore ./.env
   chmod 600 .env
   $DC up -d bot
   ```
8. Troubleshooting: empty `RCLONE_*` startup error, 401/403 (token), `pg_dump` auth failure (`POSTGRES_*`), unhealthy healthcheck (`logs backup`, `pending/`), collision suffix `_2`.
9. Security: `.env` contains payment keys; the crypt password is mandatory for offsite copies; never commit `.env`.

- [ ] **Step 2: Append the changelog entry**

Append to `../claude-changes.md`:

```markdown
## 2026-11-19 — Google Drive backups

**Scope:** `docker-compose.yml`, `docker/backup/*`, `.env.example`, `docs/backups-google-drive.md`.
**What:** Added an opt-in `backup` Compose profile: 5-minute `pg_dump` plus paired `.env`, encrypted rclone upload with plaintext names, fixed GFS retention (12/24/30/12/12), plaintext restore README in the Drive root, healthcheck.
**Why:** The bot's built-in daily backup is local-only and its ORM export misses tables; payments need a frequent offsite copy independent of the bot process.
```

- [ ] **Step 3: Verify docs exist and mention the key paths**

Run: `test -f docs/backups-google-drive.md && grep -q 'COMPOSE_PROFILES=backup' docs/backups-google-drive.md && grep -q 'pg_restore' docs/backups-google-drive.md`
Expected: exit code 0.

- [ ] **Step 4: Commit**

```bash
git add docs/backups-google-drive.md
git commit -m "docs: document Google Drive backup setup and restore"
```

---

### Task 5: Server verification (manual)

**Files:** none

This task runs on the deployment server; it cannot be executed from the development machine.

- [ ] **Step 1: Enable and start**

Add `COMPOSE_PROFILES=backup` and the two `RCLONE_*` secrets to `.env`, then run the existing compose command with `up -d --build`.

- [ ] **Step 2: Verify uploads**

```bash
DC="docker compose -f docker-compose.yml -f docker-compose.local.yml -f docker-compose.asteria.yml"
$DC logs --tail=100 backup
$DC exec backup rclone lsf gcrypt:5min --files-only
$DC exec backup rclone lsf gcrypt:hourly --files-only
$DC exec backup rclone lsf gcrypt:daily --files-only
```

Expected: a new pair every 5 minutes; the first upload seeds all tiers.

- [ ] **Step 3: Verify retention and health**

```bash
$DC exec backup sh -c 'rclone lsf gcrypt:5min --files-only | grep -c "\.dump$"'
docker inspect --format '{{.State.Health.Status}}' remnawave_bot_backup
```

Expected: `12` dumps after 13 uploads; `healthy`.

- [ ] **Step 4: Restore drill**

```bash
$DC exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" remnawave_bot_restore'
$DC exec -T backup sh -c 'pg_restore -U "$PGUSER" -d remnawave_bot_restore --no-owner --no-privileges /backups/local/<name>.dump'
$DC exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d remnawave_bot_restore -c "SELECT count(*) FROM users;"'
$DC exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" remnawave_bot_restore'
```

Expected: restore exits 0, `users` count is non-zero, drop succeeds.
