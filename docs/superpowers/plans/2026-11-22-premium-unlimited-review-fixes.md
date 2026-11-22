# Premium Unlimited Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement task-by-task. Check steps after verification.

**Goal:** Address the five review findings without changing the database schema.

**Architecture:** Use existing Redis cache for actor-bound Telegram action tokens. Keep database state authoritative for enforcement and make rendered unlimited status respect manual closure.

**Tech Stack:** Python, aiogram, Redis, SQLAlchemy, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-11-22-premium-unlimited-review-fixes-design.md`

## Global Constraints

- Work in the named worktree, leave local EOL-only test changes untouched, do not change cabinet routes or schema.
- Existing Telegram buttons without valid tokens must fail before any write or debit. Enforce the 64-byte callback limit. No new dependency.
- Run each regression RED before editing product code, then GREEN. Leave worktree uncommitted unless asked to commit. Append project changelog before completion.

## Review Focus

- Grant between quote and click shifts index; do not buy the adjacent package even if its size matches.
- Price or selected subscription changes, token expires, or confirm is pressed twice; do not silently debit.
- Admin list reorder or missing target; do not change a different squad. Real frozen callback must render after commit.
- Panel failure following tariff switch; the worker retries the still-entitled squad instead of losing the restore signal.
- Closed unlimited plus an active finite squad; proxy shows finite total, menu refreshes, reopening restores infinity.

### Task 1: Bind Telegram top-up to the shown quote

Files: `app/handlers/subscription/traffic.py`, `app/handlers/menu.py` if needed, new `app/utils/premium_callbacks.py`, `tests/handlers/test_premium_topup_flow.py`.

- [x] Write `test_confirmed_squad_granted_unlimited_does_not_buy_neighbor`, `test_premium_confirmation_rejects_changed_price_or_subscription` and an expired/replayed token test. Assert no debit or package/corresponding cart on refusal; add or preserve the happy path.
- [x] Run `.venv/Scripts/python.exe -m pytest -q tests/handlers/test_premium_topup_flow.py`; confirm new scenarios fail before production changes.
- [x] In `premium_callbacks.py`, implement `store_premium_callback(actor_id: int, action: str, payload: dict) -> str | None` and `read_premium_callback(actor_id: int, action: str, token: str, *, consume: bool = False) -> dict | None`. Use actor/action-scoped keys, a short random token and 10-minute TTL; use `cache.getdel` for confirmations. `traffic.py` stores subscription ID, UUID, GB and final kopecks; buys using the captured UUID, rejects a mismatched subscription or fresh price before debit/cart, and keeps the locked apply check. Update older test fixtures to use genuine confirmation contexts while retaining a test for rejected legacy callbacks.
- [x] Rerun that module and `tests/services/test_premium_traffic_purchase.py`; require all green.

### Task 2: Bind admin actions and refresh the card

Files: `app/handlers/admin/users.py`, `tests/handlers/test_admin_users_premium.py`. Uses the Task 1 callback store/read helpers.

- [x] Add a grant/revoke round-trip test using a real frozen `CallbackQuery`, reorder rows after creating a button and assert the intended UUID is changed and rendered. Cover legacy/missing context, no state created on empty revoke, and Telegram's 64-byte limit.
- [x] Run `.venv/Scripts/python.exe -m pytest -q tests/handlers/test_admin_users_premium.py`; confirm the new scenarios expose the wrong target or render failure.
- [x] On rendering the two new admin action buttons, store exact target user ID, subscription ID and UUID. Read by actor and action, show the name on confirmation, consume confirmation and find the target row by UUID. Revalidate eligibility and existing locked state. For revoke, use locked `get_state` (no creation). Recompute the card position from UUID or use the exact target in the renderer, never assign `callback.data`.
- [x] Rerun the admin test module and require all green.

### Task 3: Preserve the worker's restore signal across a tariff switch

Files: `app/database/crud/subscription.py`, `app/services/premium_traffic_service.py`, `tests/crud/test_drop_orphan_premium_states.py`, `tests/services/test_premium_traffic_service.py`.

- [x] Add `test_unlimited_orphan_after_tariff_switch_retries_panel_restore`: local membership remains, old row is limited+unlimited, first write fails, second succeeds. Assert the flag stays set after failure and is only cleared after success; grant survives both. Update the old CRUD assertion that expected premature clearing.
- [x] Run the two test modules; confirm the new test fails because the flag is already cleared after tariff change.
- [x] Preserve `is_limited` in `_drop_orphan_premium_states` for retained grants. Use `_clear_orphan` to send the updated membership and only persist clearing after successful, non-deferred panel write; closed rows stay untouched. Avoid unnecessary write/commit for already-restored retained rows.
- [x] Rerun the two test modules and related grace/multi-tariff tests.

### Task 4: Display only effective unlimited access

Files: `app/utils/rich_menu.py`, `app/webserver/subscription_proxy.py`, `tests/utils/test_premium_squad_lines.py`, `tests/webserver/test_subscription_proxy.py`, `tests/services/test_live_menu.py`.

- [x] Add tests for a closed grant not rendering infinity, a closed grant not forcing a mixed subscription total to zero, and fingerprint changing on closure/reopen without revoking the grant.
- [x] Run the three test modules; confirm the new expectations fail.
- [x] Use the same effective condition (`unlimited_at is not None` and `closed_at is None`) for the line, fingerprint, and proxy. Preserve the existing finite display and aggregation paths.
- [x] Rerun those modules and verify.

### Task 5: Verify and document

- [x] Run `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider` and record all failures/skips; compare unexpected failures with baseline if needed.
- [x] Run Ruff check/format check for changed Python files and `git diff --check`; confirm local EOL-only diffs were untouched. Update `docs/premium-traffic-limits.md` where behavior changed; record one entry in project `../claude-changes.md`.
