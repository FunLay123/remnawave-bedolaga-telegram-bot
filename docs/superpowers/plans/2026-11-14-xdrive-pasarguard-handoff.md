# Xdrive through PasarGuard — handoff

**Written for:** another AI agent picking up this work. The human partner is
Russian-speaking; the codebase mixes Russian comments with English docs.

## Mission

Build one subscription URL that carries the Remnawave profile plus an
xdrive-only profile from PasarGuard. The bot is the source of truth; PasarGuard
is a slave. One PasarGuard user per bot `Subscription`; xdrive traffic is
counted in the single Remnawave premium squad's pool. Merge only when the
client already receives Xray JSON. No Remnawave or PasarGuard source changes.

## Read first

1. Design: `docs/superpowers/specs/2026-11-14-xdrive-pasarguard-subscription-design.md`
2. Plan: `docs/superpowers/plans/2026-11-14-xdrive-pasarguard-subscription.md`

Both are uncommitted as of 2026-11-14. Commit them first only if the user asks.

## Current state

- Branch `redesign`, HEAD `435a2f8e feat: refine notification menu actions`.
  `fork/redesign` is at the same commit.
- The two spike commits (`77e67aa1`, `86ebcc5c`) were removed locally and from
  `fork/redesign` with a force-push. Do not reintroduce the spike code.
- No implementation code has been written. The plan's tasks 1–10 are the next
  step.
- The spike proved: PasarGuard generates a valid xdrive entry (VLESS encryption
  non-empty, `mux.enabled=true`, `xdriveSettings`; `xray run -test` passes with
  the xdrive fork), and the real client accepted a merged array of Remnawave +
  PasarGuard configs.

## Non-negotiable decisions

- Merge only when the upstream body is structurally Xray JSON. Never force a
  format. Base64, Clash/Mihomo, sing-box, Stash, HTML, `HEAD` and non-GET pass
  through unchanged.
- One PasarGuard user per subscription; no per-device UUIDs.
- PasarGuard is a slave: the projector reads the panel every pass and
  overwrites manual edits; the stored hash is audit only.
- `XDRIVE_ENABLED=false` means drain-to-disabled, not a no-op. The projector
  runs when PasarGuard is configured, even with the flag off.
- Exactly one positive premium squad. More than one disables xdrive for that
  subscription.
- Grace is not accounted in v1: an open grace overlay makes the subscription
  ineligible.
- Trial follows the premium-squad rule. The trial tariff must have a positive
  `server_traffic_limits` entry; same hosts + lower general limit is not enough.
- `data_limit` is absolute: `used_traffic_now + remaining_pool_bytes`;
  `data_limit_reset_strategy='no_reset'`.
- No xdrive before a `SubscriptionPremiumTraffic` row exists.
- Usernames: `<PASARGUARD_USERNAME_PREFIX><subscription.id>` (default `bx`);
  note: `xdrive-bot:<PASARGUARD_INSTANCE_ID>:sub:<id>`.

## Verified facts — do not re-derive

### PasarGuard API (exact routes)

- `POST /api/user` — create.
- `PUT /api/user/by-id/{user_id}` — modify (`expire`, `data_limit`,
  `group_ids`, `note`, `status`; `null`/omitted means no change).
- `PUT /api/user/by-id/{user_id}/disabled` — body `{"disabled": true|false}`.
- `POST /api/user/by-id/{user_id}/revoke_sub` — rotates the VLESS UUID and
  invalidates the public subscription token.
- `DELETE /api/user/by-id/{user_id}`.
- `GET /api/user/by-id/{user_id}` — user card.
- `GET /api/users` — paginated list; the operation loads
  `lifetime_used_traffic=True`, so both `used_traffic` and
  `lifetime_used_traffic` are available. No per-user requests are needed.
- `GET /api/user/{user_id}/subscription/xray` — admin-authenticated config
  bytes; no subscription token and no HWID registration.
- Auth: `X-Api-Key: <key>` (or bearer).

### PasarGuard semantics

- `User.is_limited = data_limit is not None and data_limit > 0 and
  data_limit <= used_traffic` — `used_traffic` is cumulative, not per period.
- `lifetime_used_traffic = reseted_usage + used_traffic`.
- `modify_user` recomputes status from `expire`/`data_limit`; `set_disabled(false)`
  resolves via `_resolve_enabled_user_status` and will not revive a stale
  `limited`/`expired` user without valid fields (send `status='active'` after
  writing `expire`/`data_limit` if needed).
- Disabled users are removed from node payloads (`app/node/sync.py`), so
  disable is a real revocation.
- `revoke_sub` rotates vless/trojan/shadowsocks credentials
  (`app/db/crud/user.py::build_revoked_proxy_settings`).
- The subscription endpoint needs a token; the admin config endpoint does not.
  Do not store tokens.
- Subscription format is selected by the first matching rule; the default rule
  is a catch-all `links_base64`. A user-agent that must receive xdrive has to
  be mapped to `xray` **above** the catch-all, or use the explicit
  `/{token}/xray` path (the bot uses the admin API instead).

### Remnawave

- One VLESS UUID per user; HWID is device counting only, not per-device
  credentials.
- `XRAY_JSON` output is an array of full configs, one per host; PasarGuard's
  `xray` output has the same shape, so the merge is array concatenation.
- Host `xrayJsonTemplate` is static JSON; it cannot carry a per-user UUID.
  There is no xdrive transport. Do not try to make Remnawave emit xdrive.
- Default response rules end with an unconditional `Fallback Base64`
  (`prisma/seed/default/response-rules.ts`). The xdrive-capable UA needs an
  `XRAY_JSON` rule above it (or the `/json` path override).
- `revoke_user_subscription` rotates credentials and refreshes
  `subscription_url`; `shortUuid` is not the credential.

### xdrive

- Fork: `e:/Misc/repository/xdrive/Xray-core` (`utls-chrome155`).
- Config needs `streamSettings.network="xdrive"` + `xdriveSettings`; VLESS
  encryption must not be `none`; `mux.enabled=true`. The fork refuses the
  config otherwise.
- The client config contains the storage `secrets` (`xdriveSettings.secrets`)
  and `remoteFolder` — they are shared by all users of the host. A leaked
  config exposes the storage backend; UUID rotation does not revoke them.
  Never log or commit these.
- PasarGuard's Xray subscription template must not use the removed
  `proxySettings` field on `dns-out`; the spike hit this and the fork refused
  the whole config. Use `streamSettings.sockopt.dialerProxy` or remove it.
- Setup guide (Windows-1251): `e:/tmp/xdrive-guide/guide.txt`.
- The test deployment used `service: "s3"`; do not assume Google Drive.

## Where the code is

Bot (workspace root):

- `app/webserver/subscription_proxy.py` — `SubscriptionProxyMiddleware._proxy`,
  `patch_adblock_dns`, `premium_usage`, `short_uuid_candidates`. The merge hook
  belongs after the adblock patch.
- `app/webserver/unified_app.py` — middleware mounting.
- `app/services/premium_traffic_service.py` — `_collect_targets`,
  `_fetch_usage`, `_apply_usage`, `_limit_squad`, `_restore_squad`,
  `_drop_grace_owned`; singleton at the end.
- `app/database/crud/premium_traffic.py` — `get_state`, `record_usage`,
  `start_new_period` (reset the two pasarguard fields here).
- `app/database/models.py` — `Subscription` (~line 2607),
  `SubscriptionPremiumTraffic` (~line 3043), `UserStatus`.
- `app/services/subscription_service.py` — `create_remnawave_user`,
  `revoke_subscription`.
- `app/services/subscription_deletion_service.py` —
  `delete_subscription_record` (hard delete; enqueue the intent before it).
- `app/config.py` — settings; `.env.example` — documentation.
- `main.py` — premium worker startup/shutdown; add the projector there.
- Migration head is `0136`; the plan adds `0137`.

Reference repos:

- PasarGuard panel: `e:/Misc/repository/PasarGuard/panel`
  (`app/routers/user.py`, `app/db/models.py`, `app/db/crud/user.py`,
  `app/operation/user.py`, `app/jobs/review_users.py`,
  `app/subscription/xray.py`, `app/subscription/share.py`,
  `app/node/user.py`, `app/node/sync.py`).
- Remnawave backend: `e:/Misc/repository/remnawave/backend`
  (`src/modules/subscription-template/generators/xray-json.generator.service.ts`,
  `src/modules/subscription-response-rules/`,
  `prisma/seed/default/response-rules.ts`,
  `src/modules/users/entities/base-users.entity.ts`).
- mirzabot (PasarGuard as Marzban-compatible panel):
  `e:/Misc/repository/mirzabot` (`function.php`, `admin.php`).
- xdrive fork: `e:/Misc/repository/xdrive/Xray-core`
  (`transport/internet/xdrive/`).
- Windows xdrive binary for config tests: `e:/tmp/xray-main/xray.exe`
  (v26.11.9); geodata in `e:/tmp/xray-assets/`.

## Spike artifacts (outside the repo; may contain secrets)

- `e:/tmp/xdrive-spike/tools/spike_check.py` — validates a config and merges
  two arrays; redacts secrets; supports `--xray` and `--asset-dir`.
- `e:/tmp/xdrive-spike/tools/try_connect.py` — starts one entry as a client and
  curls through its SOCKS inbound (not run in the final session).
- `e:/tmp/xdrive-spike/tools/spike_proxy_full.patch` and
  `spike_proxy_force_json.patch` — the removed throwaway proxy spike; reference
  only, do not apply as production code.
- `e:/tmp/xdrive-spike/data/pg2.json` — real test config with secrets. Delete
  after use; never commit.

## Test patterns to follow

- API client: `tests/external/test_remnawave_3_0_0.py` (fake session / mocked
  transport, exact routes).
- Proxy helper: `tests/webserver/test_adblock_dns_patch.py` (pure function,
  object/array/gzip/non-Xray cases).
- Premium worker: `tests/services/test_premium_traffic_service.py`
  (`SimpleNamespace`, `AsyncMock`, `tests/fixtures/sqlite_memory.py`).
- DB: `memory_session` from `tests/fixtures/sqlite_memory.py`.

## Project rules

- No commits without an explicit user request. The plan's commit steps are
  conditional; ask first.
- Never add `Co-Authored-By` / AI attribution trailers.
- Append a changelog entry to `../claude-changes.md` (outside the repo) after a
  code change; never put secrets there.
- Edit only `ru.json` for locales.
- Repository stores LF; force LF when writing files from Python on Windows.
- No PR references in commit messages.
- Do not log or commit PasarGuard keys, subscription tokens, or xdrive
  `secrets`/`remoteFolder`/`s3` settings.

## Next actions

1. Read the design and plan end to end.
2. Confirm with the user: (a) the trial tariff has a positive premium-squad
   limit; (b) a tariff with more than one premium squad should get no xdrive;
   (c) a global `XDRIVE_ENABLED` flag is enough for v1.
3. Execute the plan tasks 1–10 with TDD. Each task is self-contained; the
   Interfaces blocks carry the exact names and signatures.
4. After task 10, run the lifecycle matrix tests and the regression suite.
5. Do not reintroduce the spike; do not modify Remnawave or PasarGuard source.
