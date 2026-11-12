from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.handlers.referral as referral_handler
from app.config import settings
from app.database.models import SubscriptionStatus
from app.utils.referral_ui import build_days_only_referral_screen


def _user(*, subscriptions=None):
    return SimpleNamespace(
        id=1,
        telegram_id=1001,
        language='ru',
        referral_code='ABC123',
        subscriptions=subscriptions or [],
        is_trial_already_used=lambda: False,
        auth_type='telegram',
    )


def _sub(status: str, *, trial: bool = False, tariff=None, start_date=None, end_date=None):
    return SimpleNamespace(
        status=status,
        is_trial=trial,
        tariff=tariff,
        start_date=start_date,
        end_date=end_date,
    )


def test_full_screen_has_stats_link_share_and_no_old_buttons():
    user = _user(subscriptions=[_sub(SubscriptionStatus.ACTIVE.value)])
    summary = {
        'invited_count': 12,
        'paid_referrals_count': 4,
        'total_earned_days': 40,
        'recent_earnings': [],
    }

    text, keyboard = build_days_only_referral_screen(
        user=user,
        summary=summary,
        bot_referral_link='https://t.me/bot?start=ref_ABC123',
        trial_available=False,
    )

    assert 'Приглашено: <b>12</b>' in text
    assert 'Оплатили: <b>4</b>' in text
    assert 'Получено бонусом: <b>+40 дн.</b>' in text
    assert '<blockquote><code>https://t.me/bot?start=ref_ABC123</code></blockquote>' in text

    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert 'referral_list' in callbacks
    assert 'back_to_menu' in callbacks
    assert 'referral_show_qr' not in callbacks
    assert 'referral_analytics' not in callbacks
    assert 'referral_reward_settings' not in callbacks
    assert 'referral_withdrawal' not in callbacks

    share = keyboard.inline_keyboard[0][0]
    assert share.url.startswith('https://t.me/share/url?')
    assert share.style == 'primary'
    assert share.icon_custom_emoji_id == '5334552404736104314'


def test_no_subscription_screen_has_trial_cta_and_back():
    user = _user(subscriptions=[])
    text, keyboard = build_days_only_referral_screen(
        user=user,
        summary={'invited_count': 0, 'paid_referrals_count': 0, 'total_earned_days': 0},
        bot_referral_link='https://t.me/bot?start=ref_ABC123',
        trial_available=True,
    )

    assert 'Бонусные дни начисляются только' in text
    assert 'на подходящую подписку (не суточную)' in text
    assert 'https://t.me/bot?start=ref_ABC123' not in text

    rows = keyboard.inline_keyboard
    assert rows[0][0].text == 'Начать триал'
    assert rows[0][0].callback_data == 'referral_start_trial'
    assert rows[0][0].style == 'primary'
    assert rows[0][0].icon_custom_emoji_id == '5314310000531766389'
    assert rows[-1][0].callback_data == 'back_to_menu'


def test_no_subscription_screen_falls_back_to_subscription_cta():
    user = _user(subscriptions=[])
    text, keyboard = build_days_only_referral_screen(
        user=user,
        summary={'invited_count': 0, 'paid_referrals_count': 0, 'total_earned_days': 0},
        bot_referral_link='https://t.me/bot?start=ref_ABC123',
        trial_available=False,
    )

    assert 'Бонусные дни начисляются только' in text
    assert keyboard.inline_keyboard[0][0].text == 'Моя подписка'
    assert keyboard.inline_keyboard[0][0].callback_data == 'menu_subscription'
    assert keyboard.inline_keyboard[0][0].style == 'primary'
    assert keyboard.inline_keyboard[0][0].icon_custom_emoji_id == '5314310000531766389'


def test_terms_block_uses_referral_settings(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_TRIAL_BONUS_DAYS', 7)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 21)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', 21)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', 21)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 60)

    user = _user(subscriptions=[_sub(SubscriptionStatus.ACTIVE.value)])
    text, _keyboard = build_days_only_referral_screen(
        user=user,
        summary={'invited_count': 0, 'paid_referrals_count': 0, 'total_earned_days': 0},
        bot_referral_link='https://t.me/bot?start=ref_ABC123',
        trial_available=False,
    )

    assert '+7 дней к триалу другу' in text
    assert '+21 день вам и другу' in text
    assert 'за первую покупку от 60 дней' in text


def test_daily_tariff_note_shown_when_first_purchase_bonus_is_active(monkeypatch):
    """Активный бонус за первую покупку обязан предупредить про суточные тарифы."""
    monkeypatch.setattr(settings, 'REFERRAL_TRIAL_BONUS_DAYS', 0)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', 7)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', 3)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)

    user = _user(subscriptions=[_sub(SubscriptionStatus.ACTIVE.value)])
    text, _keyboard = build_days_only_referral_screen(
        user=user,
        summary={'invited_count': 0, 'paid_referrals_count': 0, 'total_earned_days': 0},
        bot_referral_link='https://t.me/bot?start=ref_ABC123',
        trial_available=False,
    )

    assert 'Суточные тарифы не участвуют в бонусе за первую покупку.' in text


def test_daily_tariff_note_absent_when_first_purchase_bonus_is_zero(monkeypatch):
    """При нулевом бонусе за первую покупку предупреждение не показывается."""
    monkeypatch.setattr(settings, 'REFERRAL_TRIAL_BONUS_DAYS', 3)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', 0)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', 0)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)

    user = _user(subscriptions=[_sub(SubscriptionStatus.ACTIVE.value)])
    text, _keyboard = build_days_only_referral_screen(
        user=user,
        summary={'invited_count': 0, 'paid_referrals_count': 0, 'total_earned_days': 0},
        bot_referral_link='https://t.me/bot?start=ref_ABC123',
        trial_available=False,
    )

    assert 'Суточные тарифы не участвуют' not in text


def test_separate_side_days_are_shown_per_side(monkeypatch):
    """Разные значения сторон не сливаются в одно обещание «вам и другу»."""
    monkeypatch.setattr(settings, 'REFERRAL_TRIAL_BONUS_DAYS', 3)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', 7)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', 3)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)

    user = _user(subscriptions=[_sub(SubscriptionStatus.ACTIVE.value)])
    text, _keyboard = build_days_only_referral_screen(
        user=user,
        summary={'invited_count': 0, 'paid_referrals_count': 0, 'total_earned_days': 0},
        bot_referral_link='https://t.me/bot?start=ref_ABC123',
        trial_available=False,
    )

    assert '+3 дня вам' in text
    assert '+7 дней другу' in text
    assert 'вам и другу' not in text
    assert 'за первую покупку от 30 дней' in text


def test_daily_tariff_subscription_gets_cta_without_link():
    """Суточному тарифу дни не начислить: экран не должен обещать обратное."""
    user = _user(
        subscriptions=[
            _sub(
                SubscriptionStatus.ACTIVE.value,
                tariff=SimpleNamespace(is_daily=True),
            )
        ]
    )

    text, keyboard = build_days_only_referral_screen(
        user=user,
        summary={'invited_count': 0, 'paid_referrals_count': 0, 'total_earned_days': 0},
        bot_referral_link='https://t.me/bot?start=ref_ABC123',
        trial_available=True,
    )

    assert 'Бонусные дни начисляются только' in text
    # Владелец суточного тарифа подписку имеет, поэтому «при наличии подписки»
    # звучало бы для него ложью; формулировка должна говорить о подходящей.
    assert 'на подходящую подписку (не суточную)' in text
    assert 'при наличии подписки' not in text
    assert 'https://t.me/bot?start=ref_ABC123' not in text
    assert keyboard.inline_keyboard[0][0].callback_data == 'referral_start_trial'
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert 'referral_list' not in callbacks


def test_expired_zero_duration_placeholder_gets_cta_without_link():
    """EXPIRED с end_date == start_date — пустышка, а не подписка с днями."""
    moment = datetime.now(UTC)
    user = _user(
        subscriptions=[
            _sub(SubscriptionStatus.EXPIRED.value, start_date=moment, end_date=moment),
        ]
    )

    text, keyboard = build_days_only_referral_screen(
        user=user,
        summary={'invited_count': 0, 'paid_referrals_count': 0, 'total_earned_days': 0},
        bot_referral_link='https://t.me/bot?start=ref_ABC123',
        trial_available=True,
    )

    assert 'Бонусные дни начисляются только' in text
    assert 'https://t.me/bot?start=ref_ABC123' not in text
    assert keyboard.inline_keyboard[0][0].callback_data == 'referral_start_trial'


def test_active_subscription_with_normal_dates_still_shows_link():
    """Обычная живая подписка не должна попасть под новые ограничения."""
    moment = datetime.now(UTC)
    user = _user(
        subscriptions=[
            _sub(
                SubscriptionStatus.ACTIVE.value,
                tariff=SimpleNamespace(is_daily=False),
                start_date=moment,
                end_date=moment.replace(year=moment.year + 1),
            )
        ]
    )

    text, keyboard = build_days_only_referral_screen(
        user=user,
        summary={'invited_count': 0, 'paid_referrals_count': 0, 'total_earned_days': 0},
        bot_referral_link='https://t.me/bot?start=ref_ABC123',
        trial_available=False,
    )

    assert '<blockquote><code>https://t.me/bot?start=ref_ABC123</code></blockquote>' in text
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert 'referral_list' in callbacks


@pytest.mark.parametrize(
    ('trial_bonus', 'purchase_bonus'),
    [(0, 0), (0, 7), (5, 0)],
)
def test_zero_bonus_terms_are_omitted(monkeypatch, trial_bonus, purchase_bonus):
    monkeypatch.setattr(settings, 'REFERRAL_TRIAL_BONUS_DAYS', trial_bonus)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', purchase_bonus)
    # Оба новых ключа пусты: эффективные значения — общий fallback.
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)

    user = _user(subscriptions=[_sub(SubscriptionStatus.ACTIVE.value)])
    text, _keyboard = build_days_only_referral_screen(
        user=user,
        summary={'invited_count': 0, 'paid_referrals_count': 0, 'total_earned_days': 0},
        bot_referral_link='https://t.me/bot?start=ref_ABC123',
        trial_available=False,
    )

    assert '+0 дней' not in text
    if trial_bonus == 0:
        assert 'к триалу другу' not in text
    else:
        assert f'+{trial_bonus} ' in text
        assert 'к триалу другу' in text
    if purchase_bonus == 0:
        assert 'вам и другу' not in text
        assert 'за первую покупку' not in text
    else:
        assert f'+{purchase_bonus} ' in text
        assert 'вам и другу' in text
    if trial_bonus == 0 and purchase_bonus == 0:
        assert 'За каждого друга' not in text


@pytest.mark.asyncio
async def test_show_referral_info_renders_days_only_screen_as_text(monkeypatch):
    """В режиме логотипа фото-сообщение ломает CTA: экран обязан быть текстовым."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', True)

    captured = {}

    async def fake_summary(db, user_id):
        return {'invited_count': 0, 'paid_referrals_count': 0, 'total_earned_days': 0}

    async def fake_edit(callback, text, keyboard, parse_mode='HTML', *, force_text=False):
        captured['force_text'] = force_text

    monkeypatch.setattr(referral_handler, 'get_user_referral_summary', fake_summary)
    monkeypatch.setattr(referral_handler, 'is_trial_available_for_referral', lambda user: True)
    monkeypatch.setattr(referral_handler, 'edit_or_answer_photo', fake_edit)

    callback = MagicMock()
    callback.bot.get_me = AsyncMock(return_value=SimpleNamespace(username='bot'))
    callback.answer = AsyncMock()

    await referral_handler.show_referral_info(callback, _user(subscriptions=[]), SimpleNamespace())

    assert captured['force_text'] is True
    callback.answer.assert_awaited_once()
