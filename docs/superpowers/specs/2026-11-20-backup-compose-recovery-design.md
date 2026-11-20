# Compose configuration in Restic recovery snapshots

## Goal

Keep the exact ordered Compose inputs of each bot alongside its PostgreSQL dump and `.env`, and detect project mismatch before using recovered configuration. Asteria production uses project `bot-asteria` and an extra `docker-compose.asteria.yml`; test must not share its Restic repository or private bot network.

## Design

Opt in with `BACKUP_COMPOSE_FILES`, a colon-separated ordered list of explicit repository-root Compose YAML basenames. Compose supplies the effective project name as `BACKUP_COMPOSE_PROJECT` via `${COMPOSE_PROJECT_NAME:-}`. An empty file list preserves the legacy two-file snapshot. If enabled, accept only a valid lowercase Compose project identifier and unique safe basenames, each a readable nonempty regular non-symlink file beneath the same directory as the app `.env`. Never glob for inputs or copy Docker credentials, mounted secrets, local volumes, or live Docker state. `compose/project-name`, `compose/files.list` and `compose/<basename>` become additional members of the same encrypted tar; preserve list order. Invalid or missing selected configuration prevents publication and leaves freshness unchanged.

Provide a read-only restore inspection/extraction path using an explicit operator-supplied expected project. Never source a manifest or apply recovered settings over a running stack. On a fresh host review the inputs, pass explicit `-p` and ordered `-f` flags, and let Compose recreate project-local networks. The existing shared panel network is an independently managed dependency: verify it exists and intentionally attach only through the appropriate Compose inputs. No Docker socket, network inspect archive, networks automatically changed, DB switch automation, server changes or push. The old two-member snapshot stays usable for data recovery only, with an explicit notice that Compose metadata is missing. Asteria configuration is an operator example, not checked-in private config.

## Approaches

Explicit source list + identity selected: portable, reviewable, and independent of runtime permissions. Auto-discovering every override would risk archiving inactive/test configuration. Capturing live Docker state would need a socket and recreate host-specific attachments; not needed for declarative recovery.
