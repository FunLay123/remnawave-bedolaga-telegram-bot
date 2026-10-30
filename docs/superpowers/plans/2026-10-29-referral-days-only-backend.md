# Referral Days-Only Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the days-only referral program backend: +3 trial days for invited users, exactly-once +10 days for both sides on the first paid subscription purchase of 30+ days, no money/levels/withdrawal, and no schema migration.

**Architecture:** A durable marker row in `referral_earnings` records the first paid purchase per referee. A dedicated service locks users and subscriptions, writes the marker and both reward rows, and adds days in one transaction. Existing subscription `extend_subscription` is not reused for rewards because it resets traffic, kills trials, and performs housekeeping; a dedicated `add_reward_days` helper is used instead.

**Tech Stack:** Python 3.12+, aiogram 3, SQLAlchemy async, PostgreSQL in production, SQLite in tests, pytest, structlog.

**Spec:** `../referral-days-only-design.md`. Final decisions that override the spec where they differ: `skip` is the only referrer-without-subscription mode; the main-menu referral button is not `primary`; the no-subscription screen offers a trial CTA; notification buttons use icon `5314310000531766389`; trial activation shows the toast `Триал активирован` only when the callback can still be answered.

## Global Constraints

- No new DB columns, indexes, or Alembic migrations.
- Do not modify `User.has_made_first_topup` or `User.has_had_paid_subscription`.
- The only referrer-without-subscription behavior is `skip`: referee gets +10, referrer gets nothing.
- A paid purchase means `paid_amount_kopeks > 0`; free coupons, gifts, admin grants, and referral-created subscriptions are not purchases.
- The first paid purchase always writes the marker, even when its duration is below 30 days.
- +10 is granted only when the first paid purchase duration is at least 30 days.
- No money, percentage, level, or withdrawal rewards may be paid in days-only mode.
- Use Russian copy only; edit `ru.json` only when a new key is required.
- Keep repository line endings LF.
- Do not add AI co-authorship trailers to commits.
- First-release hook coverage: bot classic/tariff/simple/menu quick activation, cabinet/miniapp balance, Stars/YooKassa simple, and Lava/Platega/Cashera first charges. Auto-purchase and monitoring autopay are renewal paths and depend on the marker written by the initial purchase; if a deployment can produce a first purchase through either path, add a hook before enabling the program.

## Review Focus

- Provider checkout placeholders (`create_sbp_pending_subscription`) and `PENDING` rows must not look like a prior paid purchase. Covered by the marker design and tests in Tasks 3 and 4.
- A first 14-day purchase must consume the first-purchase marker; a later 30-day purchase must not receive +10. Covered by Task 4.
- A referrer without a subscription must not block the referee reward, and must not receive days. Covered by Task 4.
- Two concurrent first purchases for the same referee must produce exactly one marker and one pair of rewards. Covered by Task 4 lock tests; a PostgreSQL concurrency test is added in Task 8.
- A referred pending trial must keep its +3 bonus when activated late. Covered by Task 6.

---

### Task 1: Add days-only constants, reasons, and configuration

**Files:**
- Modify: `app/services/referral_reward_service.py`
- Modify: `app/config.py:458-500`
- Test: `tests/services/test_referral_days_only_contract.py`

**Interfaces:**
- Produces: `REASON_FIRST_PURCHASE`, `REASON_FIRST_PURCHASE_REFEREE`, `REASON_FIRST_PURCHASE_REGISTERED` constants.
- Produces: `REFEREE_DIRECTED_REASONS` including the two referee-directed reasons.
- Produces: `settings.is_referral_days_only_scheme() -> bool`.
- Produces: `settings.REFERRAL_TRIAL_BONUS_DAYS`, `settings.REFERRAL_FIRST_PURCHASE_BONUS_DAYS`, `settings.REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS`.

- [ ] **Step 1: Write the failing contract test**

```python
# tests/services/test_referral_days_only_contract.py
from app.config import settings
from app.services.referral_reward_service import (
    REFEREE_DIRECTED_REASONS,
    REASON_FIRST_PURCHASE,
    REASON_FIRST_PURCHASE_REFEREE,
    REASON_FIRST_PURCHASE_REGISTERED,
)


def test_first_purchase_reasons_have_the_required_direction():
    assert REASON_FIRST_PURCHASE == 'referral_first_purchase'
    assert REASON_FIRST_PURCHASE_REFEREE == 'referral_first_purchase_referee'
    assert REASON_FIRST_PURCHASE_REGISTERED == 'referral_first_purchase_registered'
    assert REASON_FIRST_PURCHASE_REFEREE in REFEREE_DIRECTED_REASONS
    assert REASON_FIRST_PURCHASE_REGISTERED in REFEREE_DIRECTED_REASONS
    assert REASON_FIRST_PURCHASE not in REFEREE_DIRECTED_REASONS


def test_days_only_scheme_and_defaults(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    assert settings.is_referral_days_only_scheme()
    assert settings.REFERRAL_TRIAL_BONUS_DAYS == 3
    assert settings.REFERRAL_FIRST_PURCHASE_BONUS_DAYS == 10
    assert settings.REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS == 30

    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')
    assert not settings.is_referral_days_only_scheme()
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `pytest tests/services/test_referral_days_only_contract.py -v`
Expected: FAIL with `ImportError: cannot import name 'REASON_FIRST_PURCHASE'`.

- [ ] **Step 3: Add reasons and exclusions**

In `app/services/referral_reward_service.py` after the existing reason constants:

```python
REASON_FIRST_PURCHASE = 'referral_first_purchase'
REASON_FIRST_PURCHASE_REFEREE = 'referral_first_purchase_referee'
REASON_FIRST_PURCHASE_REGISTERED = 'referral_first_purchase_registered'

REFEREE_DIRECTED_REASONS = frozenset(
    {
        REASON_DAYS_REFEREE,
        REASON_FIRST_PURCHASE_REFEREE,
        REASON_FIRST_PURCHASE_REGISTERED,
    }
)
```

Keep `is_referee_directed()` unchanged; it reads the set.

- [ ] **Step 4: Add config fields and the scheme predicate**

In `app/config.py` near the existing referral settings:

```python
REFERRAL_REWARD_SCHEME: str = 'legacy'
REFERRAL_TRIAL_BONUS_DAYS: int = 3
REFERRAL_FIRST_PURCHASE_BONUS_DAYS: int = 10
REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS: int = 30
```

Add near `is_referral_levels_scheme()`:

```python
def is_referral_days_only_scheme(self) -> bool:
    """Дни за триал и за первую покупку; деньги/уровни/вывод отключены."""
    return str(self.REFERRAL_REWARD_SCHEME or '').strip().lower() == 'days_only'
```

Add the three values to `get_referral_settings()` so the mini app and cabinet do not guess:

```python
'trial_bonus_days': self.REFERRAL_TRIAL_BONUS_DAYS,
'first_purchase_bonus_days': self.REFERRAL_FIRST_PURCHASE_BONUS_DAYS,
'first_purchase_min_period_days': self.REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS,
```

- [ ] **Step 5: Run the test and verify it passes**

Run: `pytest tests/services/test_referral_days_only_contract.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/referral_reward_service.py app/config.py tests/services/test_referral_days_only_contract.py
git commit -m "referral: add days-only reasons and settings"
```

---

### Task 2: Add a reward-days helper that does not reset traffic or kill trials

**Files:**
- Modify: `app/database/crud/subscription.py:1194` (add helper before `extend_subscription`)
- Test: `tests/crud/test_add_reward_days.py`

**Interfaces:**
- Produces: `async def add_reward_days(db: AsyncSession, subscription: Subscription, days: int) -> Subscription | None`
- Returns the freshly locked subscription on success; returns `None` for `PENDING`/`DISABLED` or unknown status.
- Does not commit; the caller commits.
- Does not reset `traffic_used_gb`, does not deactivate other trials, does not run traffic housekeeping, does not change `is_trial`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/crud/test_add_reward_days.py
from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.database.crud.subscription import add_reward_days
from app.database.models import Base, Subscription, SubscriptionStatus, User
from tests.fixtures.sqlite_memory import memory_session

TABLES = list(Base.metadata.sorted_tables)


def _user(uid: int) -> User:
    return User(id=uid, telegram_id=1000 + uid, first_name=f'U{uid}', language='ru', balance_kopeks=0)


def _sub(
    sub_id: int,
    user_id: int,
    *,
    status: str,
    days: int,
    trial: bool = False,
    traffic_used: float = 0.0,
) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        status=status,
        is_trial=trial,
        start_date=now - timedelta(days=30),
        end_date=now + timedelta(days=days),
        traffic_limit_gb=100,
        traffic_used_gb=traffic_used,
        device_limit=1,
    )


@pytest.mark.asyncio
async def test_active_subscription_gets_days_without_traffic_reset(monkeypatch):
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_sub(10, 1, status=SubscriptionStatus.ACTIVE.value, days=10, traffic_used=42.0))
        await db.commit()

        sub = await db.get(Subscription, 10)
        before = sub.end_date
        got = await add_reward_days(db, sub, 10)

        assert got is not None
        assert got.end_date >= before + timedelta(days=9, hours=23)
        assert got.traffic_used_gb == 42.0
        assert got.is_trial is False
        assert got.status == SubscriptionStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_expired_trial_reactivates_as_trial(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_sub(10, 1, status=SubscriptionStatus.EXPIRED.value, days=-5, trial=True))
        await db.commit()

        sub = await db.get(Subscription, 10)
        got = await add_reward_days(db, sub, 10)

        assert got is not None
        assert got.status == SubscriptionStatus.TRIAL.value
        assert got.is_trial is True
        assert got.end_date > datetime.now(UTC) + timedelta(days=9)


@pytest.mark.asyncio
async def test_limited_subscription_keeps_limited_status(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_sub(10, 1, status=SubscriptionStatus.LIMITED.value, days=5))
        await db.commit()

        sub = await db.get(Subscription, 10)
        got = await add_reward_days(db, sub, 10)

        assert got is not None
        assert got.status == SubscriptionStatus.LIMITED.value
        assert got.end_date > datetime.now(UTC) + timedelta(days=14)


@pytest.mark.asyncio
async def test_pending_and_disabled_are_not_touched(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_sub(10, 1, status=SubscriptionStatus.PENDING.value, days=5))
        db.add(_sub(11, 1, status=SubscriptionStatus.DISABLED.value, days=5))
        await db.commit()

        assert await add_reward_days(db, await db.get(Subscription, 10), 10) is None
        assert await add_reward_days(db, await db.get(Subscription, 11), 10) is None
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `pytest tests/crud/test_add_reward_days.py -v`
Expected: FAIL with `ImportError: cannot import name 'add_reward_days'`.

- [ ] **Step 3: Implement `add_reward_days`**

Place before `extend_subscription` in `app/database/crud/subscription.py`:

```python
async def add_reward_days(
    db: AsyncSession,
    subscription: Subscription,
    days: int,
) -> Subscription | None:
    """Добавить бонусные дни, не применяя платёжные побочки.

    В отличие от ``extend_subscription`` этот helper не сбрасывает трафик, не
    выключает другие триалы и не делает housekeeping докупок. Он только
    продлевает срок, поднимает истёкшую подписку и чистит напоминания.
    """
    if days <= 0:
        return None

    result = await db.execute(
        select(Subscription)
        .where(Subscription.id == subscription.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked = result.scalar_one_or_none()
    if locked is None:
        return None

    status = (locked.status or '').lower()
    if status in (SubscriptionStatus.PENDING.value, SubscriptionStatus.DISABLED.value):
        return None
    if status not in (
        SubscriptionStatus.ACTIVE.value,
        SubscriptionStatus.TRIAL.value,
        SubscriptionStatus.LIMITED.value,
        SubscriptionStatus.EXPIRED.value,
    ):
        return None

    from app.services.grace_access_echo import undo_grace_overlay_echo

    await undo_grace_overlay_echo(db, locked)

    now = datetime.now(UTC)
    base = locked.end_date if locked.end_date and locked.end_date > now else now
    locked.end_date = base + timedelta(days=days)

    if status == SubscriptionStatus.EXPIRED.value:
        locked.status = SubscriptionStatus.TRIAL.value if locked.is_trial else SubscriptionStatus.ACTIVE.value

    locked.updated_at = now
    await clear_notifications(db, locked.id, commit=False)
    await db.flush()
    return locked
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `pytest tests/crud/test_add_reward_days.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/database/crud/subscription.py tests/crud/test_add_reward_days.py
git commit -m "referral: add reward days helper"
```

---

### Task 3: Add the first-purchase marker, backfill, and late-attach protection

**Files:**
- Create: `app/services/referral_purchase_service.py`
- Modify: `app/services/referral_service.py:184-360` (`attach_referrer_if_missing`)
- Test: `tests/services/test_referral_first_purchase_marker.py`

**Interfaces:**
- Produces: `async def has_first_purchase_marker(db, referee_id: int) -> bool`
- Produces: `async def ensure_marker_for_already_paid_referral(db, user_id: int) -> bool`
- Produces: `async def backfill_first_purchase_markers(db) -> int`
- `ensure_marker...` commits; backfill commits in batches.

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/test_referral_first_purchase_marker.py
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database.models import Base, ReferralEarning, Subscription, SubscriptionStatus, User
from app.services.referral_purchase_service import (
    backfill_first_purchase_markers,
    ensure_marker_for_already_paid_referral,
    has_first_purchase_marker,
)
from app.services.referral_reward_service import REASON_FIRST_PURCHASE_REGISTERED
from tests.fixtures.sqlite_memory import memory_session

TABLES = list(Base.metadata.sorted_tables)


def _user(uid: int, referred_by_id: int | None = None) -> User:
    return User(
        id=uid,
        telegram_id=1000 + uid,
        first_name=f'U{uid}',
        language='ru',
        balance_kopeks=0,
        referred_by_id=referred_by_id,
    )


def _paid_sub(sub_id: int, user_id: int) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=30),
        traffic_limit_gb=100,
        device_limit=1,
    )


@pytest.mark.asyncio
async def test_marker_is_written_once_for_a_paid_referral(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_sub(10, 2)])
        await db.commit()

        assert await has_first_purchase_marker(db, 2) is False
        assert await ensure_marker_for_already_paid_referral(db, 2) is True
        assert await has_first_purchase_marker(db, 2) is True
        assert await ensure_marker_for_already_paid_referral(db, 2) is False

        rows = (
            (
                await db.execute(
                    select(ReferralEarning).where(
                        ReferralEarning.user_id == 2,
                        ReferralEarning.reason == REASON_FIRST_PURCHASE_REGISTERED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].days_granted == 0
        assert rows[0].amount_kopeks == 0


@pytest.mark.asyncio
async def test_backfill_is_idempotent(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _user(3, referred_by_id=1),
                _paid_sub(10, 2),
            ]
        )
        await db.commit()

        assert await backfill_first_purchase_markers(db) == 1
        assert await backfill_first_purchase_markers(db) == 0
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `pytest tests/services/test_referral_first_purchase_marker.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the marker helpers**

Create `app/services/referral_purchase_service.py` with this first slice:

```python
from __future__ import annotations

import structlog
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ReferralEarning, ReferralRewardType, Subscription, User
from app.services.referral_reward_service import REASON_FIRST_PURCHASE_REGISTERED

logger = structlog.get_logger(__name__)


async def has_first_purchase_marker(db: AsyncSession, referee_id: int) -> bool:
    result = await db.execute(
        select(ReferralEarning.id)
        .where(
            ReferralEarning.user_id == referee_id,
            ReferralEarning.reason == REASON_FIRST_PURCHASE_REGISTERED,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def ensure_marker_for_already_paid_referral(db: AsyncSession, user_id: int) -> bool:
    user = await db.get(User, user_id)
    if user is None or user.referred_by_id is None:
        return False
    if await has_first_purchase_marker(db, user_id):
        return False

    paid_exists = await db.execute(
        select(Subscription.id)
        .where(
            Subscription.user_id == user_id,
            Subscription.is_trial.is_(False),
        )
        .limit(1)
    )
    if paid_exists.scalar_one_or_none() is None:
        return False

    db.add(
        ReferralEarning(
            user_id=user_id,
            referral_id=user.referred_by_id,
            amount_kopeks=0,
            reason=REASON_FIRST_PURCHASE_REGISTERED,
            reward_type=ReferralRewardType.DAYS.value,
            level=1,
            days_granted=0,
        )
    )
    await db.commit()
    return True


async def backfill_first_purchase_markers(db: AsyncSession) -> int:
    paid_referrals = await db.execute(
        select(User.id, User.referred_by_id).where(
            User.referred_by_id.isnot(None),
            exists(
                select(Subscription.id).where(
                    Subscription.user_id == User.id,
                    Subscription.is_trial.is_(False),
                )
            ),
        )
    )

    created = 0
    for user_id, referrer_id in paid_referrals.all():
        if referrer_id is None or referrer_id == user_id:
            continue
        if await has_first_purchase_marker(db, user_id):
            continue
        db.add(
            ReferralEarning(
                user_id=user_id,
                referral_id=referrer_id,
                amount_kopeks=0,
                reason=REASON_FIRST_PURCHASE_REGISTERED,
                reward_type=ReferralRewardType.DAYS.value,
                level=1,
                days_granted=0,
            )
        )
        created += 1

    await db.commit()
    if created:
        logger.info('Referral first-purchase markers backfilled', count=created)
    return created
```

- [ ] **Step 4: Hook late attachment**

At the end of `attach_referrer_if_missing` in `app/services/referral_service.py`, after the successful attachment and refresh, add:

```python
    try:
        from app.services.referral_purchase_service import ensure_marker_for_already_paid_referral

        await ensure_marker_for_already_paid_referral(db, user.id)
    except Exception as error:
        logger.error(
            'Failed to register first-purchase marker after referral attach',
            user_id=user.id,
            referrer_id=referrer.id,
            error=str(error),
        )
```

This must run after the conditional UPDATE and before/after `process_referral_registration`; either order is safe because the marker only reads subscriptions.

- [ ] **Step 5: Run tests**

Run: `pytest tests/services/test_referral_first_purchase_marker.py tests/services/test_attach_referrer_if_missing.py -v`
Expected: PASS. The existing attach tests must stay green.

- [ ] **Step 6: Commit**

```bash
git add app/services/referral_purchase_service.py app/services/referral_service.py tests/services/test_referral_first_purchase_marker.py
git commit -m "referral: add first purchase marker and backfill"
```

---

### Task 4: Implement the first-purchase reward service

**Files:**
- Modify: `app/services/referral_purchase_service.py`
- Test: `tests/services/test_referral_first_purchase_service.py`

**Interfaces:**
- Produces: `async def register_first_purchase(db, *, referee_id: int, subscription_id: int, duration_days: int, paid_amount_kopeks: int, source: str) -> FirstPurchaseResult`
- Produces: `async def register_first_purchase_after_purchase(**kwargs) -> FirstPurchaseResult`
- `FirstPurchaseResult` fields: `status`, `referee_days`, `referrer_days`, `referee_subscription_id`, `referrer_subscription_id`.
- Statuses: `skipped`, `not_referred`, `already_registered`, `registered`, `rewarded`, `error`.

- [ ] **Step 1: Write the failing service tests**

```python
# tests/services/test_referral_first_purchase_service.py
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, ReferralEarning, Subscription, SubscriptionStatus, User
from app.services.referral_purchase_service import register_first_purchase
from app.services.referral_reward_service import (
    REASON_FIRST_PURCHASE,
    REASON_FIRST_PURCHASE_REFEREE,
    REASON_FIRST_PURCHASE_REGISTERED,
)
from tests.fixtures.sqlite_memory import memory_session

TABLES = list(Base.metadata.sorted_tables)


def _user(uid: int, referred_by_id: int | None = None) -> User:
    return User(
        id=uid,
        telegram_id=1000 + uid,
        first_name=f'U{uid}',
        language='ru',
        balance_kopeks=0,
        referred_by_id=referred_by_id,
    )


def _sub(sub_id: int, user_id: int, *, trial: bool = False, days: int = 30) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=trial,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=days),
        traffic_limit_gb=100,
        device_limit=1,
    )


@pytest.mark.asyncio
async def test_first_30_day_purchase_rewards_both_sides(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2), _sub(11, 1)])
        await db.commit()

        result = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=30,
            paid_amount_kopeks=10_000,
            source='test',
        )

        assert result.status == 'rewarded'
        assert result.referee_days == 10
        assert result.referrer_days == 10

        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        by_reason = {row.reason: row for row in rows}
        assert by_reason[REASON_FIRST_PURCHASE_REGISTERED].days_granted == 0
        assert by_reason[REASON_FIRST_PURCHASE].days_granted == 10
        assert by_reason[REASON_FIRST_PURCHASE_REFEREE].days_granted == 10

        referee_sub = await db.get(Subscription, 10)
        referrer_sub = await db.get(Subscription, 11)
        assert referee_sub.end_date > datetime.now(UTC) + timedelta(days=39)
        assert referrer_sub.end_date > datetime.now(UTC) + timedelta(days=39)


@pytest.mark.asyncio
async def test_first_14_day_purchase_consumes_marker_without_reward(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2), _sub(11, 1)])
        await db.commit()

        first = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=14,
            paid_amount_kopeks=5_000,
            source='test',
        )
        second = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=30,
            paid_amount_kopeks=10_000,
            source='test',
        )

        assert first.status == 'registered'
        assert first.referee_days == 0
        assert second.status == 'already_registered'
        assert second.referee_days == 0

        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert [row.reason for row in rows] == [REASON_FIRST_PURCHASE_REGISTERED]


@pytest.mark.asyncio
async def test_referrer_without_subscription_skips_only_referrer(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2)])
        await db.commit()

        result = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=30,
            paid_amount_kopeks=10_000,
            source='test',
        )

        assert result.status == 'rewarded'
        assert result.referee_days == 10
        assert result.referrer_days == 0
        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert REASON_FIRST_PURCHASE_REFEREE in {row.reason for row in rows}
        assert REASON_FIRST_PURCHASE not in {row.reason for row in rows}


@pytest.mark.asyncio
async def test_free_or_unreferred_purchase_does_not_write_marker(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _user(3), _sub(10, 2), _sub(11, 3)])
        await db.commit()

        free = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=30,
            paid_amount_kopeks=0,
            source='test',
        )
        unreferred = await register_first_purchase(
            db,
            referee_id=3,
            subscription_id=11,
            duration_days=30,
            paid_amount_kopeks=10_000,
            source='test',
        )

        assert free.status == 'skipped'
        assert unreferred.status == 'not_referred'
        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert rows == []
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `pytest tests/services/test_referral_first_purchase_service.py -v`
Expected: FAIL with `ImportError` or `AttributeError`.

- [ ] **Step 3: Implement the service**

Append to `app/services/referral_purchase_service.py`:

```python
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import add_reward_days
from app.database.models import Subscription, SubscriptionStatus, User
from app.services.referral_reward_service import (
    REASON_FIRST_PURCHASE,
    REASON_FIRST_PURCHASE_REFEREE,
    REASON_FIRST_PURCHASE_REGISTERED,
)

FirstPurchaseStatus = Literal['skipped', 'not_referred', 'already_registered', 'registered', 'rewarded', 'error']


@dataclass(frozen=True, slots=True)
class FirstPurchaseResult:
    status: FirstPurchaseStatus
    referee_days: int = 0
    referrer_days: int = 0
    referee_id: int | None = None
    referrer_id: int | None = None
    referee_subscription_id: int | None = None
    referrer_subscription_id: int | None = None


async def _lock_user(db: AsyncSession, user_id: int) -> User | None:
    result = await db.execute(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _lock_subscription(db: AsyncSession, subscription_id: int, user_id: int) -> Subscription | None:
    result = await db.execute(
        select(Subscription)
        .where(Subscription.id == subscription_id, Subscription.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _lock_referrer_target(db: AsyncSession, referrer_id: int) -> Subscription | None:
    ids = (
        (
            await db.execute(
                select(Subscription.id).where(Subscription.user_id == referrer_id).order_by(Subscription.id.asc())
            )
        )
        .scalars()
        .all()
    )

    locked: list[Subscription] = []
    for subscription_id in ids:
        subscription = (
            await db.execute(
                select(Subscription)
                .where(Subscription.id == subscription_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if subscription is not None:
            locked.append(subscription)

    allowed = {
        SubscriptionStatus.ACTIVE.value,
        SubscriptionStatus.TRIAL.value,
        SubscriptionStatus.LIMITED.value,
        SubscriptionStatus.EXPIRED.value,
    }
    candidates = [sub for sub in locked if (sub.status or '').lower() in allowed]
    if not candidates:
        return None
    candidates.sort(
        key=lambda sub: (
            bool(sub.is_trial),
            -(sub.end_date.timestamp() if sub.end_date else 0),
            sub.id,
        )
    )
    return candidates[0]


async def register_first_purchase(
    db: AsyncSession,
    *,
    referee_id: int,
    subscription_id: int,
    duration_days: int,
    paid_amount_kopeks: int,
    source: str,
) -> FirstPurchaseResult:
    if paid_amount_kopeks <= 0:
        return FirstPurchaseResult(status='skipped')
    if not settings.is_referral_program_enabled() or not settings.is_referral_days_only_scheme():
        return FirstPurchaseResult(status='skipped')

    referee = await _lock_user(db, referee_id)
    if referee is None or referee.referred_by_id is None:
        return FirstPurchaseResult(status='not_referred')

    referrer_id = referee.referred_by_id
    if referrer_id == referee_id:
        return FirstPurchaseResult(status='not_referred')

    for user_id in sorted({referee_id, referrer_id}):
        if await _lock_user(db, user_id) is None:
            return FirstPurchaseResult(status='skipped')

    if await has_first_purchase_marker(db, referee_id):
        return FirstPurchaseResult(
            status='already_registered',
            referee_id=referee_id,
            referrer_id=referrer_id,
        )

    referee_sub = await _lock_subscription(db, subscription_id, referee_id)
    if referee_sub is None or (referee_sub.status or '').lower() in {
        SubscriptionStatus.PENDING.value,
        SubscriptionStatus.DISABLED.value,
    }:
        return FirstPurchaseResult(status='skipped')

    db.add(
        ReferralEarning(
            user_id=referee_id,
            referral_id=referrer_id,
            amount_kopeks=0,
            reason=REASON_FIRST_PURCHASE_REGISTERED,
            reward_type=ReferralRewardType.DAYS.value,
            level=1,
            days_granted=0,
        )
    )

    if duration_days < settings.REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS:
        await db.commit()
        logger.info(
            'Referral first purchase registered below bonus period',
            referee_id=referee_id,
            referrer_id=referrer_id,
            duration_days=duration_days,
            source=source,
        )
        return FirstPurchaseResult(
            status='registered',
            referee_id=referee_id,
            referrer_id=referrer_id,
            referee_subscription_id=referee_sub.id,
        )

    bonus = settings.REFERRAL_FIRST_PURCHASE_BONUS_DAYS
    extended_referee = await add_reward_days(db, referee_sub, bonus)
    if extended_referee is None:
        await db.rollback()
        return FirstPurchaseResult(status='skipped')

    db.add(
        ReferralEarning(
            user_id=referee_id,
            referral_id=referrer_id,
            amount_kopeks=0,
            reason=REASON_FIRST_PURCHASE_REFEREE,
            reward_type=ReferralRewardType.DAYS.value,
            level=1,
            days_granted=bonus,
            tariff_id=extended_referee.tariff_id,
        )
    )

    referrer_sub = await _lock_referrer_target(db, referrer_id)
    extended_referrer = None
    referrer_days = 0
    if referrer_sub is not None:
        extended_referrer = await add_reward_days(db, referrer_sub, bonus)
        if extended_referrer is not None:
            referrer_days = bonus
            db.add(
                ReferralEarning(
                    user_id=referrer_id,
                    referral_id=referee_id,
                    amount_kopeks=0,
                    reason=REASON_FIRST_PURCHASE,
                    reward_type=ReferralRewardType.DAYS.value,
                    level=1,
                    days_granted=bonus,
                    tariff_id=extended_referrer.tariff_id,
                )
            )

    await db.commit()
    logger.info(
        'Referral first purchase rewarded',
        referee_id=referee_id,
        referrer_id=referrer_id,
        duration_days=duration_days,
        referee_days=bonus,
        referrer_days=referrer_days,
        source=source,
    )
    return FirstPurchaseResult(
        status='rewarded',
        referee_days=bonus,
        referrer_days=referrer_days,
        referee_id=referee_id,
        referrer_id=referrer_id,
        referee_subscription_id=extended_referee.id,
        referrer_subscription_id=extended_referrer.id if extended_referrer is not None else None,
    )
```

Add `ReferralRewardType` and `ReferralEarning` are already imported at the top from Task 3. Add `logger` already exists.

- [ ] **Step 4: Add the post-purchase wrapper**

Append:

```python
async def register_first_purchase_after_purchase(
    *,
    referee_id: int,
    subscription_id: int,
    duration_days: int,
    paid_amount_kopeks: int,
    source: str,
) -> FirstPurchaseResult:
    from app.database.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            result = await register_first_purchase(
                db,
                referee_id=referee_id,
                subscription_id=subscription_id,
                duration_days=duration_days,
                paid_amount_kopeks=paid_amount_kopeks,
                source=source,
            )
    except Exception as error:
        logger.error(
            'Failed to process referral first purchase',
            referee_id=referee_id,
            subscription_id=subscription_id,
            source=source,
            error=str(error),
            exc_info=True,
        )
        return FirstPurchaseResult(status='error')
    return result
```

The wrapper deliberately swallows errors: a referral bonus failure must not turn a successful purchase into an error for the user.

- [ ] **Step 5: Add post-commit sync, Lava shift, and notifications**

Extend the wrapper with `bot=None` and call the post-commit helper after a successful result:

```python
async def register_first_purchase_after_purchase(
    *,
    referee_id: int,
    subscription_id: int,
    duration_days: int,
    paid_amount_kopeks: int,
    source: str,
    bot=None,
) -> FirstPurchaseResult:
    from app.database.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            result = await register_first_purchase(
                db,
                referee_id=referee_id,
                subscription_id=subscription_id,
                duration_days=duration_days,
                paid_amount_kopeks=paid_amount_kopeks,
                source=source,
            )
    except Exception as error:
        logger.error(
            'Failed to process referral first purchase',
            referee_id=referee_id,
            subscription_id=subscription_id,
            source=source,
            error=str(error),
            exc_info=True,
        )
        return FirstPurchaseResult(status='error')

    if result.status == 'rewarded':
        await _post_commit_effects(result, referee_id, bot=bot)
    return result
```

Add `_post_commit_effects` below it:

```python
async def _post_commit_effects(
    result: FirstPurchaseResult,
    referee_id: int,
    *,
    bot=None,
) -> None:
    from app.database.database import AsyncSessionLocal
    from app.database.models import Subscription
    from app.services.payment.lava import shift_lava_next_charge_after_manual_extension
    from app.services.remnawave_retry_queue import remnawave_retry_queue
    from app.services.subscription_service import SubscriptionService

    subscription_ids = {
        subscription_id
        for subscription_id in (
            result.referee_subscription_id,
            result.referrer_subscription_id,
        )
        if subscription_id is not None
    }

    for subscription_id in sorted(subscription_ids):
        async with AsyncSessionLocal() as db:
            subscription = await db.get(Subscription, subscription_id)
            if subscription is None:
                continue
            try:
                await SubscriptionService().sync_remnawave_user(db, subscription)
            except Exception as error:
                logger.error(
                    'Failed to sync referral reward subscription',
                    subscription_id=subscription_id,
                    error=str(error),
                )
                remnawave_retry_queue.enqueue(
                    subscription_id=subscription_id,
                    user_id=subscription.user_id,
                    action='update',
                )
            try:
                await shift_lava_next_charge_after_manual_extension(
                    db,
                    subscription_id,
                    settings.REFERRAL_FIRST_PURCHASE_BONUS_DAYS,
                )
            except Exception as error:
                logger.warning(
                    'Failed to shift Lava charge after referral reward',
                    subscription_id=subscription_id,
                    error=str(error),
                )

    if bot is None:
        return

    from app.database.crud.user import get_user_by_id
    from app.localization.texts import get_texts

    async with AsyncSessionLocal() as db:
        referee = await get_user_by_id(db, referee_id)
        if referee is None or not referee.telegram_id:
            return
        texts = get_texts(referee.language)
        if result.referee_days:
            try:
                await bot.send_message(
                    chat_id=referee.telegram_id,
                    text=(
                        '<tg-emoji emoji-id="5235695112419303615">🎁</tg-emoji> '
                        '<b>Вам +10 дней</b>\n\n'
                        'За первую покупку по приглашению.'
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=texts.t('MY_SUBSCRIPTION_BUTTON', 'Моя подписка'),
                                    callback_data='menu_subscription',
                                    icon_custom_emoji_id='5314310000531766389',
                                    style='primary',
                                )
                            ]
                        ]
                    ),
                )
            except Exception as error:
                logger.warning('Failed to notify referee about referral days', error=str(error))
```

For the referrer notification, the service result does not currently carry `referrer_id`; add `referrer_id: int | None = None` to `FirstPurchaseResult` and set it in `register_first_purchase`. Then send a second message when `result.referrer_days` and `result.referrer_id` are set:

```python
        if result.referrer_days and result.referrer_id:
            referrer = await get_user_by_id(db, result.referrer_id)
            if referrer and referrer.telegram_id:
                try:
                    await bot.send_message(
                        chat_id=referrer.telegram_id,
                        text=(
                            '<tg-emoji emoji-id="5335020345717973061">🎉</tg-emoji> '
                            '<b>Друг купил подписку</b>\n\n'
                            'Вам +10 дней к текущей подписке.'
                        ),
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text=texts.t('MAIN_MENU_BUTTON', 'Главное меню'),
                                        callback_data='back_to_menu',
                                        icon_custom_emoji_id='5314310000531766389',
                                        style='primary',
                                    )
                                ]
                            ]
                        ),
                    )
                except Exception as error:
                    logger.warning('Failed to notify referrer about referral days', error=str(error))
```

Add `referrer_id` to the dataclass and populate it in every `FirstPurchaseResult` returned by `register_first_purchase`; for `skipped`/`not_referred`/`already_registered` it may stay `None`.

- [ ] **Step 6: Add a post-commit test**

```python
# tests/services/test_referral_post_commit.py
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.payment.lava as lava_module
import app.services.remnawave_retry_queue as retry_module
import app.services.subscription_service as subscription_module
from app.services.referral_purchase_service import FirstPurchaseResult, _post_commit_effects


@pytest.mark.asyncio
async def test_post_commit_syncs_both_subscriptions(monkeypatch):
    subscriptions = {
        10: SimpleNamespace(id=10, user_id=2),
        11: SimpleNamespace(id=11, user_id=1),
    }
    fake_db = SimpleNamespace()
    fake_db.get = AsyncMock(side_effect=lambda model, subscription_id: subscriptions[subscription_id])

    @asynccontextmanager
    async def fake_session():
        yield fake_db

    sync = AsyncMock()
    shift = AsyncMock()
    enqueue = MagicMock()

    monkeypatch.setattr('app.database.database.AsyncSessionLocal', fake_session)
    monkeypatch.setattr(
        subscription_module,
        'SubscriptionService',
        lambda: SimpleNamespace(sync_remnawave_user=sync),
    )
    monkeypatch.setattr(lava_module, 'shift_lava_next_charge_after_manual_extension', shift)
    monkeypatch.setattr(retry_module.remnawave_retry_queue, 'enqueue', enqueue)

    result = FirstPurchaseResult(
        status='rewarded',
        referee_days=10,
        referrer_days=10,
        referee_subscription_id=10,
        referrer_subscription_id=11,
    )

    await _post_commit_effects(result, referee_id=2)

    assert sync.await_count == 2
    assert shift.await_count == 2
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_post_commit_enqueues_sync_failure_without_raising(monkeypatch):
    subscriptions = {10: SimpleNamespace(id=10, user_id=2)}
    fake_db = SimpleNamespace()
    fake_db.get = AsyncMock(side_effect=lambda model, subscription_id: subscriptions[subscription_id])

    @asynccontextmanager
    async def fake_session():
        yield fake_db

    sync = AsyncMock(side_effect=RuntimeError('panel down'))
    enqueue = MagicMock()

    monkeypatch.setattr('app.database.database.AsyncSessionLocal', fake_session)
    monkeypatch.setattr(
        subscription_module,
        'SubscriptionService',
        lambda: SimpleNamespace(sync_remnawave_user=sync),
    )
    monkeypatch.setattr(lava_module, 'shift_lava_next_charge_after_manual_extension', AsyncMock())
    monkeypatch.setattr(retry_module.remnawave_retry_queue, 'enqueue', enqueue)

    result = FirstPurchaseResult(
        status='rewarded',
        referee_days=10,
        referrer_days=0,
        referee_subscription_id=10,
    )

    await _post_commit_effects(result, referee_id=2)

    enqueue.assert_called_once()
```

The test uses `AsyncSessionLocal` patched at the module where `_post_commit_effects` imports it. If a future refactor moves the import, update the patch target to the new import site.

- [ ] **Step 7: Run the tests**

Run: `pytest tests/services/test_referral_first_purchase_service.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/services/referral_purchase_service.py tests/services/test_referral_first_purchase_service.py
git commit -m "referral: add first purchase reward service"
```

---

### Task 5: Wire the first-purchase service into purchase flows

**Files:**
- Modify: `app/handlers/subscription/purchase.py` (`confirm_purchase`, `handle_simple_subscription_purchase`)
- Modify: `app/handlers/subscription/tariff_purchase.py` (`confirm_tariff_purchase`, `confirm_daily_tariff_purchase`)
- Modify: `app/handlers/simple_subscription.py` (both simple purchase paths)
- Modify: `app/services/subscription_purchase_service.py` (`MiniAppSubscriptionPurchaseService.submit_purchase`)
- Modify: `app/services/payment/stars.py` (`_finalize_simple_subscription_stars_payment`)
- Modify: `app/services/payment/yookassa.py` (simple subscription activation)
- Modify: `app/services/payment/lava.py`, `app/services/payment/platega.py`, `app/services/payment/cashera.py` (first charge callbacks)
- Test: `tests/services/test_referral_purchase_hook_coverage.py`

**Interfaces:**
- Consumes: `register_first_purchase_after_purchase(...)` from Task 4.
- Must be called after the purchase transaction has committed, never before.

- [ ] **Step 1: Add the static coverage test**

```python
# tests/services/test_referral_purchase_hook_coverage.py
from pathlib import Path

EXPECTED_FILES = {
    'app/handlers/subscription/purchase.py',
    'app/handlers/subscription/tariff_purchase.py',
    'app/handlers/simple_subscription.py',
    'app/handlers/menu.py',
    'app/services/subscription_purchase_service.py',
    'app/services/payment/stars.py',
    'app/services/payment/yookassa.py',
    'app/services/payment/lava.py',
    'app/services/payment/platega.py',
    'app/services/payment/cashera.py',
}


def test_all_purchase_paths_call_the_first_purchase_hook():
    missing = [
        path
        for path in EXPECTED_FILES
        if 'register_first_purchase_after_purchase' not in Path(path).read_text(encoding='utf-8')
    ]
    assert not missing, f'Missing referral hook in: {missing}'
```

This test is intentionally source-level: the flows call different variables and have different commit points, so it pins that no path was forgotten during refactors.

- [ ] **Step 2: Run the test and verify it fails**

Run: `pytest tests/services/test_referral_purchase_hook_coverage.py -v`
Expected: FAIL with a list of all files.

- [ ] **Step 3: Add the bot classic purchase hook**

In `app/handlers/subscription/purchase.py`, in `confirm_purchase`, after `transaction = await create_transaction(...)` and before the admin notification:

```python
        from app.services.referral_purchase_service import register_first_purchase_after_purchase

        await register_first_purchase_after_purchase(
            referee_id=db_user.id,
            subscription_id=subscription.id,
            duration_days=period_days,
            paid_amount_kopeks=final_price,
            source='bot_confirm_purchase',
        )
```

Use the same pattern in `handle_simple_subscription_pay_with_balance` and `handle_simple_subscription_pay_with_balance_disabled` after the subscription and transaction are committed, with `subscription_params['period_days']`, `price_kopeks`, and source `bot_simple_subscription_balance`. `handle_simple_subscription_purchase` only renders the choice screen; it has no purchase to hook.

- [ ] **Step 4: Add the tariff hooks**

In `confirm_tariff_purchase` and `confirm_daily_tariff_purchase`, after the subscription and transaction are committed:

```python
        from app.services.referral_purchase_service import register_first_purchase_after_purchase

        await register_first_purchase_after_purchase(
            referee_id=db_user.id,
            subscription_id=subscription.id,
            duration_days=period,
            paid_amount_kopeks=final_price,
            source='bot_tariff_purchase',
        )
```

For the daily variant, keep `duration_days=period`; the service will register the marker and skip the +10 because the period is below 30.

- [ ] **Step 5: Add the simple-subscription hooks**

In both balance paths in `app/handlers/simple_subscription.py`, after `await db.commit()` / `create_paid_subscription`:

```python
        from app.services.referral_purchase_service import register_first_purchase_after_purchase

        await register_first_purchase_after_purchase(
            referee_id=db_user.id,
            subscription_id=subscription.id,
            duration_days=subscription_params['period_days'],
            paid_amount_kopeks=price_kopeks,
            source='bot_simple_subscription_balance',
        )
```

- [ ] **Step 5b: Add the menu quick-activation hook**

In `app/handlers/menu.py`, in the quick activation branch, add the hook after the existing subscription is extended (`renewal_service.finalize`) or the new subscription and transaction are created:

```python
from app.services.referral_purchase_service import register_first_purchase_after_purchase

await register_first_purchase_after_purchase(
    referee_id=db_user.id,
    subscription_id=(subscription.id if subscription is not None else new_subscription.id),
    duration_days=best_period,
    paid_amount_kopeks=(pricing.final_total if subscription is not None else best_price),
    source='menu_quick_activation',
)
```

Place it once after the `if subscription: ... else: ...` block, before the final `except`; both variables (`subscription`, `new_subscription`) are defined by then. Do not place it before `create_transaction`; the purchase must be committed first.

- [ ] **Step 6: Add the cabinet/miniapp hook**

In `MiniAppSubscriptionPurchaseService.submit_purchase`, after the transaction is created and before returning:

```python
        from app.services.referral_purchase_service import register_first_purchase_after_purchase

        await register_first_purchase_after_purchase(
            referee_id=user.id,
            subscription_id=subscription.id,
            duration_days=pricing.selection.period.days,
            paid_amount_kopeks=pricing.final_total,
            source='miniapp_balance_purchase',
        )
```

- [ ] **Step 7: Add the Stars/YooKassa simple hooks**

In `_finalize_simple_subscription_stars_payment`, after `activate_pending_subscription` commits and the transaction side effects are emitted:

```python
        from app.services.referral_purchase_service import register_first_purchase_after_purchase

        await register_first_purchase_after_purchase(
            referee_id=user.id,
            subscription_id=subscription.id,
            duration_days=period_days,
            paid_amount_kopeks=amount_kopeks,
            source='stars_simple_subscription',
        )
```

In `app/services/payment/yookassa.py`, in the `is_simple_subscription` branch after `activate_pending_subscription` commits:

```python
                            from app.services.referral_purchase_service import (
                                register_first_purchase_after_purchase,
                            )

                            await register_first_purchase_after_purchase(
                                referee_id=user.id,
                                subscription_id=subscription.id,
                                duration_days=subscription_period,
                                paid_amount_kopeks=payment.amount_kopeks,
                                source='yookassa_simple_subscription',
                            )
```

- [ ] **Step 8: Add the provider first-charge hooks**

For Lava, after `await db.commit()` in the successful subscription-charge branch:

```python
            from app.services.referral_purchase_service import register_first_purchase_after_purchase

            await register_first_purchase_after_purchase(
                referee_id=record.user_id,
                subscription_id=record.subscription_id,
                duration_days=record.charge_days,
                paid_amount_kopeks=record.amount_kopeks,
                source='lava_recurring_first_charge',
            )
```

Use the same block in Platega and Cashera with sources `platega_recurring_first_charge` and `cashera_recurring_first_charge`. These callbacks are also used for renewals; the marker prevents a second reward.

Where the handler or payment service has a bot (`callback.bot`, `self.bot`), pass `bot=...` to `register_first_purchase_after_purchase` so the post-commit notifications from Task 4 are delivered. Service-only paths without a bot may omit it; the days and ledger rows are still written.

- [ ] **Step 9: Run the coverage test and relevant existing tests**

Run:

```bash
pytest tests/services/test_referral_purchase_hook_coverage.py tests/services/test_referral_first_purchase_service.py tests/services/test_payment_service_stars.py tests/services/test_payment_service_webhooks.py -v
```

Expected: PASS. If a flow has no `subscription` variable at the chosen point, move the call to the first point after commit where the subscription ID is known; do not call it before commit.

- [ ] **Step 10: Commit**

```bash
git add app/handlers/subscription/purchase.py app/handlers/subscription/tariff_purchase.py app/handlers/simple_subscription.py app/handlers/menu.py app/services/subscription_purchase_service.py app/services/payment/stars.py app/services/payment/yookassa.py app/services/payment/lava.py app/services/payment/platega.py app/services/payment/cashera.py tests/services/test_referral_purchase_hook_coverage.py
git commit -m "referral: hook first purchase into purchase flows"
```

---

### Task 6: Add +3 trial days for invited users

**Files:**
- Modify: `app/services/trial_activation_service.py`
- Modify: `app/handlers/start.py:629`
- Modify: `app/handlers/subscription/purchase.py` (`activate_trial`, paid trial preview, paid trial balance path)
- Modify: `app/cabinet/routes/subscription_modules/purchase.py` (`GET /trial`, `POST /trial`)
- Modify: `app/webapi/routes/miniapp.py` (trial preview and activation)
- Modify: `app/database/crud/subscription.py:2944` (`activate_pending_trial_subscription`)
- Test: `tests/services/test_referral_trial_bonus.py`

**Interfaces:**
- Produces: `get_effective_trial_days(user: User, base_days: int) -> int`.
- `base_days` may come from `settings.TRIAL_DURATION_DAYS` or `tariff.trial_duration_days`.
- Returns `base_days` unchanged for non-referred users, disabled program, non-days-only scheme, or `base_days <= 0`.
- Returns `base_days + settings.REFERRAL_TRIAL_BONUS_DAYS` otherwise.

- [ ] **Step 1: Write the failing helper test**

```python
# tests/services/test_referral_trial_bonus.py
from types import SimpleNamespace

from app.config import settings
from app.services.trial_activation_service import get_effective_trial_days


def test_referred_user_gets_three_extra_days(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    user = SimpleNamespace(referred_by_id=1)
    assert get_effective_trial_days(user, 3) == 6
    assert get_effective_trial_days(user, 7) == 10


def test_non_referred_or_disabled_program_gets_base_days(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    assert get_effective_trial_days(SimpleNamespace(referred_by_id=None), 3) == 3

    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)
    assert get_effective_trial_days(SimpleNamespace(referred_by_id=1), 3) == 3


def test_zero_base_days_does_not_create_a_trial(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    assert get_effective_trial_days(SimpleNamespace(referred_by_id=1), 0) == 0
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `pytest tests/services/test_referral_trial_bonus.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement the helper**

In `app/services/trial_activation_service.py`:

```python
def get_effective_trial_days(user: User, base_days: int) -> int:
    """Базовая длительность триала плюс бонус приглашённому."""
    base = max(0, int(base_days or 0))
    if base <= 0:
        return 0
    if not settings.is_referral_program_enabled() or not settings.is_referral_days_only_scheme():
        return base
    if getattr(user, 'referred_by_id', None) is None:
        return base
    return base + max(0, int(settings.REFERRAL_TRIAL_BONUS_DAYS))
```

- [ ] **Step 4: Use the helper at user-facing trial activation points**

In `app/handlers/start.py` `_activate_pending_trial`:

```python
        from app.services.trial_activation_service import get_effective_trial_days

        effective_trial_days = get_effective_trial_days(user, settings.TRIAL_DURATION_DAYS)
        subscription = await create_trial_subscription(
            db=db,
            user_id=user.id,
            duration_days=effective_trial_days,
            traffic_limit_gb=trial_traffic_limit,
            device_limit=trial_device_limit,
            connected_squads=trial_squads,
            tariff_id=tariff_id_for_trial,
        )
```

In `app/handlers/subscription/purchase.py` `activate_trial`, replace the effective trial duration in both free and paid branches:

```python
        from app.services.trial_activation_service import get_effective_trial_days

        effective_trial_days = get_effective_trial_days(
            db_user,
            trial_duration or settings.TRIAL_DURATION_DAYS,
        )
        subscription = await create_trial_subscription(
            db,
            db_user.id,
            duration_days=effective_trial_days,
            device_limit=trial_device_limit,
            traffic_limit_gb=trial_traffic_limit,
            connected_squads=trial_squads,
            tariff_id=tariff_id_for_trial,
        )
```

For the paid-trial preview, use `paid_trial_days = get_effective_trial_days(db_user, paid_trial_days)` after reading the tariff-specific value.

In `app/cabinet/routes/subscription_modules/purchase.py`:

- `GET /trial`: after `duration_days` is resolved, set `duration_days = get_effective_trial_days(user, duration_days)`.
- `POST /trial`: before `create_trial_subscription`, set `trial_duration = get_effective_trial_days(user, trial_duration)`.

In `app/webapi/routes/miniapp.py`:

- Trial preview: use `get_effective_trial_days(user, settings.TRIAL_DURATION_DAYS)`.
- Activation: before `create_trial_subscription`, set `trial_duration = get_effective_trial_days(user, trial_duration or settings.TRIAL_DURATION_DAYS)`.

Do not change `app/services/promocode_service.py`, `app/handlers/admin/users.py`, or `app/webapi/routes/users.py`; these are not user-facing trial activation.

- [ ] **Step 5: Preserve the bonus when a pending trial is activated late**

In `app/database/crud/subscription.py` `activate_pending_trial_subscription`, replace the fallback to global settings:

```python
    duration_days = None
    if getattr(pending_subscription, 'duration_days', None):
        duration_days = pending_subscription.duration_days
    elif pending_subscription.start_date and pending_subscription.end_date:
        duration_days = max(1, (pending_subscription.end_date - pending_subscription.start_date).days)
    if duration_days:
        pending_subscription.end_date = current_time + timedelta(days=duration_days)
    elif pending_subscription.end_date and pending_subscription.end_date < current_time:
        pending_subscription.end_date = current_time + timedelta(days=settings.TRIAL_DURATION_DAYS)
```

This keeps a 6-day pending trial at 6 days instead of recomputing 3.

- [ ] **Step 6: Update trial previews and notifications that read the setting directly**

Replace direct `settings.TRIAL_DURATION_DAYS` reads in user-facing trial copy with the effective value:

- `app/handlers/subscription/purchase.py:646`, `:3733`
- `app/webapi/routes/miniapp.py:3520`, `:3527`, `:3896`, `:4082`
- `app/cabinet/routes/subscription_modules/purchase.py:1359`, `:1368`, `:1526`
- `app/handlers/stars_payments.py:346`
- `app/services/payment/yookassa.py:717`

For notifications, pass the actual subscription duration or the effective days computed for that user; do not re-read the global setting.

- [ ] **Step 7: Run tests**

Run:

```bash
pytest tests/services/test_referral_trial_bonus.py tests/test_trial_activation_paid.py tests/webapi/test_subscription_sync_routes.py -v
```

Expected: PASS. If a test asserts the literal 3 days for a referred user, update the test fixture to create a non-referred user or expect 6.

- [ ] **Step 8: Commit**

```bash
git add app/services/trial_activation_service.py app/handlers/start.py app/handlers/subscription/purchase.py app/cabinet/routes/subscription_modules/purchase.py app/webapi/routes/miniapp.py app/database/crud/subscription.py app/handlers/stars_payments.py app/services/payment/yookassa.py tests/services/test_referral_trial_bonus.py
git commit -m "referral: add invited trial bonus"
```

---

### Task 7: Disable legacy money, levels, and withdrawal in days-only mode

**Files:**
- Modify: `app/services/referral_service.py` (`process_referral_topup`, `process_referral_registration`)
- Modify: `app/services/referral_reward_service.py` (`award_referral_rewards`)
- Test: `tests/services/test_referral_days_only_legacy_disabled.py`

**Interfaces:**
- `process_referral_topup` returns `True` and writes nothing in days-only mode.
- `process_referral_registration` returns `True` before writing `referral_registration_pending` or awarding anything.
- `award_referral_rewards` returns `[]` in days-only mode.

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/test_referral_days_only_legacy_disabled.py
import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, ReferralEarning, User
from app.services.referral_reward_service import award_referral_rewards
from app.services.referral_service import process_referral_registration, process_referral_topup
from tests.fixtures.sqlite_memory import memory_session

TABLES = list(Base.metadata.sorted_tables)


def _user(uid: int, referred_by_id: int | None = None) -> User:
    return User(
        id=uid,
        telegram_id=1000 + uid,
        first_name=f'U{uid}',
        language='ru',
        balance_kopeks=0,
        referred_by_id=referred_by_id,
    )


@pytest.mark.asyncio
async def test_registration_writes_no_pending_row(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()

        assert await process_referral_registration(db, 2, 1) is True
        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_topup_does_not_change_balance_or_flags(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()

        assert await process_referral_topup(db, 2, 10_000) is True
        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert rows == []
        referee = await db.get(User, 2)
        referrer = await db.get(User, 1)
        assert referee.balance_kopeks == 0
        assert referrer.balance_kopeks == 0
        assert referee.has_made_first_topup is False


@pytest.mark.asyncio
async def test_award_rewards_is_a_noop(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()
        referee = await db.get(User, 2)
        assert await award_referral_rewards(db, referee, event='first_topup', topup_amount_kopeks=10_000) == []
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `pytest tests/services/test_referral_days_only_legacy_disabled.py -v`
Expected: FAIL because the legacy paths still write rows/balance.

- [ ] **Step 3: Add early returns**

In `process_referral_topup` at the very top of the `try` before fetching the user:

```python
        if settings.is_referral_days_only_scheme():
            logger.debug('Days-only referral scheme: topup rewards disabled', user_id=user_id)
            return True
```

In `process_referral_registration` at the very top, before the self-referral check or immediately after it:

```python
        if settings.is_referral_days_only_scheme():
            logger.debug('Days-only referral scheme: registration rewards disabled', user_id=new_user_id)
            return True
```

In `award_referral_rewards`, before `build_reward_components`:

```python
    if settings.is_referral_days_only_scheme():
        return []
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/services/test_referral_days_only_legacy_disabled.py tests/services/test_referral_service.py -v`
Expected: PASS. If an existing test expects a registration row, gate it to `legacy`/`levels` scheme or update its setup.

- [ ] **Step 5: Commit**

```bash
git add app/services/referral_service.py app/services/referral_reward_service.py tests/services/test_referral_days_only_legacy_disabled.py
git commit -m "referral: disable legacy rewards in days-only mode"
```

---

### Task 8: Update referral statistics and verify concurrency

**Files:**
- Modify: `app/utils/user_utils.py` (`get_user_referral_summary`, `get_detailed_referral_list`)
- Test: `tests/utils/test_user_referral_summary_days_only.py`
- Test: `tests/database/test_referral_first_purchase_postgres.py` (PostgreSQL-only, skip on SQLite)

**Interfaces:**
- `get_user_referral_summary` returns `paid_referrals_count` from marker rows, not `has_made_first_topup`.
- `get_detailed_referral_list` returns `has_first_purchase: bool` for each referral.
- `days_earned` remains the sum of referrer-directed reward days.

- [ ] **Step 1: Write the failing summary test**

```python
# tests/utils/test_user_referral_summary_days_only.py
from datetime import UTC, datetime

import pytest

from app.database.models import Base, ReferralEarning, User
from app.services.referral_reward_service import (
    REASON_FIRST_PURCHASE,
    REASON_FIRST_PURCHASE_REGISTERED,
)
from app.utils.user_utils import get_user_referral_summary
from tests.fixtures.sqlite_memory import memory_session

TABLES = list(Base.metadata.sorted_tables)


def _user(uid: int, referred_by_id: int | None = None) -> User:
    return User(
        id=uid,
        telegram_id=1000 + uid,
        first_name=f'U{uid}',
        language='ru',
        balance_kopeks=0,
        referred_by_id=referred_by_id,
    )


@pytest.mark.asyncio
async def test_summary_counts_markers_and_referrer_days(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _user(3, referred_by_id=1)])
        db.add_all(
            [
                ReferralEarning(
                    user_id=2,
                    referral_id=1,
                    amount_kopeks=0,
                    reason=REASON_FIRST_PURCHASE_REGISTERED,
                    reward_type='days',
                    days_granted=0,
                ),
                ReferralEarning(
                    user_id=1,
                    referral_id=2,
                    amount_kopeks=0,
                    reason=REASON_FIRST_PURCHASE,
                    reward_type='days',
                    days_granted=10,
                    created_at=datetime.now(UTC),
                ),
            ]
        )
        await db.commit()

        summary = await get_user_referral_summary(db, 1)

        assert summary['invited_count'] == 2
        assert summary['paid_referrals_count'] == 1
        assert summary['total_earned_days'] == 10
        assert summary['conversion_rate'] == 50.0
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `pytest tests/utils/test_user_referral_summary_days_only.py -v`
Expected: FAIL because `paid_referrals_count` is still `has_made_first_topup` based.

- [ ] **Step 3: Update the summary query**

In `app/utils/user_utils.py`, replace the in-memory `paid_referrals_count` sum with a marker count:

```python
        from app.services.referral_reward_service import REASON_FIRST_PURCHASE_REGISTERED

        paid_referrals_result = await db.execute(
            select(func.count(func.distinct(ReferralEarning.user_id))).where(
                ReferralEarning.referral_id == user_id,
                ReferralEarning.reason == REASON_FIRST_PURCHASE_REGISTERED,
            )
        )
        paid_referrals_count = int(paid_referrals_result.scalar() or 0)
```

Keep loading `referrals` only if other parts of the function need it; if not, remove the full user query to avoid loading all referrals.

- [ ] **Step 4: Add the per-referral paid flag**

In `get_detailed_referral_list`, add a marker lookup per referral (or a single grouped query) and include it in the result:

```python
            marker_result = await db.execute(
                select(ReferralEarning.id)
                .where(
                    ReferralEarning.user_id == referral.id,
                    ReferralEarning.reason == REASON_FIRST_PURCHASE_REGISTERED,
                )
                .limit(1)
            )
            has_first_purchase = marker_result.scalar_one_or_none() is not None
```

Add `'has_first_purchase': has_first_purchase` to the returned referral dictionary. Keep `'has_made_first_topup'` for compatibility until the UI is migrated.

- [ ] **Step 5: Run the summary and existing referral tests**

Run:

```bash
pytest tests/utils/test_user_referral_summary_days_only.py tests/handlers/test_referral_invite.py tests/crud/test_referral_earnings_filter.py -v
```

Expected: PASS.

- [ ] **Step 6: Add a PostgreSQL concurrency test**

Create `tests/database/test_referral_first_purchase_postgres.py`, skipped unless a PostgreSQL test URL is configured. The test builds its own engine because SQLite ignores `FOR UPDATE`; it must not use the shared in-memory fixture.

```python
# tests/database/test_referral_first_purchase_postgres.py
import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database.models import Base, ReferralEarning, Subscription, SubscriptionStatus, User
from app.services.referral_purchase_service import register_first_purchase
from app.services.referral_reward_service import REASON_FIRST_PURCHASE_REGISTERED

pytestmark = pytest.mark.skipif(
    not os.getenv('TEST_POSTGRES_DSN'),
    reason='PostgreSQL-only concurrency test',
)


@pytest.mark.asyncio
async def test_concurrent_first_purchase_grants_once(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    engine = create_async_engine(os.environ['TEST_POSTGRES_DSN'])
    maker = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with maker() as db:
        db.add_all(
            [
                User(id=1, telegram_id=1001, first_name='R', language='ru', balance_kopeks=0),
                User(id=2, telegram_id=1002, first_name='F', language='ru', balance_kopeks=0, referred_by_id=1),
                Subscription(
                    id=10,
                    user_id=2,
                    status=SubscriptionStatus.ACTIVE.value,
                    is_trial=False,
                    start_date=now - timedelta(days=1),
                    end_date=now + timedelta(days=30),
                    traffic_limit_gb=100,
                    device_limit=1,
                ),
                Subscription(
                    id=11,
                    user_id=1,
                    status=SubscriptionStatus.ACTIVE.value,
                    is_trial=False,
                    start_date=now - timedelta(days=1),
                    end_date=now + timedelta(days=30),
                    traffic_limit_gb=100,
                    device_limit=1,
                ),
            ]
        )
        await db.commit()

    async def call() -> object:
        async with maker() as db:
            return await register_first_purchase(
                db,
                referee_id=2,
                subscription_id=10,
                duration_days=30,
                paid_amount_kopeks=10_000,
                source='concurrency-test',
            )

    results = await asyncio.gather(call(), call())

    async with maker() as db:
        markers = (
            (
                await db.execute(
                    getattr(__import__('sqlalchemy'), 'select')(ReferralEarning).where(
                        ReferralEarning.user_id == 2,
                        ReferralEarning.reason == REASON_FIRST_PURCHASE_REGISTERED,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(markers) == 1
    assert sorted(result.status for result in results) == ['already_registered', 'rewarded']
    await engine.dispose()
```

The test uses a real PostgreSQL database, drops and recreates the schema, and must never run against a production DSN. The project's `TEST_POSTGRES_DSN` is an isolated test database.

- [ ] **Step 7: Commit**

```bash
git add app/utils/user_utils.py tests/utils/test_user_referral_summary_days_only.py tests/database/test_referral_first_purchase_postgres.py
git commit -m "referral: count days-only paid referrals from markers"
```

---

### Task 9: Add the backfill CLI and rollout verification

**Files:**
- Create: `app/tools/referral_days_only_backfill.py`
- Test: `tests/tools/test_referral_days_only_backfill.py`

**Interfaces:**
- CLI: `python -m app.tools.referral_days_only_backfill --apply`
- Without `--apply`, prints how many markers would be created and exits.
- With `--apply`, calls `backfill_first_purchase_markers` and prints the created count.

- [ ] **Step 1: Write the failing CLI test**

```python
# tests/tools/test_referral_days_only_backfill.py
import pytest

from app.tools.referral_days_only_backfill import build_parser


def test_parser_requires_explicit_apply_flag():
    parser = build_parser()
    args = parser.parse_args([])
    assert args.apply is False

    args = parser.parse_args(['--apply'])
    assert args.apply is True
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `pytest tests/tools/test_referral_days_only_backfill.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the CLI**

```python
# app/tools/referral_days_only_backfill.py
import argparse
import asyncio

from app.database.database import AsyncSessionLocal
from app.services.referral_purchase_service import backfill_first_purchase_markers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Backfill referral first-purchase markers for existing paid referrals.',
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Write markers. Without this flag the command is a dry run.',
    )
    return parser


async def _run(apply: bool) -> int:
    async with AsyncSessionLocal() as db:
        if not apply:
            from sqlalchemy import exists, func, select

            from app.database.models import Subscription, User

            result = await db.execute(
                select(func.count(User.id)).where(
                    User.referred_by_id.isnot(None),
                    exists(
                        select(Subscription.id).where(
                            Subscription.user_id == User.id,
                            Subscription.is_trial.is_(False),
                        )
                    ),
                )
            )
            print(f'Candidates: {int(result.scalar() or 0)}')
            return 0

        created = await backfill_first_purchase_markers(db)
        print(f'Created markers: {created}')
        return 0


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args.apply)))


if __name__ == '__main__':
    main()
```

The dry-run count includes already-marked users; that is acceptable for an operator estimate. The apply path is idempotent.

- [ ] **Step 4: Run the test**

Run: `pytest tests/tools/test_referral_days_only_backfill.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/tools/referral_days_only_backfill.py tests/tools/test_referral_days_only_backfill.py
git commit -m "referral: add days-only backfill command"
```

---

### Task 10: Cover the remaining paid tariff purchase flows

**Files:**
- Modify: `app/handlers/subscription/tariff_purchase.py`
- Modify: `tests/services/test_referral_purchase_hook_coverage.py`

**Interfaces:**
- Consumes: `register_first_purchase_after_purchase(...)` from Task 4.
- Adds hooks only after the subscription/payment has committed.

**Ruling:** `confirm_instant_switch` is a prorated tariff change, not a new subscription-period purchase; it is intentionally excluded. `handle_trial_pay_with_balance` activates a paid trial, not a subscription purchase; it is intentionally excluded. All other listed flows purchase a subscription period and must register the first purchase.

- [ ] **Step 1: Add function-body coverage tests**

Extend `tests/services/test_referral_purchase_hook_coverage.py` with AST checks that the helper is present inside each of these functions, after the last `create_transaction`/commit call:
- `confirm_tariff_extend`
- `confirm_tariff_switch`
- `confirm_daily_tariff_switch`
- `handle_custom_confirm`

The check must inspect the specific function body, not just the module text. A file-level substring test is not sufficient (it missed the earlier `_extend_existing_subscription` gap).

- [ ] **Step 2: Run the test and verify it fails**

Run: `pytest tests/services/test_referral_purchase_hook_coverage.py -v`
Expected: FAIL for the four functions.

- [ ] **Step 3: Hook `confirm_tariff_extend`**

After the subscription extension and transaction/payment are committed, before the admin notification:

```python
    from app.services.referral_purchase_service import register_first_purchase_after_purchase

    await register_first_purchase_after_purchase(
        referee_id=db_user.id,
        subscription_id=subscription.id,
        duration_days=period,
        paid_amount_kopeks=final_price,
        source='bot_tariff_extend',
        bot=callback.bot,
    )
```

- [ ] **Step 4: Hook `confirm_tariff_switch`**

After `extend_subscription` and the transaction/payment commit:

```python
    from app.services.referral_purchase_service import register_first_purchase_after_purchase

    await register_first_purchase_after_purchase(
        referee_id=db_user.id,
        subscription_id=subscription.id,
        duration_days=period,
        paid_amount_kopeks=final_price,
        source='bot_tariff_switch',
        bot=callback.bot,
    )
```

- [ ] **Step 5: Hook `confirm_daily_tariff_switch`**

Daily switches purchase one day. After the subscription switch commits:

```python
    from app.services.referral_purchase_service import register_first_purchase_after_purchase

    await register_first_purchase_after_purchase(
        referee_id=db_user.id,
        subscription_id=subscription.id,
        duration_days=1,
        paid_amount_kopeks=final_daily_price,
        source='bot_daily_tariff_switch',
        bot=callback.bot,
    )
```

The marker is written; +10 is skipped because the period is below 30 days.

- [ ] **Step 6: Hook `handle_custom_confirm`**

After the custom subscription is created or extended and the transaction is committed:

```python
    from app.services.referral_purchase_service import register_first_purchase_after_purchase

    await register_first_purchase_after_purchase(
        referee_id=db_user.id,
        subscription_id=subscription.id,
        duration_days=custom_days,
        paid_amount_kopeks=total_price,
        source='bot_custom_tariff_purchase',
        bot=callback.bot,
    )
```

- [ ] **Step 7: Run the coverage and focused tests**

Run:

```bash
pytest tests/services/test_referral_purchase_hook_coverage.py tests/services/test_referral_first_purchase_service.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/handlers/subscription/tariff_purchase.py tests/services/test_referral_purchase_hook_coverage.py
git commit -m "referral: hook remaining tariff purchase flows"
```

## Final Verification

Before declaring the backend complete:

- [ ] `pytest tests/services/test_referral_days_only_contract.py tests/crud/test_add_reward_days.py tests/services/test_referral_first_purchase_marker.py tests/services/test_referral_first_purchase_service.py tests/services/test_referral_purchase_hook_coverage.py tests/services/test_referral_trial_bonus.py tests/services/test_referral_days_only_legacy_disabled.py tests/utils/test_user_referral_summary_days_only.py tests/tools/test_referral_days_only_backfill.py -v`
- [ ] `pytest tests/services/test_attach_referrer_if_missing.py tests/services/test_referral_service.py tests/services/test_payment_service_stars.py tests/services/test_payment_service_webhooks.py -v`
- [ ] Manual dry run: `python -m app.tools.referral_days_only_backfill` prints a candidate count.
- [ ] Inspect `git diff` for accidental `has_made_first_topup` or `has_had_paid_subscription` changes. There must be none.
- [ ] Confirm no Alembic revision was created.
- [ ] Append the change set to `../claude-changes.md` with scope, what, and why (project rule).
