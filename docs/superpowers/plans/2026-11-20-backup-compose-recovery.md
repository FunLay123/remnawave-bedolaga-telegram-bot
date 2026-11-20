# Compose Configuration Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax.

**Goal:** Restore the right bot's declarative Compose inputs with its Restic data snapshot.

**Architecture:** Optional exact source capture in the existing bounded private staging area, plus project-checked offline restore instructions/verification. Old archives and schedules remain compatible.

**Tech Stack:** Docker Compose, shell, tar, Restic, pytest.

**Spec:** `docs/superpowers/specs/2026-11-20-backup-compose-recovery-design.md`

## Global Constraints

- Preserve old snapshots and disabled behavior. No automatic Docker or production changes.
- No source discovery, Docker socket, live network state, backup-secrets, or env plaintext output.
- Keep existing sealed single-tar publication, tmpfs and freshness semantics.
- Local synthetic verification only. Do not access actual credentials or `.env`.

## Review Focus

Reject mismatched project, unsafe path, unreadable/missing override, duplicate input, and archive claiming Compose metadata that is incomplete. A legacy two-member archive remains readable with a clear no-metadata result.

### Task 1: Capture and verify project-specific Compose inputs

**Files:** `docker/backup/backup.sh`, `docker/backup/restore-compose.sh`, `docker-compose.yml`, `.env.example`, `tests/test_backup_restic_sidecar.py`, `docker/backup/RESTORE.md`, `docs/backups-google-drive.md`.

**Interfaces:** Input `BACKUP_COMPOSE_FILES`, effective `BACKUP_COMPOSE_PROJECT`; tar entries `compose/project-name`, `compose/files.list` and `compose/<basename>` (ordered list is authoritative).

- [x] Add pytest tests: prod synthetic base/local/asteria input byte-for-byte, list order, ignore nonlisted test file, disabled tar remains exactly two members, invalid project/file/path/link/duplicate or missing input prevents a snapshot and leaves last_success unchanged, legacy restore usable, project mismatch rejected before Compose config extraction/application.
- [x] Run targeted tests against unmodified shell and show expected red.
- [x] Implement archive staging, wiring and project-checked isolated restore use; update both guides including exact prod example, distinct test repository/project, and expected shared external network behavior.
- [x] Run tests, lint and shell self-test; if using container checks isolate temp resources; report exact test evidence.
- [x] Commit scoped changes only, no integration or deployment.
