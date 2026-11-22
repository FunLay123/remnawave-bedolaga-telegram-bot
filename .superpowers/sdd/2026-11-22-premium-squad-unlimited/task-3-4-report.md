# Tasks 3–4 implementation report

## Scope

Implemented the coupled unlimited premium-state behavior from the Task 3 and
Task 4 briefs. Cabinet routes were not modified. Existing unrelated changes in
`tests/test_rich_menu_pins.py` and `tests/test_trial_disabled_menu_gating.py`
were preserved.

## RED evidence

After adding the specified regression tests, the focused command was run before
the production implementation:

```text
uv run pytest tests/services/test_premium_traffic_service.py tests/crud/test_drop_orphan_premium_states.py -q
```

The first attempt was blocked during collection because the environment had
lost `tzdata` and settings rejected `TIMEZONE=UTC`. After `uv sync --dev`, the
environment was repaired for the run with `tzdata`; the expected RED run then
reported **7 failed, 92 passed**. The failures were the new unlimited tests:

- unlimited usage was still limited;
- unlimited usage still reached the warning/limit decision path;
- granting unlimited did not restore a limited state;
- closed unlimited state was restored;
- orphaned unlimited rows were deleted;
- tariff-switch orphaned unlimited rows were deleted.

## Implementation

- Added `unlimited_at` to the worker `_Orphan` snapshot and propagated it from
  persisted state.
- Made orphan cleanup retain unlimited rows, clear `is_limited` when needed,
  restore the panel squad through the existing nested-savepoint and panel
  failure contract, and commit only after successful application.
- Split tariff-switch orphan states into `closed`, `unlimited`, and deletable
  sets; unlimited rows are retained and unlocked.
- Excluded unlimited states from the 90% warning path.
- Extended the test `_state` double with `unlimited_at` and matching
  `is_exhausted` precedence (`closed_at` first, then unlimited).

## GREEN evidence

Focused verification:

```text
uv run pytest tests/services/test_premium_traffic_service.py tests/crud/test_drop_orphan_premium_states.py -q
99 passed, 35 warnings in 6.06s
```

The warnings are existing SQLAlchemy/Pydantic deprecations and do not fail the
tests.

The requested full-suite command was also attempted:

```text
uv run pytest -q
```

It could not complete collection on this Windows environment because
`tests/test_pg_upgrade_prompt.py` imports POSIX-only `termios` through
`pty`:

```text
ModuleNotFoundError: No module named 'termios'
```

## Commit

`d90f48978 feat: preserve unlimited premium states across tariff changes`

## Concerns

- Full-suite status is unavailable on Windows due to the pre-existing
  platform-specific `termios` collection error; the focused Task 3–4 suite is
  fully green.
- Test output still contains the repository’s existing deprecation warnings.
