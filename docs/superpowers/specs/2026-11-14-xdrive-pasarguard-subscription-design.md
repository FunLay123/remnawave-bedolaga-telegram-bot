# Xdrive through PasarGuard inside the bot subscription — design

**Date:** 2026-11-14
**Status:** proposed design; implementation is not authorized yet
**Audience:** a builder who did not participate in the brainstorm or the spike

## Intent

Give a user one subscription URL while two panels cooperate behind it. Remnawave
remains the source of the main profile: servers, general traffic limit, HWID
device limit, premium squads. PasarGuard supplies only **xdrive** — the
Drive/S3-backed transport that Remnawave nodes cannot run. The bot already
proxies the subscription domain and rewrites parts of the response, so it is the
natural place to merge the two Xray JSON arrays.

One bot `Subscription` maps to one Remnawave panel user (existing) and one
PasarGuard panel user (new). The bot database is the source of truth for the
subscription lifecycle; PasarGuard is a projection that is reconciled by a
single writer.

## Goals

- One subscription URL. An xdrive-capable client sees Remnawave servers plus the
  xdrive profile in the same Xray JSON subscription.
- xdrive access follows the same lifecycle as the subscription: purchase, trial,
  renewal, expiry, general traffic limit, ban, grace, tariff switch, deletion.
- xdrive traffic is counted in the **same premium pool** as the single
  Remnawave premium squad. When that pool is exhausted, both the premium squad
  and xdrive stop. A top-up or a new period restores both without re-importing
  the client config.
- A leaked/revoked subscription can have its xdrive credential rotated
  (`revoke_sub`). Expiry and quota exhaustion only **disable** the PasarGuard
  user, so a later renewal restores the same UUID and the imported config keeps
  working.
- `XDRIVE_ENABLED=false` is a safe kill switch: no new PasarGuard users, no
  merge, and every known PasarGuard user is actively disabled. It is not a
  no-op that leaves working credentials behind.

## Non-goals

- No per-client (per-device) PasarGuard UUIDs. One PasarGuard user per
  subscription, one VLESS UUID for all of its devices.
- No Remnawave source changes and no PasarGuard source changes. PasarGuard is
  used through its HTTP API only.
- No PasarGuard → bot synchronization. Manual edits in the PasarGuard panel are
  overwritten by the projector; PasarGuard is a slave.
- No xdrive for clients that do not already receive Xray JSON. Base64, Clash,
  Mihomo, sing-box, Stash and the HTML subscription page are passed through
  unchanged.
- No merge format conversion. The bot does not force JSON on clients whose
  user-agent maps to base64. Making an xdrive-capable client receive
  `XRAY_JSON` is an operator task (a Remnawave subscription-response rule or a
  `/json` path override).
- No new public endpoints and no separate xdrive subscription URL.

## Anti-goals (a technically working implementation that still fails)

- Merging xdrive into a client that cannot run it and breaking that client's
  whole subscription.
- Leaving PasarGuard users enabled after the feature flag is turned off,
  after expiry, or after the premium pool is exhausted.
- Making subscription delivery depend on PasarGuard availability.
- Requiring every subscription-mutating code path to remember to call
  PasarGuard. The projector, not the handlers, must be the guarantee.
- Counting xdrive traffic outside the premium pool, so the sold premium GB can
  be bypassed through xdrive.
- Logging or committing xdrive settings: they contain shared storage
  credentials.

## Decisions already made with the product owner

1. **Merge point:** the bot subscription proxy. No panel changes.
2. **Merge condition:** the upstream response is already structurally Xray JSON.
   This is the product owner's explicit decision.
3. **Identity:** one PasarGuard user per bot subscription, not per device.
4. **Premium accounting:** the subscription has exactly one Remnawave premium
   squad in this deployment. xdrive usage is added to that squad's usage; the
   existing per-squad limit, removal, restoration, top-up, header and
   notification logic stays in charge.
5. **Master switch:** an env flag enables/disables the whole feature. Off means
   drain-to-disabled, as described under “Feature flag semantics”.
6. **Direction:** bot → PasarGuard only.

## Spike evidence (2026-11-14)

A throwaway spike validated the two risky assumptions. It was removed from the
repository and must not be resurrected as production code.

- PasarGuard generates a valid xdrive entry for a user whose only group is the
  xdrive group: a JSON array with `remarks="xdrive"`, a VLESS outbound whose
  `streamSettings.network` is `"xdrive"`, non-empty VLESS encryption, `mux`
  enabled and `xdriveSettings` (the `s3` service in that test). The
  xdrive-capable Xray fork accepted the entry with `xray run -test`.
- Concatenating the Remnawave Xray JSON array and the PasarGuard array was
  accepted by the real xdrive-capable client and both Remnawave and xdrive
  profiles were usable.
- Both panels ship a catch-all subscription rule that returns base64
  (`Fallback Base64` in Remnawave, `^.* → links_base64` in PasarGuard). An
  xdrive-capable client must be mapped to `XRAY_JSON` explicitly; otherwise the
  merge condition is false and no xdrive is appended.

## Architecture

```
client
  │  GET https://<sub-domain>/<shortUuid>
  ▼
nginx (subscription domain)
  ▼
bot web server
  └─ SubscriptionProxyMiddleware
       ├─ upstream fetch ──────────────► Remnawave subscription page/backend
       │                                   returns Xray JSON / base64 / HTML
       ├─ entitlement + identity lookup ─► bot DB
       ├─ xdrive config fetch ──────────► PasarGuard API
       │   GET /api/user/{id}/subscription/xray
       └─ response: Remnawave body + PasarGuard entries (only if Xray JSON)

bot background workers
  ├─ PremiumTrafficService ── reads squad usage + PasarGuard lifetime usage,
  │                            updates subscription_premium_traffic
  └─ PasarguardSyncService ── the only writer to PasarGuard:
                               create/update/disable/enable/revoke/delete
```

The PasarGuard panel itself is never exposed to clients. Its subscription
tokens are not stored or forwarded; the bot fetches the config through the
authenticated admin API by PasarGuard user id.

## Components

### 1. PasarGuard API client — `app/external/pasarguard_api.py`

A thin HTTP client, modelled on `RemnaWaveAPI`: one method per endpoint, no
business logic, structured logging, no secrets in logs.

- Auth: `X-Api-Key: <PASARGUARD_API_KEY>`.
- Base URL: `PASARGUARD_API_URL` (the panel root, e.g. `https://panel.example`).
- Methods:
  - `get_user_by_id(user_id)`, `get_user_by_username(username)`
  - `list_users(offset, limit)` — paginated bulk read for usage accounting
  - `create_user(username, group_ids, expire, data_limit, status, note)`
  - `update_user(user_id, expire=..., data_limit=..., group_ids=..., note=...)`
  - `set_disabled(user_id, disabled: bool)`
  - `revoke_sub(user_id)` — rotates the VLESS UUID and invalidates the public
    subscription token
  - `delete_user(user_id)`
  - `get_subscription_config(user_id, "xray")` — returns the bytes of
    `GET /api/user/{id}/subscription/xray`
- Error types: not-found, auth/permission, validation, transient (timeout,
  5xx). Only idempotent GETs may retry blindly. Writes are retried only after a
  read-back confirms the previous attempt did not apply.
- Timeouts and a modest request rate limit; the premium worker uses the bulk
  list endpoint, not one request per subscriber.

### 2. Identity in the bot database

New nullable columns on `subscriptions` (one Alembic migration):

- `pasarguard_user_id` — integer, PasarGuard user id.
- `pasarguard_username` — string,
  `<PASARGUARD_USERNAME_PREFIX><subscription.id>` (default prefix `bx`; the
  prefix makes every name at least 3 characters and marks bot-owned users).
- `pasarguard_created_at` — when the current PasarGuard user was created; a
  recreated identity resets the usage baseline.
- `pasarguard_last_revoke_at` — last successful or attempted `revoke_sub`.
- `pasarguard_synced_at` — last successful reconcile. `pasarguard_synced_hash`
  is kept only as an audit/fast path; it is not the gate for writes.

Partial unique index on `pasarguard_user_id` where not null, mirroring the
existing `remnawave_id` index.

A small `pasarguard_delete_intents` table stores `(subscription_id,
pasarguard_user_id, pasarguard_username, created_at)` for deletions that could
not reach PasarGuard before the subscription row was hard-deleted. The
projector drains it every pass; orphan cleanup is the hourly safety net.

Premium accounting uses the existing `subscription_premium_traffic` row of the
single premium squad. Add two nullable columns:

- `pasarguard_baseline_bytes` — PasarGuard `lifetime_used_traffic` captured at
  the start of the current premium period (or at first observation). It is
  reset together with `baseline_bytes` and `notified_*` when the premium period
  rolls over, and when the PasarGuard identity is recreated.
- `pasarguard_identity_created_at` — the value of
  `subscriptions.pasarguard_created_at` that the baseline belongs to. When it
  differs from the current subscription value, the identity was recreated:
  reset the baseline to the current lifetime and use delta 0 once. Without
  this marker a mid-period recreation would reset the baseline on every later
  pass and the delta would stay zero forever.

No separate usage table is needed while the deployment has one premium squad.
If a future tariff has multiple premium squads, the pool rule must be revisited
(open item).

### 3. Projector — `app/services/pasarguard_sync_service.py`

The only writer to PasarGuard. It computes a desired state from the bot
database and applies it idempotently.

**Desired state**

| Condition | Desired |
|---|---|
| No subscription row for a known PasarGuard identity | delete the PasarGuard user (and drain the delete-intent row) |
| `XDRIVE_ENABLED=false` | disabled for every known identity; no creation, no merge |
| Subscription not effectively active (expired, limited, disabled, pending); user banned/blocked | disabled |
| Tariff has no positive premium squad | disabled (v1 rule: xdrive exists only next to the premium pool) |
| Premium state row missing (the worker has not measured yet) | disabled; do not create/enable until the row exists |
| Premium state for the single squad is `is_limited`, `is_exhausted`, `closed_at` set, the squad is not in the connected set, or the tariff has more than one positive premium squad | disabled |
| Otherwise (effective active/trial, no open grace overlay), identity exists or can be created | enabled, `expire` = effective end, `data_limit = used_traffic_now + remaining_pool_bytes`, `group_ids` = [xdrive group] |

v1 grace rule: grace is not accounted. While a grace overlay is open the
subscription is treated as not eligible and xdrive is disabled; no grace-aware
measurement is added. Trial eligibility follows the same premium-squad rule:
if the trial tariff carries a positive premium-squad limit (same hosts, lower
limit — the product owner’s description), trial users get xdrive under that
pool; if it carries no positive limit, they get no xdrive.

`remaining pool bytes = max(0, state.total_limit_bytes - state.used_bytes)`.
The PasarGuard `data_limit` counts only xdrive traffic for that user, so it is
the correct hard ceiling on the remaining pool.

**Application rules**

- Username is `<PASARGUARD_USERNAME_PREFIX><subscription.id>` (default `bx`).
  The prefix makes every name at least 3 characters (PasarGuard requires 3–128)
  and marks bot-owned users. The `note` carries
  `xdrive-bot:<PASARGUARD_INSTANCE_ID>:sub:<id>`. Orphan cleanup deletes only
  users that match the prefix and the note pattern; it never deletes an
  unmarked user, so a shared panel is safe.
- Creation is idempotent: GET by username; if 404, create; if a concurrent
  create wins, re-read. Never create when the flag is off or the subscription
  is not eligible.
- The PasarGuard user is created with a PasarGuard-generated VLESS UUID. The
  bot does not need to match Remnawave's UUID; the two credentials live in
  different config entries.
- `group_ids` contains **only** the xdrive group. This guarantees that the
  PasarGuard subscription contains only xdrive hosts.
- Every pass reads the panel's users in bulk (`GET /api/users`, which returns
  status, expire, data_limit, group_ids, used_traffic and
  lifetime_used_traffic) and compares the actual fields with the desired ones.
  `pasarguard_synced_hash` is not a write gate: a manual panel edit or a manual
  deletion would otherwise persist forever and contradict the slave model.
  Write only when an actual field differs; compare `data_limit` with a small
  tolerance (for example 1 MiB or 1%) because the two counters are sampled at
  slightly different times.
- `data_limit` is an **absolute** lifetime figure, not a period figure:
  `data_limit = used_traffic_now + remaining_pool_bytes`, where
  `used_traffic_now` is the PasarGuard user's cumulative `used_traffic` from
  the bulk list and `remaining_pool_bytes = max(0, total_limit_bytes -
  used_bytes)` from the premium state. PasarGuard compares `data_limit` to the
  cumulative `used_traffic`, so "remaining only" would mark an active user
  limited immediately. No periodic usage reset is required. When the desired
  value would be zero, disable instead of writing zero (zero means unlimited).
  Create/update with `data_limit_reset_strategy='no_reset'`: the absolute cap
  assumes `used_traffic` never resets behind the bot's back.
- Enabling sequence: write `expire`, the absolute `data_limit`, `group_ids` and
  `note` first; then clear `disabled`. If the panel still reports `limited` or
  `expired` because its fields were stale, send `status='active'` in the update
  after the new `expire`/`data_limit` are in place. `set_disabled(false)`
  resolves the status from `expire`/`data_limit` and will not revive a stale
  limited/expired user on its own.
- Do not create or enable a PasarGuard user until the premium state row exists.
  The purchase/trial path nudges the premium worker to create it immediately;
  if it is still missing, leave xdrive off and retry on the next pass. Enabling
  with an unknown pool would either set zero (unlimited) or an arbitrary cap.
- `expire` and `data_limit` are defence in depth: if the bot is down,
  PasarGuard's own review job flips the user to expired/limited and removes it
  from nodes.
- Disable is used for expiry, limit exhaustion and feature-off. Revoke is used
  only for an explicit subscription revoke/leak. Delete is used only for an
  explicit subscription deletion or orphan cleanup.
- Revoke is synchronous on the bot's revoke path: call `revoke_sub`, invalidate
  the merge cache key, then return the new Remnawave URL. If PasarGuard is
  unavailable, record a durable revoke intent and let the projector retry;
  never block the Remnawave revoke. A duplicate rotation after a crash is
  harmless — the old UUID is invalid either way.
- Deletion paths capture `pasarguard_user_id`/username before the subscription
  row is hard-deleted and enqueue a delete intent. The projector drains intents
  every pass; orphan cleanup remains the hourly safety net.
- Single-flight: one reconcile pass at a time. Errors are logged and retried on
  the next pass; they never propagate into subscription delivery.

**Passes**

- Periodic pass every `PASARGUARD_SYNC_INTERVAL_SECONDS` (default 300, minimum
  60), plus on-demand nudges from key events.
- Backfill on startup/first enable: iterate eligible subscriptions without an
  identity, in batches, rate-limited, resumable.
- Orphan cleanup: list PasarGuard users that match
  `<PASARGUARD_USERNAME_PREFIX><id>` and the bot's note marker, parse the
  subscription id, compare with the database; delete users whose subscription
  is gone. Run at most hourly. Users without the marker are never touched.

**Nudges (latency, not correctness)**

Add a cheap `nudge(subscription_id)` call after: subscription create, trial
activation, purchase/renewal, expiry/limit transition, ban/unban, premium
limit/restore/top-up, revoke, deletion. Nudges are best-effort and coalesced;
the periodic pass is the guarantee, so a forgotten handler only delays the
change.

The premium worker must not write to PasarGuard directly. It updates the
premium state and nudges the projector.

### 4. Premium pool integration — `app/services/premium_traffic_service.py`

The existing worker keeps its per-squad period, limit, top-up, notification and
panel-removal logic. The only change is the usage number.

- Once per pass, fetch all PasarGuard users in pages via `list_users` and build
  `{username: (used_traffic, lifetime_used_traffic)}`. The list endpoint
  already loads both fields (the operation calls the CRUD with
  `load_lifetime_used_traffic=True`), so no per-user requests are needed. Map
  `<PASARGUARD_USERNAME_PREFIX><subscription.id>` to the subscription id.
- The xdrive delta for a subscription is
  `max(0, lifetime_now - state.pasarguard_baseline_bytes)`. If the baseline is
  null, or `state.pasarguard_identity_created_at` differs from
  `subscriptions.pasarguard_created_at`, or `lifetime_now` is below the
  baseline (a panel-side reset), set the baseline to `lifetime_now`, store the
  current identity timestamp and use delta 0 once. The marker prevents a
  recreation from resetting the baseline on every later pass.
- Apply the delta **after** `_net_usage()`: the panel figures are day-granular
  and the existing baseline correction applies only to them; the PasarGuard
  lifetimes are exact and must not be corrected twice.
  Concretely: `record_usage(state, self._net_usage(...) + xdrive_delta, ...)`.
- `state.used_bytes` therefore includes squad usage and xdrive usage. The
  existing decision (`used_bytes >= total_limit_bytes`), the app header
  (`subscription-userinfo`), warnings and exhaustion notifications keep working
  without new branches.
- v1 guard: if the tariff has more than one positive premium squad, do not
  enable or measure xdrive for that subscription and log an error. The delta is
  applied once per premium-squad target; without the guard it would be added to
  every squad's state row.
- Grace: v1 does not account for it. `_drop_grace_owned` skips grace-owned
  subscriptions entirely (no measurement, no state writes), and the projector
  treats an open grace overlay as not eligible and disables xdrive. The grace
  traffic cap therefore cannot be bypassed through xdrive, and a stale
  `data_limit` cannot mis-count. No grace-aware measurement path in v1.
- When the squad is removed, the projector disables the PasarGuard user. On
  restore/top-up/new period, the projector re-enables it. The UUID does not
  change.
- A PasarGuard outage must not decrease `used_bytes`: keep the last measured
  value for that pass, log, and retry. The existing monotonic guard on
  `record_usage` already protects the stored value.
- When `XDRIVE_ENABLED=false`, the worker does not add xdrive usage. The PG
  users are disabled and generate no traffic, so the lifetime delta is flat.

### 5. Proxy merge — `app/webserver/subscription_proxy.py`

The middleware already intercepts the subscription host, forwards to the
upstream, decompresses when it needs to parse JSON, and rewrites
`subscription-userinfo`. Extend it after the upstream response:

1. Resolve the subscription by the `shortUuid` in the path (the existing
   `short_uuid_candidates` helper).
2. Skip unless all are true: `XDRIVE_ENABLED`; the subscription has a
   PasarGuard identity; the effective subscription state allows access; the
   premium state is not limited/exhausted/closed.
3. Forward the request as today. When a merge may happen, request
   `accept-encoding: identity` (as the adblock path already does) so the body
   can be parsed; also drop `if-none-match` / `if-modified-since` so a `304`
   without a body cannot hide the merge.
4. Read the body. Decompress gzip if present.
5. **Merge condition:** the body parses as a JSON object or array and at least
   one entry has an `outbounds` list containing a dict with a `protocol` key.
   That is the structural signature of an Xray config. sing-box configs use
   `type` and are passed through. Base64, text/plain, HTML and unparseable
   bodies are passed through unchanged.
6. Fetch the PasarGuard Xray config through the admin API
   (`GET /api/user/{id}/subscription/xray`), with a short TTL cache
   (`XDRIVE_CONFIG_CACHE_SECONDS`, default 60). The cache key includes the
   PasarGuard user id and `pasarguard_last_revoke_at` / identity generation, so
   a synchronous revoke invalidates it immediately; the TTL is the fallback.
   The client's `X-HWID` headers are not needed: the admin fetch does not
   perform PasarGuard's HWID registration, and device counting remains a
   Remnawave concern.
7. Append the PasarGuard entries to the array. Return a JSON array even if the
   upstream returned a single object.
8. Headers: drop `content-encoding` if the body was decompressed, drop `etag`
   and `last-modified` because the body no longer matches them, recompute
   `content-length`, keep the upstream `subscription-userinfo`, content-type
   and client-refresh headers.
9. On any PasarGuard error, timeout, malformed body or missing identity, return
   the Remnawave body untouched. The merge is an addition, never a failure
   point.
10. `HEAD` requests, browser HTML requests and non-GET methods pass through
    unchanged.

**Adblock order.** The existing adblock patch runs first, over the Remnawave
array only. The PasarGuard entries are appended afterwards and are not
rewritten: adblock DNS/routing and `targetStrategy` were not validated with the
xdrive fork. If xdrive should get adblock later, that is a separate change.

**Compatibility caveat.** Xray JSON capability is not the same as xdrive
capability. A stock Xray client may reject an unknown `xdrive` transport. The
product owner chose “merge whenever the client already receives Xray JSON”; the
design keeps that rule. Operators who serve both stock and xdrive clients should
add the optional `XDRIVE_CLIENT_UA_REGEX` guard: when set, the merge happens
only for a matching user-agent, while other JSON clients receive the untouched
Remnawave body. Default is empty (no extra restriction).

**Deployment prerequisite.** The xdrive-capable client's user-agent must map to
`XRAY_JSON` in Remnawave, above the system `Fallback Base64` rule (or the
subscription URL must use the `/json` path override). This is an operator
configuration step; the bot does not force the format.

### 6. Configuration

New env settings, all read once at startup unless stated otherwise:

- `XDRIVE_ENABLED` (default `false`) — master user-facing switch.
- `PASARGUARD_API_URL` — panel root.
- `PASARGUARD_API_KEY` — API key with user read/create/update/delete/revoke
  permissions for the bot.
- `PASARGUARD_XDRIVE_GROUP_ID` — the PasarGuard group whose only inbound is
  xdrive.
- `PASARGUARD_USERNAME_PREFIX` (default `bx`) — bot-owned username prefix.
- `PASARGUARD_INSTANCE_ID` (default the bot instance/host name) — the ownership
  marker inside the user note.
- `PASARGUARD_SYNC_INTERVAL_SECONDS` (default 300, minimum 60).
- `PASARGUARD_API_TIMEOUT_SECONDS` (default 15).
- `PASARGUARD_SYNC_BATCH_SIZE` (default 200).
- `XDRIVE_CONFIG_CACHE_SECONDS` (default 60).
- `XDRIVE_CLIENT_UA_REGEX` (optional, default empty).

If `PASARGUARD_API_URL` or `PASARGUARD_API_KEY` is missing, the projector does
not start and the merge is a no-op. The feature is then fully off, which is the
intended hard stop after a drain pass.

### 7. Feature flag semantics

- **On:** normal desired state, merge allowed, xdrive usage counted.
- **Off:** no creation, no merge, no xdrive usage added; the projector’s desired
  state is `disabled` for every known identity. The first pass after flipping
  the flag cuts xdrive at the nodes. PasarGuard users are not deleted, so
  turning the flag back on restores the same UUIDs.
- **Hard stop:** after one drain pass with the flag off, clear
  `PASARGUARD_API_*`. The projector cannot run and no PasarGuard calls are made.
  This is documented as the decommissioning path; clearing credentials before
  the drain leaves users enabled and is an operator error.

### 8. Lifecycle matrix

| Event | Bot DB | PasarGuard result |
|---|---|---|
| Trial activation | subscription + premium state created | normal rules if the trial tariff has a positive premium-squad limit; otherwise no xdrive |
| Purchase | subscription + premium state created | projector creates and enables the user; expire = end |
| Renewal / autopay / gift / promo / tariff switch | status/end/tariff updated | projector updates expire/data_limit; UUID unchanged |
| Expiry | effective status changes | disabled; `expire` already in the past is a backstop |
| General traffic limit (Remnawave LIMITED) | subscription limited | disabled |
| Ban / unban | user blocked/unblocked | disabled / enabled |
| Premium top-up | state total grows, `is_limited` cleared | enabled if eligible; `data_limit` updated |
| Premium period reset | baseline/notified reset | `pasarguard_baseline_bytes` reset; enabled if eligible |
| Premium state row missing | no state | xdrive not enabled; the purchase/trial path nudges the worker |
| Premium squad closed/opened by admin (`closed_at`) | state flag | disabled / enabled |
| Grace access open/close | effective end extended | v1: not accounted; xdrive disabled while the overlay is open; expire is not extended for xdrive |
| Device limit change | subscription device_limit | no PasarGuard write; Remnawave enforces at fetch |
| Traffic reset / tariff switch | limits reset | expire/data_limit updated; baseline follows the period |
| Multi-tariff | one subscription per tariff | one PasarGuard user per subscription |
| Account merge | subscriptions moved/deleted | identity follows the subscription id; orphans deleted |
| Subscription revoke | `last_revoke_at` | synchronous `revoke_sub` + cache invalidation; retry intent on failure |
| Subscription deletion | row removed | synchronous best-effort delete; durable delete intent; orphan cleanup safety net |
| Remnawave webhook / panel-is-truth import | bot DB updated | projector applies the new desired state |
| Feature flag off | no DB change | all known identities disabled |

### 9. Security and secrets

- xdrive client configs contain the storage credentials (`xdriveSettings`
  secrets) shared by every user of the host. A leaked config exposes the
  storage backend, not just one UUID. Rotating the PasarGuard UUID does not
  revoke those credentials; rotating the host configuration does. This is
  accepted for v1 and must be documented for the operator.
- Never log request/response bodies of PasarGuard subscription configs, the
  API key, or xdrive settings. Log ids, usernames, remarks and counts only.
- Do not store PasarGuard subscription tokens or URLs. The admin API by user id
  is enough and survives `revoke_sub`.
- Do not commit `pg.json` fixtures with real secrets. Test fixtures use fake
  values.
- The `X-Api-Key` is env-only; it is not exposed to the cabinet or the client.

### 10. Failure modes

- **PasarGuard down:** the merge returns the Remnawave body; the projector
  retries; subscriptions keep working without xdrive.
- **Bot down:** nginx serves the Remnawave subscription page as before; the
  PasarGuard `expire`/`data_limit` fields enforce the last known state.
- **PasarGuard user missing:** the projector recreates it on the next eligible
  pass. The xdrive UUID changes, so the client must refresh the subscription;
  Remnawave profiles are unaffected.
- **Premium worker down:** the pool usage lags; `data_limit`/`expire` remain the
  hard backstop.
- **Premium state row missing:** treat the subscription as not eligible and do
  not merge; log once per pass.
- **Clock/timezone:** all comparisons are UTC; panel timestamps are normalized
  with the existing helpers.
- **Race between create and reconcile:** unique index on
  `(pasarguard_user_id)` and GET-by-username make creation idempotent.

## What is left to the builder

- Choose the precise effective-access predicate to mirror the existing
  grace/temporary-access logic; reuse the bot’s existing helper rather than
  re-deriving status from `Subscription.status`.
- Decide how the projector reads the “premium pool exhausted” state without
  duplicating the premium worker’s period logic (read the state row; do not
  recompute the period independently).
- Choose the `data_limit` comparison tolerance for drift detection and the
  write cadence that follows from it.
- Choose the retry/backoff policy for PasarGuard writes and the nudge coalescing
  mechanism (the existing event emitter is a candidate).
- Choose the durable intent storage for revoke/delete (a small table is
  proposed) and how the projector drains it.
- Choose whether `XDRIVE_CLIENT_UA_REGEX` is set in production; the product
  owner’s rule is JSON-only, the regex is a safety valve.

## Open questions for the product owner

1. **Multiple premium squads.** v1 disables xdrive for a tariff with more than
   one positive premium squad. Confirm this is acceptable if such a tariff
   appears; a future version can designate one squad or pool the sum.
2. **Per-tariff switch.** v1 uses the global `XDRIVE_ENABLED` env flag only.
   Confirm that per-tariff control is not needed for the first release.

## Assumption to verify during implementation

The trial tariff has a positive `server_traffic_limits` entry for the premium
squad. The product owner describes trials as “the same hosts with a lower
limit”; the same hosts and a lower general traffic limit are not enough — the
premium pool is the separate per-squad field. If the trial tariff has no
positive entry, trial users get no xdrive under the v1 rule.

## Test plan

- Unit: desired-state matrix (every lifecycle row), identity idempotency,
  merge detection (Xray array, single object, sing-box, base64, HTML, gzip,
  HEAD), entitlement skips, header rewriting, the absolute `data_limit`
  formula, the enable sequence for a stale limited/expired user, drift
  detection for manual panel edits and manual deletion, and the multi-squad
  guard.
- Contract: fake PasarGuard API covering 200/401/404/409/5xx, pagination,
  revoke, delete, status recalculation; assert no secrets in logs.
- Premium math: squad usage + xdrive delta; period rollover; top-up; identity
  recreation (the baseline resets exactly once); a panel reset below the
  baseline; PasarGuard outage does not decrease usage; header value;
  state-row gating.
- Revoke/delete: synchronous revoke + cache invalidation; retry intent when the
  panel is down; a delete intent drains after the subscription row is gone.
- Adblock: with adblock enabled, the Remnawave entries are patched and the
  PasarGuard entry is byte-identical to what the panel returned.
- Regression: with `XDRIVE_ENABLED=false` the proxy output is byte-identical to
  today (apart from the existing premium/adblock rewrites), and no PasarGuard
  calls are made when credentials are absent.
- Live smoke (optional, gated): create a test subscription, fetch the merged
  JSON, validate one xdrive entry with the xdrive fork, confirm the client
  connects; disable, confirm the node drops the user; top up, confirm the same
  UUID works again.

## Rollout

1. Migration + PasarGuard client + identity, flag off.
2. Projector + backfill + orphan cleanup, flag off; verify dry-run logs and that
   no PasarGuard writes happen.
3. Merge behind the flag for one test subscription / UA regex.
4. Premium pool accounting.
5. Lifecycle audit: walk every row of the matrix in a test environment.
6. Canary group, then enable globally.
