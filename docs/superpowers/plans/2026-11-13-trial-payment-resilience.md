# Trial Onboarding and Payment Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover trial users who never connect and payers whose payment stalls or fails, using the existing Remnawave webhook, payment verifier, and amount-first top-up flow.

**Architecture:** Add small adapters around existing event paths rather than new schedulers. Remnawave connection events get durable dedupe; payment verification emits a rate-limited admin digest; failed provider statuses render or send the existing payment-method keyboard with the failed method removed.

**Tech Stack:** Python 3.14, aiogram 3, FastAPI, SQLAlchemy async, Redis, pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-11-13-trial-payment-resilience-design.md`

## Global Constraints

- Telegram bot only: do not change cabinet, Mini App, Web API, pricing, crediting, checkout, tariffs, referrals, or panel-sync behavior.
- No database migration and no new trial polling service.
- Use existing `SentNotification` markers for Remnawave connection dedupe and existing Redis cache plus an in-memory fallback for payment-alert/fallback cooldowns.
- Payment alert defaults are exactly: `PAYMENT_ALERTS_ENABLED=true`, `PAYMENT_ALERT_PENDING_MINUTES=30`, `PAYMENT_ALERT_REPEAT_HOURS=6`, `PAYMENT_ALERT_MAX_PER_CYCLE=20`.
- Only the new `send_payment_alert` path bypasses `ADMIN_NOTIFICATIONS_ENABLED`; all existing notification paths keep their current gating.
- YooKassa failure statuses are `canceled` and `failed`; Heleket failure statuses are `cancel`, `fail`, and `system_fail`.
- Exclude only the exact failed method (`yookassa`, `yookassa_sbp`, or `heleket`), not every rail of the same provider.
- Proactive fallback cooldown is 24 hours per provider/payment; manual status screens always render the fallback.
- Russian user-facing additions go only into `app/localization/locales/ru.json`.
- Follow TDD: write the named failing test first, run it, then implement.
- Append one entry per task to `E:/Misc/repository/bedolaga/claude-changes.md` (the project-mandated changelog outside the repository).
- Do not push, deploy, change production configuration, or contact external services. Commit messages contain no attribution trailers and no PR references.

## Review Focus

- Repeated or reordered Remnawave events must not spam a user or send a nudge after first connection (Task 1 tests).
- A failed delivery must not be recorded as a successful Remnawave nudge (Task 1 test).
- Pending age exactly 30 minutes alerts; 29 minutes does not; cooldown works without Redis (Task 2 tests).
- Payment-alert bypass must be limited to the new payment-alert method and must not enable other admin notifications (Task 2 tests).
- Successful, pending, overpaid, and refunded payments must never receive failure fallback; alternative methods and support/back navigation must remain usable (Task 3 tests).

---

### Task 1: Remnawave trial connection nudges without duplicates

**Files:**
- Modify: `app/services/remnawave_webhook_service.py`
- Modify: `main.py`
- Test: `tests/services/test_remnawave_connection_onboarding.py`
- Changelog: `E:/Misc/repository/bedolaga/claude-changes.md`

**Interfaces:**
- Consumes: `notification_sent(db, user_id, subscription_id, notification_type, days_before=None)` and `record_notification(...)` from `app.database.crud.notification`.
- Produces: `_notify_user(...) -> bool` (existing callers may ignore the result); marker types `webhook_first_connected` and `webhook_not_connected`, with the hour threshold in `days_before`.

- [ ] **Step 1: Write failing webhook dedupe tests**

Create tests for:
1. two `user.not_connected` events with the same subscription and `notConnectedAfterHours=1` call `_notify_user` only once and record `webhook_not_connected` with `days_before=1`;
2. a failed first delivery (`_notify_user` returns `False`) does not record the marker and permits a later retry;
3. after `_handle_first_connected`, a later not-connected event sends nothing;
4. duplicate first-connected events notify once and record `webhook_first_connected`;
5. `main.py`'s `should_start_web_app` expression includes `settings.is_remnawave_webhook_enabled()` as an independent OR condition.

Use mocks for DB marker helpers and `_notify_user`; do not require PostgreSQL or Telegram.

- [ ] **Step 2: Run the new tests and verify they fail**

Run: `python -m pytest tests/services/test_remnawave_connection_onboarding.py -q`
Expected: failures because `_notify_user` has no boolean result, markers are absent, and the startup predicate lacks the Remnawave condition.

- [ ] **Step 3: Implement durable event dedupe and startup wiring**

In `_notify_user`, return `False` on every existing early return/exception and return the boolean result of `notification_delivery_service.send_notification` on success; keep all existing text, keyboard, and preference behavior unchanged.

In `_handle_user_not_connected`, when a subscription exists:
- normalize `notConnectedAfterHours` to a non-negative integer, defaulting invalid/missing values to `0`;
- skip if `webhook_first_connected` exists;
- skip if `webhook_not_connected` exists for that normalized hour;
- record `webhook_not_connected` only after `_notify_user` returns `True`.

In `_handle_first_connected`, dedupe the notification with `webhook_first_connected`; after handling the event, ensure the marker exists so later not-connected events are suppressed even if first-connected notifications are disabled.

Add `or settings.is_remnawave_webhook_enabled()` to `should_start_web_app` in `main.py`.

- [ ] **Step 4: Run focused and existing webhook tests**

Run: `python -m pytest tests/services/test_remnawave_connection_onboarding.py tests/services/test_remnawave_expiration_webhook.py tests/webserver/test_unified_app.py -q`
Expected: all pass.

- [ ] **Step 5: Append changelog and commit**

Add the Task 1 entry to the external changelog, then commit only these files with message `feat: dedupe trial connection webhook nudges`.

### Task 2: Independent pending-payment and provider-error alerts

**Files:**
- Create: `app/services/payment_alert_service.py`
- Modify: `app/services/payment_verification_service.py`
- Modify: `app/services/admin_notification_service.py`
- Modify: `app/config.py`
- Modify: `.env.example`
- Test: `tests/services/test_payment_alert_service.py`
- Test: `tests/services/test_payment_verification_alerts.py`
- Changelog: `E:/Misc/repository/bedolaga/claude-changes.md`

**Interfaces:**
- Consumes: existing `PendingPayment`, `list_recent_pending_payments`, `run_manual_check`, `method_display_name`, and `PaymentService.bot`.
- Produces: `PaymentAlert` dataclass with `kind`, `method`, `local_id`, `identifier`, `amount_kopeks`, `status`, `age_minutes`; `build_pending_alert(record, now)`, `build_check_error_alert(record, now)`, and `dispatch_payment_alerts(bot, alerts) -> bool`.
- Produces: `AdminNotificationService.send_payment_alert(text) -> bool` and optional `_send_message(..., bypass_master=False)` keyword; new category `NotificationCategory.PAYMENTS`.

- [ ] **Step 1: Write failing alert boundary, cooldown, and bypass tests**

Cover:
- pending age 29 minutes returns no alert; exactly 30 returns `kind='pending'`;
- provider check failure returns `kind='check_error'` and formatted output contains method/identifier/status/age but no exception text;
- first digest sends, identical alert keys are suppressed for 6 hours, and the same behavior works with Redis disconnected;
- more than 20 alerts produces 20 item lines plus a single `… и ещё N` line;
- `send_payment_alert` reaches `_send_message(..., bypass_master=True)` with `ADMIN_NOTIFICATIONS_ENABLED=false`, while the ordinary `_send_message` default remains master-gated;
- verification exceptions are collected and the digest is dispatched only after the DB commit, without changing `run_manual_check` crediting behavior.

- [ ] **Step 2: Run the new tests and verify they fail**

Run: `python -m pytest tests/services/test_payment_alert_service.py tests/services/test_payment_verification_alerts.py -q`
Expected: failures because the alert service, settings, category, and dedicated send path do not exist.

- [ ] **Step 3: Implement alert construction, cooldown, and admin delivery**

Add the five settings from Global Constraints to `Settings` and document them in `.env.example`.

Implement `payment_alert_service.py` with sanitized digest formatting and cooldown keys `payment_alert:{kind}:{method.value}:{local_id}`. Use `cache.setnx(..., expire=6*3600)` when connected and a single-process timestamp map otherwise. Delete/release claims when delivery fails so the next cycle can retry. Limit item lines to 20 and append the overflow count.

Add `PAYMENTS` to category topics and category-enabled handling. Add `send_payment_alert` that requires `PAYMENT_ALERTS_ENABLED` and a configured chat, then calls `_send_message(..., category=PAYMENTS, bypass_master=True)`. Add the optional `bypass_master` keyword without changing default behavior.

In `AutoPaymentVerificationService._run_checks`, collect pending alerts at the configured age and check-error alerts when `run_manual_check` raises. Dispatch once after the successful session commit using `self._payment_service.bot`; never send exception payloads in the digest.

- [ ] **Step 4: Run focused and existing payment/admin tests**

Run: `python -m pytest tests/services/test_payment_alert_service.py tests/services/test_payment_verification_alerts.py tests/services/test_payment_service_webhooks.py tests/services/test_admin_notification_hardening.py -q`
Expected: all pass.

- [ ] **Step 5: Append changelog and commit**

Add the Task 2 entry to the external changelog, then commit only these files with message `feat: alert admins about stalled payment verification`.

### Task 3: Alternative payment methods after YooKassa/Heleket failure

**Files:**
- Create: `app/services/payment_fallback_service.py`
- Modify: `app/keyboards/inline.py`
- Modify: `app/handlers/balance/yookassa.py`
- Modify: `app/handlers/balance/heleket.py`
- Modify: `app/services/payment/yookassa.py`
- Modify: `app/services/payment/heleket.py`
- Modify: `app/localization/locales/ru.json`
- Test: `tests/services/test_payment_fallback_service.py`
- Test: `tests/handlers/test_failed_payment_fallback.py`
- Changelog: `E:/Misc/repository/bedolaga/claude-changes.md`

**Interfaces:**
- Consumes: `get_payment_methods_keyboard(amount_kopeks, language, back_callback='balance_topup')`, payment records/users, and the existing `topup_amount|method|kopeks` flow.
- Produces: optional `exclude_methods: Collection[str] | None = None` on `get_payment_methods_keyboard`.
- Produces: `build_payment_fallback(user, amount_kopeks, failed_method) -> tuple[str, InlineKeyboardMarkup]` and `send_payment_fallback_once(bot, user, amount_kopeks, failed_method, payment_id) -> bool`.

- [ ] **Step 1: Write failing keyboard, status, and idempotency tests**

Cover:
- with Stars, both YooKassa rails, and Heleket enabled, excluding `yookassa` removes only the card callback and preserves `yookassa_sbp`, Stars, and Heleket at the original amount;
- excluding `heleket` removes only Heleket; no alternative methods leaves support/back navigation usable;
- YooKassa `canceled`/`failed` and Heleket `cancel`/`fail`/`system_fail` invoke the proactive sender once per provider/payment in 24 hours;
- successful, pending, `paid_over`, and `refund_paid` never invoke it;
- a second manual status check renders the fallback even after a proactive message was already sent;
- proactive send failure releases the cooldown key for retry.

- [ ] **Step 2: Run the new tests and verify they fail**

Run: `python -m pytest tests/services/test_payment_fallback_service.py tests/handlers/test_failed_payment_fallback.py -q`
Expected: failures because exclusion, fallback builder, and status hooks do not exist.

- [ ] **Step 3: Implement fallback keyboard, builder, and provider hooks**

Add `exclude_methods` to `get_payment_methods_keyboard`; filter method rows before applying configured names/order while preserving back/support controls and exact amount callbacks.

Implement `payment_fallback_service.py`:
- `build_payment_fallback` returns Russian localized text with the original amount and the filtered keyboard; append a support URL button when configured and retain a working back callback;
- `send_payment_fallback_once` requires a Telegram user, claims `payment_fallback:{failed_method}:{payment_id}` for 24 hours with `cache.setnx`, sends a new message, and deletes the claim on exception/failure;
- use an in-memory single-process fallback when Redis is disconnected.

Call the proactive sender after provider status is persisted:
- YooKassa webhook/status sync for `canceled`/`failed`; map `payment_method_type='sbp'` to `yookassa_sbp`, otherwise `yookassa`;
- Heleket `_process_heleket_payload` for `cancel`/`fail`/`system_fail`, before its non-paid early return and without touching crediting.

In both manual status handlers, render/edit the fallback for the same failure statuses even if proactive delivery already happened. Do not alter success, pending, wrong-amount, overpayment, or refund flows.

Add only the required Russian keys to `ru.json`.

- [ ] **Step 4: Run focused and existing payment/top-up tests**

Run: `python -m pytest tests/services/test_payment_fallback_service.py tests/handlers/test_failed_payment_fallback.py tests/services/test_payment_service_webhooks.py tests/handlers/test_balance_topup_retry_hint.py tests/utils/test_payment_button_styles.py -q`
Expected: all pass.

- [ ] **Step 5: Append changelog and commit**

Add the Task 3 entry to the external changelog, then commit only these files with message `feat: offer alternative methods after failed topups`.
