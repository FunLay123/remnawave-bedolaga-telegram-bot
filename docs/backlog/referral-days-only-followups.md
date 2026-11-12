# Referral days_only — backlog

Date: 2026-11-12
Context: after P0 (daily first-purchase policy + per-side days), P1 (marker unique
index/lock/retries) and the quick-win batch. The quick-win batch covers:
admin marker N+1, zero-marker filters in activity/analytics, dead `COUNT(DISTINCT)`,
`.gitattributes` LF rules, config getter deduplication, structured logs for
`daily_not_counted`/marker lock/marker conflict, and the daily-not-counted UI hint.

The items below are deliberately deferred. None blocks the current deployment;
they are ordered by cost/risk.

## Medium

- **Chunk the first-purchase backfill.** `backfill_first_purchase_markers`
  still builds one `IN (candidate_ids)` for the already-marked check
  (`app/services/referral_purchase_service.py:294`) and locks all candidate user
  rows in one batch. On a large `users`/`referral_earnings` table this can hit
  asyncpg's parameter limit and hold many row locks. Chunk candidates and commit
  per chunk.
- **Countries add-on false "paid".** `has_paid_subscription_condition` excludes
  traffic/device add-ons by description but not "Добавление стран к подписке"
  / "Adding countries to subscription" (`app/handlers/subscription/countries.py:392`,
  `app/cabinet/routes/subscription_modules/servers.py:218`). A historical
  countries-only payment can produce a first-purchase marker and deny the real
  first-purchase reward. Check historical rows with:
  `SELECT count(*) FROM transactions WHERE type = 'subscription_payment' AND is_completed = true AND (description ILIKE '%стран%' OR description ILIKE '%countries%');`
  If non-zero, add narrow patterns or move add-ons to a dedicated transaction
  type/flag.
- **Broader PostgreSQL tests.** CI already has `make test-postgres`
  (`TEST_DATABASE_URL=... REQUIRE_POSTGRES_TESTS=1 pytest -m postgres`). Add
  PG-only tests for: concurrent backfill vs purchase, admin attach vs purchase,
  and applying migrations 0135/0136 to a real PostgreSQL schema.
- **Backfill maintenance guard.** `--apply` now documents the quiesced-window
  requirement and tolerates per-candidate `IntegrityError`, but it cannot tell a
  live first purchase from a historical one. Consider an explicit
  `--purchases-paused` confirmation flag and a clearer operator warning.
- **Marker reconciliation tool.** A small periodic/admin job that finds users with
  a real `SUBSCRIPTION_PAYMENT` but no first-purchase marker and writes it. This
  closes rare marker failures without running the full backfill.
- **Single `reward_only()` predicate.** Several stats now filter the anchor and
  zero marker one by one (`user_activity_service`, `partner_stats_service`,
  `admin_stats`, `admin_users`, withdrawal reports). Centralize it so a new
  zero-reason cannot leak into statistics again.

### Review-round findings (2026-11-13, deferred)

- **Saved-cart auto-purchase does not call the first-purchase hook.**
  `_auto_extend_subscription` (`app/services/subscription_auto_purchase_service.py:700`)
  and `_auto_purchase_tariff` (`:1071`) charge the balance and create a
  `SUBSCRIPTION_PAYMENT` but never call `register_first_purchase_after_purchase`.
  A first real purchase through the saved-cart-after-top-up flow gets no days
  and no marker; the next purchase is rewarded as the first.
  `AUTO_PURCHASE_AFTER_TOPUP_ENABLED=false` in this instance, so dormant until
  the flow is enabled.
- **Fixed 2026-11-13:** `SubscriptionRenewalService.finalize` and
  `confirm_instant_switch` now call `register_first_purchase_after_purchase`;
  `menu.py` passes `skip_first_purchase_hook=True` to avoid a duplicate.
- **Withdrawal audit zero-row leaks.** `earnings_by_reason`
  (`app/services/referral_withdrawal_service.py:369`) still groups the zero
  registration anchor and the zero first-purchase marker as fake income reasons,
  and `get_first_referral_earning_date` (`:69`) still counts the zero marker as
  the first earning. Withdrawal is disabled in days_only, so audit-only today.
- **Countries add-on false "paid"** remains as described in Medium above.
  Provider-recurring daily charges also escape `DAILY_CHARGE_PATTERNS`:
  Platega/Cashera descriptions like `СБП-автопродление Platega` /
  `Автопродление <cashera>` are `SUBSCRIPTION_PAYMENT` and would be treated as
  a real subscription purchase.
- **Generic anchor-write rollback discards caller attribution.**
  `app/services/referral_service.py:783` rolls back the caller's session when
  the anchor insert fails, dropping a flushed-but-uncommitted `referred_by_id`
  (campaign/cabinet auth) while still reporting success. Bot-only paths are
  mostly unaffected; fix if campaign/cabinet surfaces are enabled.
- **NOWAIT marker drop has no later retry.** The existing-anchor retry uses
  `lock_nowait` (`app/services/referral_service.py:753`) and simply skips when
  the caller holds the user row. Add a retry/reconciliation path (see the marker
  reconciliation bullet) so a skipped marker is eventually written.
- **Withdrawal stranded after scheme switch.** `is_referral_withdrawal_enabled()`
  hard-disables withdrawal in days_only, so money earned under a previous
  legacy scheme cannot be withdrawn after switching. Product decision: allow
  withdrawal of the legacy balance or explicitly write it off.
- **Zero-amount `referral_first_topup` bookkeeping reuse.** The diagnostics
  path writes `amount_kopeks=0, reason='referral_first_topup'` pair markers;
  user-facing earnings queries do not filter them, so they can consume a
  recent-earnings slot or top-5 row. Legacy diagnostics are gated off in
  days_only, so this is dormant.

## Architecture

- **Dedicated transaction type/metadata for daily and add-ons.** The current
  predicates rely on hardcoded Russian descriptions (`DAILY_CHARGE_PATTERNS`,
  `ADDON_DESCRIPTION_PATTERNS`). A new payment path silently escapes the filter.
  Replace with a transaction type, a `transaction_kind` column, or a metadata
  JSON field.
- **Post-commit marker retry instead of `nowait`.** The existing-anchor retry uses
  `FOR UPDATE NOWAIT` + skip to avoid waiting on a caller's uncommitted row lock.
  A proper post-commit hook/outbox would remove the workaround.
- **Unified first-purchase policy service.** The rules are spread across the
  paid predicate, `register_first_purchase`, notifications, UI promises and
  backfill. A single domain service should own the definition for daily, short
  purchases, gifts, coupons, trial conversions and refunds.
- **Refund/chargeback semantics.** A completed `SUBSCRIPTION_PAYMENT` that was
  later refunded/charged back still counts as "already paid". Decide whether the
  first-purchase marker should ignore refunded purchases.
- **Cabinet/landing preparation.** If the cabinet or landing surfaces are enabled,
  re-check the cross-session lock ordering (`campaign_service.attribute_campaign`
  holds the user row lock while calling registration) and run the PG concurrency
  suite with those entry points covered.

## Product decisions

- **"Оплатили" counter.** It now excludes daily-only payers because a daily
  purchase no longer writes a first-purchase marker. Decide whether the metric
  should count daily subscribers; if yes, add a separate paid metric.
- **Short non-daily first purchase.** A 14-day (below `MIN_PERIOD_DAYS`) purchase
  currently writes the marker and consumes the bonus, while paying no days. Keep
  or change?
- **Backfill run and historical markers.** Decide whether to run the backfill on
  the current database and spot-check users whose only historical payment is a
  daily/add-on/refunded row.

## Operations

- Run `make test-postgres` in CI/staging; the new concurrent marker test is
  PG-only and skipped locally without `TEST_DATABASE_URL`/Docker.
- Apply `alembic upgrade head` (0135 + 0136) in a maintenance window. Migration
  0136 creates a non-`CONCURRENTLY` unique index; on a large `referral_earnings`
  table it can block writes briefly.
- One-time SQL checks: historical countries add-ons, daily-only payers,
  duplicate first-purchase markers.
- Post-deploy monitoring: `IntegrityError` on marker writes, `lock_nowait`
  skips, `daily_not_counted` volume, and the first-purchase reward rate.
- Known pre-existing full-suite failures (not referral): `panel_sync` expiry /
  no-bypass, three `panel_deletion` cases, `SIGHUP` on Windows, and the
  tariff-switch Windows path-separator test.

## Accepted / won't fix now

- Non-`CONCURRENTLY` unique index (plan-mandated, mirrors 0135); use a
  maintenance window.
- Hardcoded Russian daily/add-on descriptions; documented as a known limitation
  until the transaction-type work lands.
- `confirm_instant_switch` in the bot does not call the first-purchase hook;
  pre-existing behavior, outside the P0/P1 scope.
- `COUNT(DISTINCT)` in the paid-referrals count is redundant under the new unique
  index; removed in the quick-win batch.
