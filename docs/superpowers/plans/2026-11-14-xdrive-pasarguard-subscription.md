# Xdrive through PasarGuard — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve one subscription URL that merges the Remnawave Xray JSON profile with an xdrive-only profile from PasarGuard, and keep the PasarGuard user's lifecycle and premium-pool accounting in lockstep with the bot database.

**Architecture:** The bot database is the source of truth. A single projector (`PasarguardSyncService`) reconciles PasarGuard users; a subscription-proxy hook appends PasarGuard's Xray JSON to Remnawave's Xray JSON only when the client already receives Xray JSON; the premium worker adds xdrive's lifetime delta to the single premium squad's usage. No Remnawave or PasarGuard source changes.

**Tech Stack:** Python 3.12, Starlette middleware, async SQLAlchemy, Alembic, httpx, pytest/pytest-asyncio, in-memory SQLite test fixtures.

**Spec:** `docs/superpowers/specs/2026-11-14-xdrive-pasarguard-subscription-design.md`

## Global Constraints

- Merge only when the upstream body is structurally Xray JSON. Never force a format. Base64, Clash/Mihomo, sing-box, Stash, HTML, `HEAD` and non-GET requests pass through unchanged.
- One PasarGuard user per bot `Subscription`; no per-device UUIDs.
- PasarGuard is a slave. The bot database is the only source of truth; manual PasarGuard edits are overwritten by the projector.
- `XDRIVE_ENABLED=false` means drain-to-disabled: no creation, no merge, every known PasarGuard user disabled. A no-op is forbidden.
- Exactly one positive premium squad. More than one disables xdrive for that subscription.
- Grace is not accounted in v1: an open grace overlay makes the subscription ineligible; xdrive is disabled.
- Trial follows the same premium-squad rule; the trial tariff must have a positive `server_traffic_limits` entry.
- `data_limit` is absolute: `used_traffic_now + remaining_pool_bytes`; create/update with `data_limit_reset_strategy='no_reset'`.
- No xdrive before a `SubscriptionPremiumTraffic` row exists.
- Never log or commit PasarGuard API keys or xdrive settings (`secrets`, `remoteFolder`, `s3`). Never store PasarGuard subscription tokens.
- Usernames: `<PASARGUARD_USERNAME_PREFIX><subscription.id>`; note: `xdrive-bot:<PASARGUARD_INSTANCE_ID>:sub:<id>`.

## Review Focus

These are the failure modes most likely to hurt a user or the business; each has a test in the task that owns the code.

1. **Non-Xray bodies must pass through unchanged.** A base64 or sing-box client must not receive a broken JSON array because the merge guessed the format.
2. **`data_limit` is cumulative.** PasarGuard compares it with cumulative `used_traffic`, not period usage; a "remaining only" value silently limits active users.
3. **Drift and manual deletion.** The projector must read the panel every pass; trusting the stored hash leaves manual enables, group edits, or deleted users in place forever.
4. **Revoke/delete latency.** After the user-visible revoke or subscription deletion, the old PasarGuard UUID must not remain valid; intents must cover a PasarGuard outage.
5. **Eligibility gates.** No xdrive without a premium state row, during grace, or for a tariff with more than one premium squad.

---

### Task 1: PasarGuard API client

**Files:**
- Create: `app/external/pasarguard_api.py`
- Test: `tests/external/test_pasarguard_api.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) PasarguardUser(id: int, username: str, status: str, expire: datetime | None, data_limit: int | None, used_traffic: int, lifetime_used_traffic: int, group_ids: list[int], note: str | None)`
  - `PasarguardAPIError(Exception)`, `PasarguardTransientError(PasarguardAPIError)`, `PasarguardNotFoundError(PasarguardAPIError)`
  - `PasarguardAPI(base_url: str, api_key: str, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None)` with `close()`, `get_user_by_id`, `get_user_by_username`, `list_users(offset=0, limit=200) -> list[PasarguardUser]`, `create_user(...)`, `update_user(user_id, **fields)`, `set_disabled(user_id, disabled)`, `revoke_sub(user_id)`, `delete_user(user_id)`, `get_subscription_config(user_id, client_type='xray') -> bytes`.

- [ ] **Step 1: Write the failing tests**

Cover the exact routes and the auth header with `httpx.MockTransport`:

```python
def test_create_user_posts_to_api_user():
    # request.method == "POST"; request.url.path == "/api/user"
    # request.headers["x-api-key"] == "secret"
    # body includes username, group_ids, expire, data_limit,
    #   data_limit_reset_strategy="no_reset", note

def test_get_user_by_id_uses_by_id_path():
    # GET /api/user/by-id/42

def test_disable_uses_by_id_disabled_and_toggle_body():
    # PUT /api/user/by-id/42/disabled; body == {"disabled": True}

def test_revoke_uses_by_id_revoke_sub():
    # POST /api/user/by-id/42/revoke_sub

def test_delete_uses_delete_by_id():
    # DELETE /api/user/by-id/42

def test_list_users_paginates_and_parses_lifetime():
    # GET /api/users?offset=0&limit=200; user.lifetime_used_traffic == 10

def test_subscription_config_returns_raw_bytes():
    # GET /api/user/42/subscription/xray; body bytes preserved

async def test_404_raises_not_found():
async def test_5xx_and_timeout_raise_transient():
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/external/test_pasarguard_api.py -v`
Expected: FAIL with `ModuleNotFoundError: app.external.pasarguard_api`.

- [ ] **Step 3: Implement `PasarguardAPI`**

Use a persistent `httpx.AsyncClient` with `base_url`, `X-Api-Key`, and the injected transport for tests. Parse the user payload through `PasarguardUser`. Map 404 to `PasarguardNotFoundError`; 5xx/timeout to `PasarguardTransientError`; other 4xx to `PasarguardAPIError`. Do not log headers or bodies.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/external/test_pasarguard_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/external/pasarguard_api.py tests/external/test_pasarguard_api.py
git commit -m "feat: add PasarGuard API client"
```

---

### Task 2: Configuration

**Files:**
- Modify: `app/config.py`
- Modify: `.env.example`
- Test: `tests/test_config_xdrive.py`

**Interfaces:**
- Produces on `Settings`:
  - `XDRIVE_ENABLED: bool = False`
  - `PASARGUARD_API_URL: str | None = None`
  - `PASARGUARD_API_KEY: str | None = None`
  - `PASARGUARD_XDRIVE_GROUP_ID: int | None = None`
  - `PASARGUARD_USERNAME_PREFIX: str = 'bx'`
  - `PASARGUARD_INSTANCE_ID: str = ''`
  - `PASARGUARD_SYNC_INTERVAL_SECONDS: int = 300`
  - `PASARGUARD_API_TIMEOUT_SECONDS: int = 15`
  - `PASARGUARD_SYNC_BATCH_SIZE: int = 200`
  - `XDRIVE_CONFIG_CACHE_SECONDS: int = 60`
  - `XDRIVE_CLIENT_UA_REGEX: str = ''`
  - `is_pasarguard_configured() -> bool` (URL, key and group present)
  - `is_xdrive_enabled() -> bool` (the user-facing `XDRIVE_ENABLED` flag only;
    the projector uses `is_pasarguard_configured()` so a flag-off drain still
    runs)
  - `get_pasarguard_instance_id() -> str` (`PASARGUARD_INSTANCE_ID` or hostname)
  - `get_xdrive_sync_interval_seconds() -> int` (minimum 60)

- [ ] **Step 1: Write the failing tests**

```python
def test_xdrive_disabled_by_default():
    assert Settings(BOT_TOKEN="1:x").is_xdrive_enabled() is False

def test_pasarguard_configured_requires_url_key_and_group():
    # URL/key/group missing -> False; all three present -> True

def test_sync_interval_floor_is_sixty_seconds():
    assert settings_with(PASARGUARD_SYNC_INTERVAL_SECONDS=5).get_xdrive_sync_interval_seconds() == 60

def test_instance_id_falls_back_to_hostname():
    assert settings_with(PASARGUARD_INSTANCE_ID="").get_pasarguard_instance_id()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config_xdrive.py -v`
Expected: FAIL with `AttributeError` / missing settings.

- [ ] **Step 3: Add the settings and `.env.example` block**

Add a commented `# ===== XDRIVE / PASARGUARD =====` block next to the premium-traffic block. Defaults must keep the feature off.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config_xdrive.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/config.py .env.example tests/test_config_xdrive.py
git commit -m "feat: add xdrive and PasarGuard settings"
```

---

### Task 3: Database columns, intents table and CRUD helpers

**Files:**
- Modify: `app/database/models.py`
- Create: `migrations/alembic/versions/0137_xdrive_pasarguard.py`
- Create: `app/database/crud/pasarguard.py`
- Test: `tests/database/test_xdrive_schema.py`, `tests/crud/test_pasarguard_identity.py`

**Interfaces:**
- `Subscription` gains nullable columns: `pasarguard_user_id` (Integer), `pasarguard_username` (String(128)), `pasarguard_created_at` (AwareDateTime), `pasarguard_last_revoke_at` (AwareDateTime), `pasarguard_revoke_pending_at` (AwareDateTime), `pasarguard_synced_at` (AwareDateTime), `pasarguard_synced_hash` (String(64)). Partial unique index on `pasarguard_user_id` where not null.
- `SubscriptionPremiumTraffic` gains nullable columns: `pasarguard_baseline_bytes` (BigInteger), `pasarguard_identity_created_at` (AwareDateTime).
- New model `PasarguardDeleteIntent` (`pasarguard_delete_intents`): `id`, `subscription_id` (Integer, not null), `pasarguard_user_id` (Integer, nullable), `pasarguard_username` (String(128), nullable), `created_at` (AwareDateTime).
- CRUD module functions:
  - `set_pasarguard_identity(db, subscription, *, user_id: int, username: str, created_at: datetime) -> None`
  - `clear_pasarguard_identity(db, subscription) -> None`
  - `add_delete_intent(db, *, subscription_id: int, user_id: int | None, username: str | None) -> None`
  - `list_delete_intents(db, limit: int = 100) -> list[PasarguardDeleteIntent]`
  - `delete_intent(db, intent) -> None`

- [ ] **Step 1: Write the failing schema/CRUD tests**

```python
def test_subscription_has_pasarguard_columns(memory_engine):
    # inspect(subscriptions).columns contains all seven names

def test_pasarguard_user_id_is_unique_when_set(memory_session):
    # two subscriptions with the same non-null id -> IntegrityError

async def test_set_and_clear_identity(memory_session):
    # set -> fields populated; clear -> all null

async def test_delete_intent_round_trip(memory_session):
    # add -> list -> delete -> empty
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/database/test_xdrive_schema.py tests/crud/test_pasarguard_identity.py -v`
Expected: FAIL with missing columns/model.

- [ ] **Step 3: Add the migration and models**

`down_revision = '0136'`. Mirror the existing migration style; use `batch_alter_table` where the project does. Add the table and the partial index.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/database/test_xdrive_schema.py tests/crud/test_pasarguard_identity.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/database/models.py app/database/crud/pasarguard.py \
  migrations/alembic/versions/0137_xdrive_pasarguard.py \
  tests/database/test_xdrive_schema.py tests/crud/test_pasarguard_identity.py
git commit -m "feat: persist PasarGuard identity and delete intents"
```

---

### Task 4: Desired-state evaluator

**Files:**
- Create: `app/services/pasarguard_sync_service.py` (types and pure evaluator only)
- Test: `tests/services/test_pasarguard_desired_state.py`

**Interfaces:**
- `@dataclass(frozen=True) PremiumStateView(is_limited: bool, is_exhausted: bool, closed_at: datetime | None, total_limit_bytes: int, used_bytes: int)`
- `@dataclass(frozen=True) SyncSubject(subscription_id: int, subscription_status: str, end_date: datetime | None, user_status: str, premium_state: PremiumStateView | None, premium_squad_count: int, grace_open: bool, pasarguard_user: PasarguardUser | None)`
- `@dataclass(frozen=True) DesiredState(exists: bool, enabled: bool, username: str, expire: datetime | None, data_limit: int | None, group_ids: tuple[int, ...], note: str | None, reason: str)`
- `evaluate_desired_state(subject, *, enabled: bool, group_id: int, prefix: str, instance_id: str, now: datetime) -> DesiredState`
- `subscription_allows_xdrive(*, status: str, end_date, user_status: str, grace_open: bool, now: datetime) -> bool` (shared with the proxy lookup)
- `premium_state_allows_xdrive(*, state: PremiumStateView | None, squad_count: int) -> bool` (shared with the proxy lookup)

- [ ] **Step 1: Write the failing tests**

```python
def test_flag_off_disables_existing_user_and_creates_nothing():
    # existing -> exists True, enabled False; absent -> exists False

def test_active_subscription_with_one_allowed_squad_is_enabled():
    # enabled; data_limit == used + (total - used) == total when used 0
    # expire == end_date; group_ids == (7,); username == "bx12"; note contains "sub:12"

def test_trial_with_premium_squad_is_enabled():
def test_trial_without_premium_state_is_disabled():
def test_data_limit_is_absolute_not_remaining():
    # used=10, total=20, state.used=10 -> data_limit == 20, not 10

def test_expired_limited_disabled_pending_and_blocked_are_disabled():
def test_open_grace_is_disabled():
def test_missing_premium_state_is_disabled():
def test_two_premium_squads_are_disabled():
def test_closed_or_exhausted_premium_state_is_disabled():
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/services/test_pasarguard_desired_state.py -v`
Expected: FAIL with missing module/names.

- [ ] **Step 3: Implement the pure evaluator**

No DB or API calls. `enabled` argument is `settings.is_xdrive_enabled()`. `exists` is `pasarguard_user is not None` unless the subject is eligible and a user can be created; for an eligible subject with no user, `exists=True, enabled=True` (creation). For an ineligible subject with no user, `exists=False`. Put the shared predicates in this module so Task 6 imports them.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/services/test_pasarguard_desired_state.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/pasarguard_sync_service.py tests/services/test_pasarguard_desired_state.py
git commit -m "feat: add xdrive desired-state evaluator"
```

---

### Task 5: Projector apply pass

**Files:**
- Modify: `app/services/pasarguard_sync_service.py`
- Test: `tests/services/test_pasarguard_sync_service.py`

**Interfaces:**
- `class PasarguardSyncService` with `is_enabled() -> bool` (returns `settings.is_pasarguard_configured()`; the projector must run when the flag is off to drain existing users), `nudge(subscription_id: int | None = None) -> None`, `async process_once() -> dict[str, int]`, `async start_monitoring()`, `stop()`.
- Module singleton `pasarguard_sync_service = PasarguardSyncService()`.
- `process_once` keys: `checked`, `created`, `updated`, `disabled`, `enabled`, `deleted`, `errors`.
- Apply rules: read all PasarGuard users in bulk; compare status/expire/data_limit/group_ids/note; create missing; update drift; disable ineligible; re-enable a disabled/limited/expired user only after writing `expire` and the absolute `data_limit`; send `status='active'` if the panel still reports limited/expired.

- [ ] **Step 1: Write the failing tests**

Use `memory_session` and a fake API object with the Task 1 surface.

```python
async def test_creates_missing_user_with_group_expire_and_absolute_limit():
async def test_manual_disable_is_overwritten_by_the_next_pass():
async def test_manual_group_and_note_edits_are_overwritten():
async def test_manual_user_deletion_is_recreated():
async def test_equal_state_makes_no_write_calls():
async def test_stale_limited_user_is_reenabled_with_active_status():
async def test_ineligible_subscription_disables_an_existing_user():
async def test_pasarguard_error_is_counted_and_does_not_raise():
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/services/test_pasarguard_sync_service.py -v`
Expected: FAIL with missing class.

- [ ] **Step 3: Implement `process_once`**

Load the subject rows in batches, join `User.status`, premium states, the open-grace set, and the bulk PasarGuard map. Evaluate and apply. Single-flight guard; catch and count errors. No writes when fields match (within 1 MiB or 1% for `data_limit`). On a create `409` / duplicate username, re-read by username and continue. After a successful apply update `pasarguard_synced_at` and `pasarguard_synced_hash`; they are audit only, never a write gate.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/services/test_pasarguard_sync_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/pasarguard_sync_service.py tests/services/test_pasarguard_sync_service.py
git commit -m "feat: reconcile PasarGuard users from the bot database"
```

---

### Task 6: Entitlement lookup, config provider and proxy merge

**Files:**
- Create: `app/services/xdrive_service.py`
- Modify: `app/webserver/subscription_proxy.py`
- Modify: `app/webserver/unified_app.py`
- Test: `tests/services/test_xdrive_service.py`, `tests/webserver/test_xdrive_merge.py`

**Interfaces:**
- `@dataclass(frozen=True) XdriveTarget(subscription_id: int, pasarguard_user_id: int, cache_version: str)`
- `async lookup_xdrive_target(short_uuids: list[str]) -> XdriveTarget | None`
- `class PasarguardConfigProvider(api: PasarguardAPI, cache_seconds: int)` with `async get(target) -> bytes | None` and `async aclose()`
- In `subscription_proxy.py`: `merge_xdrive_configs(body: bytes, configs: list[Any]) -> bytes | None` (pure) and `SubscriptionProxyMiddleware.__init__(..., xdrive_lookup=lookup_xdrive_target, xdrive_config: PasarguardConfigProvider | None = None)`.
- The middleware merges only after the adblock patch, only for GET, only when the body is structurally Xray JSON, and only when `XDRIVE_CLIENT_UA_REGEX` is empty or matches the UA. It drops `etag`/`last-modified`, recomputes `content-length`, and drops `content-encoding` if it decompressed. Any provider/lookup error returns the original body.

- [ ] **Step 1: Write the failing merge and provider tests**

```python
def test_appends_pasarguard_array_to_remnawave_array():
def test_single_remnawave_object_becomes_an_array():
def test_sing_box_body_is_untouched():
    # {"outbounds": [{"type": "vless"}]} -> None
def test_base64_and_html_bodies_are_untouched():
def test_gzip_body_is_decompressed_and_merged():
def test_empty_pasarguard_config_returns_none():
async def test_middleware_returns_original_body_for_base64_upstream():
async def test_middleware_returns_original_body_when_provider_fails():
async def test_provider_caches_by_user_and_revoke_version():
async def test_provider_returns_none_on_api_error():
async def test_lookup_skips_grace_limited_closed_and_missing_state():
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/webserver/test_xdrive_merge.py tests/services/test_xdrive_service.py -v`
Expected: FAIL with missing functions.

- [ ] **Step 3: Implement the provider, lookup and merge**

The lookup opens `AsyncSessionLocal`, resolves the subscription by `remnawave_short_uuid`, and reuses `subscription_allows_xdrive` / `premium_state_allows_xdrive` from Task 4. The provider caches `(user_id, cache_version)` for `XDRIVE_CONFIG_CACHE_SECONDS`. In `unified_app.py`, build the provider only when `settings.is_xdrive_enabled()`, append `provider.aclose` to `shutdown_handlers`, and pass it to the middleware.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/webserver/test_xdrive_merge.py tests/services/test_xdrive_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/xdrive_service.py app/webserver/subscription_proxy.py \
  app/webserver/unified_app.py tests/webserver/test_xdrive_merge.py \
  tests/services/test_xdrive_service.py
git commit -m "feat: merge xdrive configs into Xray JSON subscriptions"
```

---

### Task 7: Premium-pool accounting

**Files:**
- Modify: `app/services/premium_traffic_service.py`
- Modify: `app/database/crud/premium_traffic.py`
- Test: `tests/services/test_premium_traffic_xdrive.py`

**Interfaces:**
- `PremiumTrafficService._fetch_pasarguard_usage(api: PasarguardAPI) -> dict[int, tuple[int, int]]` keyed by subscription id, values `(used_traffic, lifetime_used_traffic)`; returns `{}` when `settings.is_xdrive_enabled()` is false or PasarGuard is not configured.
- `PremiumTrafficService._xdrive_delta(state, subscription, usage: tuple[int, int] | None) -> int`.
- `_apply_usage(..., pasarguard_usage: tuple[int, int] | None = None)` adds the delta after `_net_usage`.
- `start_new_period` resets `state.pasarguard_baseline_bytes = None` and `state.pasarguard_identity_created_at = None`.
- If the tariff has more than one premium squad for a subscription, the delta is 0 for that subscription.

- [ ] **Step 1: Write the failing tests**

```python
def test_delta_is_lifetime_minus_baseline():
def test_first_observation_sets_baseline_and_returns_zero():
def test_identity_recreation_resets_baseline_once():
    # marker differs -> 0; second call with same marker -> real delta
def test_lifetime_below_baseline_resets_baseline():
def test_absent_pasarguard_usage_returns_zero():
def test_period_reset_clears_pasarguard_baseline():
async def test_apply_usage_adds_xdrive_delta_to_squad_usage():
async def test_multi_squad_subscription_gets_zero_delta():
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/services/test_premium_traffic_xdrive.py -v`
Expected: FAIL with missing methods/columns.

- [ ] **Step 3: Implement the usage fetch and delta**

Fetch PasarGuard users once per `process_once` pass via the bulk endpoint; map `<prefix><subscription_id>` to the subscription id; on any PasarGuard error return `{}` and let the existing monotonic `record_usage` keep the last value. Apply the delta only to targets of subscriptions with exactly one premium squad.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/services/test_premium_traffic_xdrive.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/premium_traffic_service.py app/database/crud/premium_traffic.py \
  tests/services/test_premium_traffic_xdrive.py
git commit -m "feat: count xdrive lifetime usage in the premium pool"
```

---

### Task 8: Backfill, orphan cleanup, delete and revoke intents

**Files:**
- Modify: `app/services/pasarguard_sync_service.py`
- Test: `tests/services/test_pasarguard_sync_maintenance.py`

**Interfaces:**
- `PasarguardSyncService._backfill(db, api, users) -> int`
- `PasarguardSyncService._cleanup_orphans(db, api, users) -> int`
- `PasarguardSyncService._drain_delete_intents(db, api) -> int`
- `PasarguardSyncService._retry_pending_revokes(db, api) -> int`
- Module functions `async revoke_pasarguard_subscription(subscription, db) -> bool` and `async enqueue_pasarguard_delete(db, subscription) -> None` (the latter inserts the intent before the row is deleted).

- [ ] **Step 1: Write the failing tests**

```python
async def test_backfill_creates_only_eligible_subscriptions():
async def test_orphan_cleanup_deletes_only_prefixed_and_noted_users():
    # a manual user "bx999" without the note marker is NOT deleted
async def test_delete_intent_is_drained_after_subscription_is_gone():
async def test_pending_revoke_is_retried_and_cleared():
async def test_pasarguard_down_leaves_intents_in_place():
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/services/test_pasarguard_sync_maintenance.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the maintenance passes**

Backfill only subscriptions without an identity that the evaluator marks eligible. Orphan cleanup parses the prefix and note, deletes only owned users whose subscription id is absent, and never touches unmarked users. Orphan cleanup runs at most hourly (track the last run in memory); delete intents and pending revokes (`pasarguard_revoke_pending_at`) run every pass and are cleared after success.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/services/test_pasarguard_sync_maintenance.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/pasarguard_sync_service.py tests/services/test_pasarguard_sync_maintenance.py
git commit -m "feat: backfill and maintain PasarGuard identities"
```

---

### Task 9: Lifecycle wiring and startup

**Files:**
- Modify: `app/services/subscription_service.py`
- Modify: `app/services/subscription_deletion_service.py`
- Modify: `app/services/premium_traffic_service.py`
- Modify: `main.py`
- Test: `tests/services/test_xdrive_lifecycle_wiring.py`

**Interfaces:**
- `create_remnawave_user` calls `pasarguard_sync_service.nudge(subscription.id)` after a successful panel link.
- `revoke_subscription` calls `await revoke_pasarguard_subscription(...)` after the Remnawave revoke commits and before returning the new URL.
- `delete_subscription_record` calls `await enqueue_pasarguard_delete(db, subscription)` before `db.delete(subscription)`.
- `_limit_squad` and `_restore_squad` call `pasarguard_sync_service.nudge(subscription.id)` after success.
- `main.py` starts `pasarguard_sync_service.start_monitoring()` when `settings.is_pasarguard_configured()` (even when `XDRIVE_ENABLED` is off, so the drain pass runs) and stops it on shutdown.

- [ ] **Step 1: Write the failing wiring tests**

```python
async def test_revoke_calls_pasarguard_and_records_timestamp():
async def test_revoke_failure_sets_pending_and_does_not_break_remnawave_revoke():
async def test_delete_enqueues_intent_before_hard_delete():
async def test_premium_limit_and_restore_nudge_the_projector():
async def test_create_remnawave_user_nudges_the_projector():
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/services/test_xdrive_lifecycle_wiring.py -v`
Expected: FAIL.

- [ ] **Step 3: Wire the calls and audit hard-delete paths**

Add the calls. Then run `grep -rn "db.delete(subscription)" app/` and route every remaining hard-delete through `enqueue_pasarguard_delete` (or `delete_subscription_record`). Add the startup/shutdown task in `main.py` next to the premium worker.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/services/test_xdrive_lifecycle_wiring.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/subscription_service.py app/services/subscription_deletion_service.py \
  app/services/premium_traffic_service.py main.py tests/services/test_xdrive_lifecycle_wiring.py
git commit -m "feat: wire xdrive lifecycle events to the projector"
```

---

### Task 10: Lifecycle matrix integration tests and operator docs

**Files:**
- Create: `tests/integration/test_xdrive_lifecycle.py`
- Create: `docs/xdrive-pasarguard.md`
- Modify: `../claude-changes.md` (project changelog)

**Interfaces:** none new; this task proves the matrix in the spec.

- [ ] **Step 1: Write the integration tests**

One test per matrix row, using the in-memory DB and a fake PasarGuard API that records calls:

```python
async def test_purchase_creates_and_enables_user():
async def test_renewal_updates_expire_and_keeps_uuid():
async def test_expiry_disables_user():
async def test_general_traffic_limit_disables_user():
async def test_ban_disables_and_unban_enables():
async def test_premium_topup_reenables_user():
async def test_premium_period_reset_resets_baseline():
async def test_closed_premium_access_disables_and_reopened_enables():
async def test_grace_disables_user():
async def test_trial_without_premium_squad_gets_no_xdrive():
async def test_device_limit_change_does_not_touch_pasarguard():
async def test_tariff_switch_updates_expire_and_data_limit():
async def test_remnawave_webhook_import_is_projected():
async def test_feature_flag_off_disables_all_known_users():
async def test_multi_squad_tariff_gets_no_xdrive():
```

- [ ] **Step 2: Run the tests**

Run: `pytest tests/integration/test_xdrive_lifecycle.py -v`
Expected: PASS (if a row fails, fix the owning task's code, not the test).

- [ ] **Step 3: Write operator docs**

Create `docs/xdrive-pasarguard.md` covering: env variables, the PasarGuard group/inbound/host setup, the `encryption` requirement for xdrive, the Remnawave SRR rule `UA -> XRAY_JSON` above `Fallback Base64`, the flag-off drain procedure, and the xdrive secret-exposure risk. No secrets or real URLs.

- [ ] **Step 4: Append the changelog entry**

Add one entry to `../claude-changes.md` with scope, what, and why, following the project format. Do not put secrets there.

- [ ] **Step 5: Commit**

`../claude-changes.md` lives outside the repository by project rule; write the
entry but do not `git add` it.

```bash
git add tests/integration/test_xdrive_lifecycle.py docs/xdrive-pasarguard.md
git commit -m "test: cover xdrive lifecycle and document operations"
```
