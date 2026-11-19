# Referral Purchase Review Fixes Implementation Plan

> **For agentic workers:** Use test-driven development. The implementer writes and runs tests; the parent agent reviews the diff personally.

**Goal:** Fix three confirmed purchase and trial carry-over defects in the current uncommitted implementation.

**Architecture:** Commit a normal Telegram tariff purchase/renewal as one unit. Refresh writers' inputs under the subscription lock. Track the referral tail explicitly for new trials, leaving legacy rows untouched.

**Tech Stack:** Python, SQLAlchemy async, Alembic, pytest.

**Spec:** `docs/superpowers/specs/2026-11-18-referral-purchase-review-fixes-design.md`

## Global Constraints

- Work only in the provided isolated worktree. Its uncommitted baseline is deliberate; do not undo or commit it.
- One ordinary tariff, free trial, `days_only`. No deploy, no remote APIs or DB, no `.env`, no commit, no additional subagents.
- Project rules require one changelog entry before reporting done; parent writes it at the original repo's `../claude-changes.md` after review.
- Use the original workspace's installed Python via its absolute path with worktree as cwd. Use `PYTHONDONTWRITEBYTECODE=1` and `pytest -p no:cacheprovider`.

## Review Focus

- Debt prevention: failed extension after debit/flush, including promo balance fields; successful purchase journals once.
- Atomicity: any incidental commit inside the shared CRUD, including deactivation; keep panel interactions and purchase events post-commit.
- Stale state: two sessions, one same-tariff purchase after another commits ten days.
- Reward provenance: only bonus and referral days carry; real remaining time caps what carries.
- Legacy and expiry: nullable new state, no backfill, expired days don't come back after free extension.

### Task 1: Complete the purchase and carry-over fixes

- [ ] Add real async session regression tests before implementation. For purchase/renewal use `tests/handlers/test_purchase_refunds.py` or a new sibling; emulate a failing purchase between debit and final commit and check persisted balance, promo, term and transaction from a fresh session. Cover a successful path with one durable `SUBSCRIPTION_PAYMENT`.
- [ ] In `tests/crud/test_referral_trial_carry_over.py`, reproduce purchase of 30 days after another session already committed +10 to a `3 base + 3 referral` trial: expect 43, not 33. Reproduce new free trial +7 non-referral days + a 30-day same-tariff purchase: expect 30 without a referral, 33 with a 3-day referral bonus, not 37/40. Cover real expiry and legacy null state.
- [ ] Run new tests against the baseline and record the expected failures.
- [ ] In `app/handlers/subscription/tariff_purchase.py`, make ordinary fixed-period confirm and confirm renewal atomic; check the existing post-rollback bug in adjacent paths previously edited (admin/classic) and give them safe ID-based compensation where not making their charge atomic. Check that the default CRUD deactivation cannot commit when `extend_subscription`/`create_paid_subscription` is called with `commit=False`; maintain post-commit side effects that still matter. Do not touch daily or paid-trial handlers.
- [ ] In `app/database/crud/subscription.py`, refresh term and trial state under lock without discarding deliberate same-transaction changes. In `app/database/models.py` and a new migration after `0137`, use nullable explicit source accounting, e.g. `trial_referral_seconds`; keep legacy `NULL` behavior. Initialize new free trials even when the bonus is zero. Capture the applied bonus in activation rather than guessing from the full duration, and update tracked referral state in the same transaction as `add_reward_days`. Make future non-referral extensions preserve only the still-live referral amount (zero if already expired); ensure copies/replacement do not inherit old accounting for a new trial. Keep actual transfer bounded by the live term. Preview uses the same helper.
- [ ] Rerun targeted tests, run the project's suite if feasible, `git diff --check`, and confirm a single Alembic head. Report exact commands/outcomes, integration risks, and the file list, without committing.
