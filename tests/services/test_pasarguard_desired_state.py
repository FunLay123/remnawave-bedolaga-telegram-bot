"""Матрица желаемого состояния пользователя PasarGuard (xdrive).

Чистый evaluator: без БД и API. Проверяем все гейты eligibility из дизайна —
флаг, статус подписки и пользователя, grace, premium-пул и абсолютный
``data_limit``.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import SubscriptionStatus, UserStatus
from app.external.pasarguard_api import PasarguardUser
from app.services.pasarguard_sync_service import (
    PremiumStateView,
    SyncSubject,
    evaluate_desired_state,
)


NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
END = NOW + timedelta(days=30)


def _state(*, is_limited=False, is_exhausted=False, closed_at=None, total=20, used=0):
    return PremiumStateView(
        is_limited=is_limited,
        is_exhausted=is_exhausted,
        closed_at=closed_at,
        total_limit_bytes=total,
        used_bytes=used,
    )


def _pasarguard_user(*, used=0):
    return PasarguardUser(
        id=42,
        username='bx12',
        status='active',
        expire=END,
        data_limit=20,
        used_traffic=used,
        lifetime_used_traffic=used,
        group_ids=[7],
        note='xdrive-bot:inst:sub:12',
    )


def _subject(**overrides):
    values = {
        'subscription_id': 12,
        'subscription_status': SubscriptionStatus.ACTIVE.value,
        'end_date': END,
        'user_status': UserStatus.ACTIVE.value,
        'premium_state': _state(),
        'premium_squad_count': 1,
        'grace_open': False,
        'pasarguard_user': None,
    }
    values.update(overrides)
    return SyncSubject(**values)


def _evaluate(subject, *, enabled=True):
    return evaluate_desired_state(subject, enabled=enabled, group_id=7, prefix='bx', instance_id='inst', now=NOW)


def test_flag_off_disables_existing_user_and_creates_nothing():
    existing = _evaluate(_subject(pasarguard_user=_pasarguard_user()), enabled=False)
    assert existing.exists is True
    assert existing.enabled is False

    absent = _evaluate(_subject(), enabled=False)
    assert absent.exists is False
    assert absent.enabled is False


def test_active_subscription_with_one_allowed_squad_is_enabled():
    desired = _evaluate(_subject())

    assert desired.exists is True
    assert desired.enabled is True
    assert desired.data_limit == 20
    assert desired.expire == END
    assert desired.group_ids == (7,)
    assert desired.username == 'bx12'
    assert 'sub:12' in desired.note


def test_trial_with_premium_squad_is_enabled():
    desired = _evaluate(_subject(subscription_status=SubscriptionStatus.TRIAL.value))

    assert desired.enabled is True


def test_trial_without_premium_state_is_disabled():
    desired = _evaluate(_subject(subscription_status=SubscriptionStatus.TRIAL.value, premium_state=None))

    assert desired.exists is False
    assert desired.enabled is False


def test_data_limit_is_absolute_not_remaining():
    desired = _evaluate(
        _subject(
            premium_state=_state(total=20, used=10),
            pasarguard_user=_pasarguard_user(used=10),
        )
    )

    assert desired.enabled is True
    assert desired.data_limit == 20
    assert desired.data_limit != 10


@pytest.mark.parametrize(
    'overrides',
    [
        {'subscription_status': SubscriptionStatus.EXPIRED.value},
        {'subscription_status': SubscriptionStatus.LIMITED.value},
        {'subscription_status': SubscriptionStatus.DISABLED.value},
        {'subscription_status': SubscriptionStatus.PENDING.value},
        {'user_status': UserStatus.BLOCKED.value},
        {'end_date': NOW - timedelta(days=1)},
    ],
    ids=['expired', 'limited', 'disabled', 'pending', 'blocked', 'past_end_date'],
)
def test_expired_limited_disabled_pending_and_blocked_are_disabled(overrides):
    desired = _evaluate(_subject(**overrides))

    assert desired.enabled is False


def test_open_grace_is_disabled():
    desired = _evaluate(_subject(grace_open=True))

    assert desired.enabled is False


def test_missing_premium_state_is_disabled():
    desired = _evaluate(_subject(premium_state=None))

    assert desired.enabled is False


def test_two_premium_squads_are_disabled():
    desired = _evaluate(_subject(premium_squad_count=2))

    assert desired.enabled is False


@pytest.mark.parametrize(
    'state',
    [
        _state(closed_at=NOW),
        _state(is_exhausted=True),
        _state(is_limited=True),
    ],
    ids=['closed', 'exhausted', 'limited'],
)
def test_closed_or_exhausted_premium_state_is_disabled(state):
    desired = _evaluate(_subject(premium_state=state))

    assert desired.enabled is False
