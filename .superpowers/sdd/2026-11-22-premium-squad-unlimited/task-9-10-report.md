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
