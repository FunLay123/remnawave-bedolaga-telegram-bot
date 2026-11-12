from app.config import Settings, settings
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


def test_first_purchase_side_days_default_to_none_and_fall_back(monkeypatch):
    """Раздельные дни сторон по умолчанию не заданы и падают в общий ключ."""
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 10)
    assert settings.REFERRAL_FIRST_PURCHASE_REFEREE_DAYS is None
    assert settings.REFERRAL_FIRST_PURCHASE_REFERRER_DAYS is None
    assert settings.get_referral_first_purchase_referee_days() == 10
    assert settings.get_referral_first_purchase_referrer_days() == 10
    assert settings.REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS == 30


def test_first_purchase_side_days_override_common_bonus(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 10)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', 7)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', 3)

    assert settings.get_referral_first_purchase_referee_days() == 7
    assert settings.get_referral_first_purchase_referrer_days() == 3


def test_negative_side_days_are_normalized_to_zero(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', -5)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', -1)

    assert settings.get_referral_first_purchase_referee_days() == 0
    assert settings.get_referral_first_purchase_referrer_days() == 0


def test_side_override_mixes_with_fallback_for_the_other_side(monkeypatch):
    """Заданный ключ одной стороны не перетягивает вторую с общего fallback."""
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 10)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', 7)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', None)

    assert settings.get_referral_first_purchase_referee_days() == 7
    assert settings.get_referral_first_purchase_referrer_days() == 10


def test_negative_override_does_not_inherit_positive_fallback(monkeypatch):
    """Отрицательный ключ стороны — это 0, а не «взять общий бонус»."""
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 10)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', -3)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', None)

    assert settings.get_referral_first_purchase_referee_days() == 0
    assert settings.get_referral_first_purchase_referrer_days() == 10


def test_empty_env_strings_for_side_days_fall_back_to_common_key():
    """Пустое значение в .env — это «не задано», а не ValidationError на старте.

    ``REFERRAL_FIRST_PURCHASE_REFEREE_DAYS=`` в .env раньше ронял pydantic
    ещё до применения fallback: ``int | None`` не принимает пустую строку.
    """
    config = Settings(
        REFERRAL_FIRST_PURCHASE_BONUS_DAYS=10,
        REFERRAL_FIRST_PURCHASE_REFEREE_DAYS='',
        REFERRAL_FIRST_PURCHASE_REFERRER_DAYS='   ',
    )

    assert config.REFERRAL_FIRST_PURCHASE_REFEREE_DAYS is None
    assert config.REFERRAL_FIRST_PURCHASE_REFERRER_DAYS is None
    assert config.get_referral_first_purchase_referee_days() == 10
    assert config.get_referral_first_purchase_referrer_days() == 10


def test_negative_common_fallback_is_normalized_to_zero(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', -4)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', None)

    assert settings.get_referral_first_purchase_referee_days() == 0
    assert settings.get_referral_first_purchase_referrer_days() == 0


def test_referral_settings_expose_effective_side_days(monkeypatch):
    """Старый общий ключ остаётся в словаре, новые — эффективные значения."""
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', 7)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', 3)

    referral_settings = settings.get_referral_settings()

    assert referral_settings['first_purchase_referee_days'] == 7
    assert referral_settings['first_purchase_referrer_days'] == 3
    assert referral_settings['first_purchase_bonus_days'] == settings.REFERRAL_FIRST_PURCHASE_BONUS_DAYS


def test_withdrawal_is_disabled_in_days_only_scheme_even_when_flag_is_on(monkeypatch):
    """В days-only деньги не начисляются, выводить нечего — флаг не должен протекать."""
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')

    assert settings.is_referral_withdrawal_enabled() is False


def test_withdrawal_stays_enabled_in_legacy_scheme_when_flag_is_on(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')

    assert settings.is_referral_withdrawal_enabled() is True
