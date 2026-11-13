# Expiry, Autopay and Discount Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make autopay/expiry notifications cycle-safe and retryable, and make the two expired-discount waves consistent in pricing, validity, eligibility, and Telegram UI.

**Architecture:** Keep persistent notification state in `SentNotification`, keyed by subscription and encoded `end_date` cycle where retry state must survive renewal/restart. Add a typed delivery outcome while preserving the existing `_send_message_with_logo` return contract for unrelated callers. Reuse `PricingEngine` for wave previews with an explicit personal-offer override, centralize wave-only claim/renewal guards, and restyle only existing purchase/renewal period keyboards.

**Tech Stack:** Python 3, SQLAlchemy async, Aiogram 3, pytest/pytest-asyncio, Ruff, JSON locales.

**Spec:** `docs/superpowers/specs/2026-11-14-expiry-autopay-discount-notifications-design.md`

## Global Constraints

- Bot-only, single-tariff, free trial, days_only referrals; do not change excluded legacy/provider/cart/cabinet/miniapp behavior.
- Do not enable Remnawave webhook user notifications or `NOTIFICATION_EXPIRED_1D_ENABLED`; do not change topup-triggered automatic extension.
- Preserve existing uncommitted work, including the four «Главное меню» additions and premium notifications only at 90%/exhaustion. Never use `git reset`, `git clean`, or overwriting checkout.
- Do not create commits or push. The commit steps from the generic planning template are intentionally replaced by working-tree verification.
- Only `ru.json` may change among locales; preserve LF everywhere.
- Wave types are exactly `expired_discount_wave2` and `expired_discount_wave3`.
- Wave period buttons use custom emoji `5335020345717973061`, final price plus `Скидка N%`, no old price/strike/arrows/`🔥−N%`; primary period and Back remain otherwise unchanged.
- After each logical implementation change, append the required entry to `../claude-changes.md` before completion reporting.

## Review Focus

- A rich send can succeed while returning `None`, and a timeout can return the same value: tests must assert typed `SENT` versus `TIMEOUT`, never truthiness (Tasks 1–2).
- A retry marker from an older `end_date`, or an old EXPIRED row with no pending marker, must never send: test restart-like fresh service instances and renewal before retry (Task 2).
- Sequential floor arithmetic can make nominal and effective percents differ: test 10% group + 20% wave and derive the displayed percent from the actual undiscounted/final totals (Tasks 3 and 6).
- Claim and renewal can race, and an existing better personal discount must survive both send and claim paths: test locked re-read, rejected writes, and no success screen (Tasks 4–5).
- Naive datetimes loaded from older SQLite rows and NULL legacy wave deadlines must not become perpetual discounts: test aware/naive boundaries, matched backfill, unmatched 24-hour grace, and cleanup (Tasks 4–5).

---

### Task 1: Typed Delivery Outcomes and Autopay Cycle Cadence

**Files:**
- Modify: `app/services/monitoring_service.py`
- Test: `tests/services/test_autopay_notification_cycle.py`
- Preserve existing tests in `tests/services/test_notification_redesign.py` and `tests/services/test_autopay_fail_notifications.py`, updating only assertions whose old behavior is explicitly removed by the spec.

**Interfaces:**
- Produces: `NotificationDeliveryStatus` enum with `SENT`, `FAILED`, `TIMEOUT`, `SKIPPED`, `UNREACHABLE` and `NotificationDeliveryResult(status, message=None)`.
- Produces: `_send_message_with_logo_result(...) -> NotificationDeliveryResult`; existing `_send_message_with_logo(...)` remains a compatibility wrapper returning `message` as before and accepts optional `notification_type` for Task 3.
- Produces: `_send_autopay_failed_notification_result(...) -> NotificationDeliveryStatus`; existing boolean `_send_autopay_failed_notification(...)` remains compatible.
- Persistent markers: `autopay_attention` (confirmed), `autopay_attention_unconfirmed` (ambiguous timeout), both with `days_before=warning_day`; confirmed delivery also records `expiring` for that day. Existing renewal cleanup starts a new cycle.

- [ ] **Step 1: Write failing cadence and outcome tests**
  Cover exact list membership and `ceil(hours_left / 24)` at 72/48/24-hour boundaries; one merged card for failures on days 3/2/1; failure then success; unconfigured day followed by configured day; late schedule; direct jump into a warning window; autopay-off manual warnings; no generic `AUTOPAY_SHORT`/`AUTOPAY_OK`, repeat/final messages; `charge_error` silence; retry after explicit `FAILED`; terminal suppression after `TIMEOUT`; and clean markers after `end_date` advances. Assert confirmed `SENT` even when rich delivery returns no `Message`, and no confirmed marker for timeout.
- [ ] **Step 2: Run the new tests and confirm the old behavior fails**
  Run: `pytest -q tests/services/test_autopay_notification_cycle.py`
  Expected: failures showing generic planned autopay warnings/final reminders and missing typed outcomes/persistent markers.
- [ ] **Step 3: Implement the minimal delivery outcome and cycle policy**
  Skip the entire generic planned-expiry branch when `subscription.autopay_enabled` is true. In `_maybe_notify_autopay_failure`, return immediately for non-`insufficient_balance` causes; notify only on an actual configured/user-allowed warning day and only if neither persistent attention marker exists. Record confirmed coverage only after `SENT`; record the unconfirmed terminal marker after `TIMEOUT`; leave explicit `FAILED` retryable. Do not use Redis or the three-hour timestamp as the source of user-facing cadence truth. Keep dormant helpers/templates compiling without invoking their removed cadence.
- [ ] **Step 4: Run focused and legacy autopay suites**
  Run: `pytest -q tests/services/test_autopay_notification_cycle.py tests/services/test_autopay_fail_notifications.py tests/services/test_notification_redesign.py tests/services/test_monitoring_notification_switches.py`
  Expected: PASS, except a pre-existing failure must be reported verbatim and not “fixed” by reverting the spec.
- [ ] **Step 5: Lint and inspect the task diff**
  Run Ruff on changed Python files and `git diff --check`; inspect that previous uncommitted production changes remain present. Do not commit.

### Task 2: Persistent Initial-Expired Retry

**Files:**
- Modify: `app/services/monitoring_service.py`
- Modify: `app/database/crud/notification.py` only if focused marker helpers keep retry code out of the service
- Test: `tests/services/test_expired_notification_retry.py`

**Interfaces:**
- Consumes: Task 1 `NotificationDeliveryStatus` and `_send_message_with_logo_result`.
- Produces: `_send_subscription_expired_notification_result(...) -> NotificationDeliveryStatus`, with existing boolean `_send_subscription_expired_notification(...)` preserved.
- Persistent type keys: `expired_retry:{int(subscription.end_date.timestamp())}` for pending state and `expired:{int(subscription.end_date.timestamp())}` for confirmed delivery. Pending `days_before` stores the next/current total attempt count, capped at 3.
- Produces: `_retry_expired_notifications(db) -> None`, invoked before new expiration processing so retries occur only on later monitoring cycles.

- [ ] **Step 1: Write failing retry tests**
  Cover initial success/no pending; failure→failure→success across fresh service instances; three failures/no fourth; renewal and changed `end_date` cancellation; pre-existing EXPIRED rows without pending markers; rich and classic timeout; blocked/deactivated/permanent Telegram errors; global/user gates; and no pending state when no initial Telegram attempt occurs. Assert pending state is persisted before each network attempt, so a restart cannot exceed three attempts.
- [ ] **Step 2: Run tests and verify failure**
  Run: `pytest -q tests/services/test_expired_notification_retry.py`
  Expected: FAIL because timeout is currently treated as success and no persistent retry recovery exists.
- [ ] **Step 3: Implement bounded persistent retry**
  Create cycle-keyed pending attempt 1 only on the actual initial bot path. On `SENT`, atomically remove pending and record confirmed expired delivery; on retryable `FAILED`/`TIMEOUT`, retain attempts 1–2 for later cycles; after unsuccessful attempt 3, remove pending and log operationally; on `UNREACHABLE`/`SKIPPED`, cancel pending. Retry only exact pending markers whose subscription is still EXPIRED in the same cycle. Never infer pending from an EXPIRED row alone. Existing `clear_notifications` on renewal remains the primary cleanup, with status/cycle checks as defense in depth.
- [ ] **Step 4: Run retry and notification regression suites**
  Run: `pytest -q tests/services/test_expired_notification_retry.py tests/services/test_monitoring_send_timeout.py tests/services/test_notification_redesign.py tests/utils/test_rich_notify.py`
  Expected: PASS; explicitly report any unrelated platform/guard failure.
- [ ] **Step 5: Lint and diff check**
  Run Ruff on changed Python files and `git diff --check`. Do not commit.

### Task 3: Wave Preview Pricing, Card Layout, and Emoji Gate

**Files:**
- Modify: `app/services/monitoring_service.py`
- Modify: `app/services/pricing_engine.py`
- Modify: `app/utils/notification_emoji.py`
- Modify: `app/localization/locales/ru.json`
- Test: `tests/services/test_discount_wave_notifications.py`
- Test: `tests/utils/test_notification_emoji.py`
- Test: `tests/test_pricing_engine.py`

**Interfaces:**
- Produces: optional `promo_offer_percent_override: int | None = None` on `PricingEngine.calculate_renewal_price(...)`; `None` preserves current behavior, while a number replaces (not stacks with) the existing personal offer for preview arithmetic.
- Produces: `_resolve_renewal_period_days(subscription) -> int`, shared by normal renewal cost and wave card preview.
- Wave send passes `notification_type='expired_discount_wave2'` or `'expired_discount_wave3'` to Task 1’s sender; those exact strings join `EXCLUDED_NOTIFICATION_TYPES`.

- [ ] **Step 1: Write failing pricing/card/gate tests**
  Assert a 1000 ₽ base with previous 10% personal offer and new 20% wave displays 800 ₽, not 720 ₽; group 10% + wave 20% charges sequential 28%; the card names the resolved period and same-period checkout matches. Assert exact Russian HTML, `5303040337059543238` preservation in rich/classic sends, claim icon `5235695112419303615` primary, topup icon `5258500422393415126` non-primary, callbacks and row order. Pin unchanged `SUBSCRIPTION_EXPIRED` `⛔`→`5258382581375723416` behavior. Assert an active personal discount >= proposed wave suppresses offer creation/sending.
- [ ] **Step 2: Run focused tests and verify failure**
  Run: `pytest -q tests/services/test_discount_wave_notifications.py tests/utils/test_notification_emoji.py tests/test_pricing_engine.py`
  Expected: new pricing, layout, period, icon, and suppression assertions FAIL.
- [ ] **Step 3: Implement wave preview and card policy**
  Resolve the same autopay/default/shortest period as renewal. Calculate group-adjusted renewal pricing with the wave percent as the only personal offer, using existing sequential integer arithmetic; never temporarily mutate the SQLAlchemy user. Put `{period}` and exact final `{discounted_price}` in both wave templates. Suppress wave creation/send while a better/equal active personal offer exists. Pass only the two wave identities through the narrow emoji-gate opt-out.
- [ ] **Step 4: Run focused and existing wave suites**
  Run: `pytest -q tests/services/test_discount_wave_notifications.py tests/services/test_monitoring_notification_fixes.py tests/services/test_notification_redesign.py tests/utils/test_notification_emoji.py tests/test_pricing_engine.py`
  Expected: PASS.
- [ ] **Step 5: Validate locale, lint, and diff**
  Parse `ru.json`; run Ruff on changed Python files and `git diff --check`; confirm non-Russian locales are untouched. Do not commit.

### Task 4: Wave Claim UI, Original Deadline, and No-Downgrade Guard

**Files:**
- Modify: `app/handlers/subscription/promo.py`
- Modify: `app/database/crud/discount_offer.py`
- Modify: `app/localization/locales/ru.json`
- Test: `tests/handlers/test_discount_wave_claim.py`

**Interfaces:**
- Consumes: exact wave type strings and active-discount helper.
- Produces: wave-only claim path that locks/re-reads the offer and its subscription, uses `offer.expires_at` unchanged for `db_user.promo_offer_discount_expires_at`, and preserves existing behavior for all non-wave offers.
- Reuses: `build_main_menu_button`, `build_miniapp_or_callback_button`, `subscription:upgrade`, `success:main_menu`, callbacks `subscription_upgrade` and `back_to_menu`.

- [ ] **Step 1: Write failing claim tests**
  Cover claim one minute before deadline (stored/success deadline exactly `offer.expires_at`); post-deadline rejection without user changes; active better/equal discount rejection without writes or success message; valid expired-subscription claim; renewed/deactivated-offer rejection; non-wave offer on active subscription unchanged; and race-boundary lock/re-read. Assert popup exactly `Скидка активирована!`, exact success HTML/deadline, purchase primary/icon `5334552404736104314`/callback, main-menu primary/icon `5314310000531766389`/callback, and row order.
- [ ] **Step 2: Run handler tests and verify failure**
  Run: `pytest -q tests/handlers/test_discount_wave_claim.py`
  Expected: FAIL on hardcoded popup, fresh/no expiry, missing guards, and old success buttons.
- [ ] **Step 3: Implement the wave-only claim branch**
  Lock the offer for update and independently re-read its subscription. Reject inactive/expired/claimed offers, active renewed subscriptions, and better/equal current personal discounts before writing any `promo_offer_discount_*` field. For a valid wave claim, set the original offer deadline and do not apply `active_discount_hours`. Use the exact wave popup/success strings and existing styled button helpers; leave other offer types on their current branch.
- [ ] **Step 4: Run claim and promo regression suites**
  Run: `pytest -q tests/handlers/test_discount_wave_claim.py tests/handlers/test_promo_segment_counts.py tests/test_miniapp_payments.py`
  Expected: PASS; no miniapp/cabinet claim behavior is broadened.
- [ ] **Step 5: Validate locale, lint, and diff**
  Parse `ru.json`, run Ruff and `git diff --check`. Do not commit.

### Task 5: Renewal Invalidation, Expiry Cleanup, and Legacy Backfill

**Files:**
- Modify: `app/database/crud/discount_offer.py`
- Modify: `app/database/crud/subscription.py`
- Modify: `app/database/crud/user.py`
- Modify: `app/services/monitoring_service.py`
- Modify: `app/utils/promo_offer.py` only for safe datetime normalization if needed
- Test: `tests/services/test_discount_wave_validity.py`
- Test: `tests/crud/test_discount_wave_renewal.py`

**Interfaces:**
- Produces: `deactivate_unclaimed_wave_offers(db, subscription_id, *, commit=False) -> int`, affecting only active, unclaimed offers of the two wave types.
- Produces: `backfill_wave_discount_expiry(db, *, now=None) -> tuple[int, int]` returning `(matched, unmatched)`; unmatched rows receive `now + timedelta(hours=24)` and only aggregate counts are logged.
- Successful positive-day `extend_subscription` invokes deactivation in the same transaction before commit; the monitoring cycle runs backfill before expired-discount cleanup.

- [ ] **Step 1: Write failing validity and renewal tests**
  Cover renewal deactivating only outstanding wave offers; claimed-before-renewal discount retained until original deadline; non-wave offers unchanged; active discount one-shot consumption; expired percent returning zero; no revival from a saved checkout/topup intent while leaving topup-triggered extension itself unchanged; provider snapshot tests remaining valid; matched NULL-deadline backfill; past matched deadline cleanup; unmatched 24-hour grace; and naive/aware datetime boundaries.
- [ ] **Step 2: Run new tests and verify failure**
  Run: `pytest -q tests/services/test_discount_wave_validity.py tests/crud/test_discount_wave_renewal.py`
  Expected: FAIL on post-renewal offers and indefinite NULL wave discounts.
- [ ] **Step 3: Implement transactional invalidation and bounded backfill**
  Deactivate only wave offers in the successful positive-day extension transaction; do not revoke already claimed user discounts. Backfill each NULL wave user deadline from its matching claimed offer, otherwise grant one 24-hour migration window from backfill time and log only the unmatched count. Let the existing cleanup clear already expired values. Do not add provider orchestration, saved-cart snapshots, schema migrations, or user-facing migration messages.
- [ ] **Step 4: Run validity, renewal, payment, and topup suites**
  Run: `pytest -q tests/services/test_discount_wave_validity.py tests/crud/test_discount_wave_renewal.py tests/test_pricing_engine.py tests/test_miniapp_payments.py tests/services/test_subscription_extend_cabinet_mode.py`
  Expected: PASS, with any platform-specific PostgreSQL guard failure reported separately.
- [ ] **Step 5: Lint and diff check**
  Run Ruff on changed Python files and `git diff --check`. Do not commit.

### Task 6: Active-Discount Period Buttons

**Files:**
- Modify: `app/keyboards/inline.py`
- Modify: `app/handlers/subscription/tariff_purchase.py`
- Test: `tests/handlers/test_discounted_period_buttons.py`
- Test: `tests/test_tariff_highlight_period.py`

**Interfaces:**
- Consumes: active personal/group discount helpers and current final-price arithmetic.
- Applies only to classic purchase/renewal `get_subscription_period_keyboard` and tariff purchase/renewal variants `get_tariff_periods_keyboard`, `get_tariff_periods_keyboard_with_traffic`, `get_tariff_extend_keyboard`; tariff-switch and unrelated buttons retain current appearance.

- [ ] **Step 1: Write failing keyboard appearance tests**
  For discounted variants assert every period row has icon `5335020345717973061`, text is `{period} — {final_price} · Скидка {effective_percent}%` (plus any pre-existing best-value badge where applicable), primary remains only period 30, Back text/icon/callback/style are unchanged, and no old price/strike/arrow/`🔥−N%` appears. Test group+offer effective percent from actual sequential totals. After personal-offer expiry with no group discount, assert original price-only labels/icons return; with a remaining group discount, discounted presentation remains.
- [ ] **Step 2: Run keyboard tests and verify failure**
  Run: `pytest -q tests/handlers/test_discounted_period_buttons.py tests/test_tariff_highlight_period.py`
  Expected: FAIL on current `🔥−N%` labels and missing per-row custom icons.
- [ ] **Step 3: Implement shared discounted button formatting**
  Add a small helper for final-price label/icon selection and use it in the listed purchase/renewal keyboards only. Compute effective percent from undiscounted subtotal and final charged price, clamping only invalid/zero-base display cases; do not change pricing or confirmation screens. Leave no-discount appearance and Back construction byte-for-byte equivalent in behavior.
- [ ] **Step 4: Run keyboard and purchase regression suites**
  Run: `pytest -q tests/handlers/test_discounted_period_buttons.py tests/test_tariff_highlight_period.py tests/handlers/test_autopay_toggle_screen.py tests/handlers/test_tariff_extend_subscription_id.py`
  Expected: PASS.
- [ ] **Step 5: Lint, full verification, changelog, and structure reference**
  Run Ruff on all changed Python files and `git diff --check`. Run all focused suites above, then the repository full test command (`pytest -q`, unless repository configuration documents another canonical command). Regenerate `docs/project_structure_reference.md` only if its generator tracks the added plan/tests. Append one consolidated entry to `../claude-changes.md`; report actual failures including known Windows termios/SIGHUP/path/grace-access issues without calling the suite green. Do not commit or push.
