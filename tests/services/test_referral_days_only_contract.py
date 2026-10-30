from app.config import settings
from app.services.referral_reward_service import (
    REASON_FIRST_PURCHASE,
    REASON_FIRST_PURCHASE_REFEREE,
    REASON_FIRST_PURCHASE_REGISTERED,
    REFEREE_DIRECTED_REASONS,
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
