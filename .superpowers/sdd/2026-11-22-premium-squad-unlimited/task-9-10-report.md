# Tasks 9–10 RED/GREEN report

## Scope

Implemented premium top-up rejection and documentation for unlimited premium squads. Cabinet admin routes were not modified. Existing unrelated changes in `tests/test_rich_menu_pins.py` and `tests/test_trial_disabled_menu_gating.py` were preserved.

## RED

Added three regression tests before production changes:

- `test_quote_refuses_unlimited_squad`
- `test_apply_refuses_unlimited_set_after_the_quote`
- `test_unlimited_squad_is_not_offered`

Command:

```text
uv run pytest tests/services/test_premium_traffic_purchase.py tests/handlers/test_premium_topup_flow.py -q
```

Result: **3 failed, 56 passed**. Each failure demonstrated the missing behavior: quote/apply did not reject `unlimited_at`, and the purchasable list still returned the unlimited squad.

## GREEN

Implemented:

- `_ensure_squad_is_not_unlimited(state)` in `app/services/premium_traffic_purchase.py`.
- `PremiumTopupError('squad_unlimited', 'На этом сервере уже действует безлимит, докупка не нужна')` in both quote and locked apply paths.
- Filtering of `unlimited_at` states from `_get_purchasable_premium_squads`.
- Documentation of `unlimited_at`, the fifth `UNLIMITED` state, `closed_at` priority, `♾️` display, proxy `total=0`, panel-wide-limit caveat, and top-up rejection.
- External `../claude-changes.md` changelog entry as specified.

Focused command:

```text
uv run pytest tests/services/test_premium_traffic_purchase.py tests/handlers/test_premium_topup_flow.py -q
```

Result: **59 passed** (17 existing warnings).

Relevant premium suite:

```text
uv run pytest tests/services/test_premium_traffic_purchase.py tests/handlers/test_premium_topup_flow.py tests/services/test_premium_traffic_service.py tests/crud/test_premium_traffic_state.py tests/crud/test_drop_orphan_premium_states.py tests/crud/test_premium_traffic_on_tariff_change.py tests/database/test_premium_traffic_schema_parity.py -q
```

Result: **200 passed** (45 existing warnings).

## Commit

Repository commit: `feat: refuse premium top-up for unlimited squads`

The external changelog is outside the repository and is not included in the commit.

## Round 1 fix

### Task 9 ordering regression

Added `TestApply.test_apply_prefers_unlimited_over_topup_limit_after_the_quote`.
The test creates a quote, then makes the locked state simultaneously unlimited
and over the top-up ceiling. Before the fix, `apply_premium_topup` checked
`_ensure_topup_fits` first and returned `topup_limit_reached`.

RED command:

```text
uv run pytest tests/services/test_premium_traffic_purchase.py::TestApply::test_apply_prefers_unlimited_over_topup_limit_after_the_quote -q
```

RED result: **1 failed**; expected `squad_unlimited`, received
`topup_limit_reached`.

Minimal fix: in the locked apply path, run `_ensure_squad_is_not_closed` and
then `_ensure_squad_is_not_unlimited` immediately after loading the state, before
`_ensure_topup_fits`. This preserves the binding `CLOSED -> UNLIMITED` precedence
for stale quotes.

GREEN command:

```text
uv run pytest tests/services/test_premium_traffic_purchase.py tests/handlers/test_premium_topup_flow.py -q
```

GREEN result: **60 passed, 16 warnings**.

### Task 10 documentation corrections

- Added the dedicated Russian section **«Безлимит на сквад»**.
- Documented the complete classification order, including `ACTIVE` and the
  `NO_LIMIT` precedence: `CLOSED -> UNLIMITED -> NO_LIMIT ->
  EXHAUSTED/REOPENED_PENDING -> ACTIVE`.
- Corrected proxy semantics: if any included premium squad is unlimited, the
  entire `subscription-userinfo` result has `total=0`, while `download` remains
  the actual aggregate usage across included squads.
- No changes were made to the external changelog; the previously noted
  docs-only-commit process item remains deferred because this fix also contains
  the required production ordering change and regression test.
