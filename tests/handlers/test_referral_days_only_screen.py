from types import SimpleNamespace

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


def _sub(status: str, *, trial: bool = False):
    return SimpleNamespace(status=status, is_trial=trial)


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
    assert 'при наличии подписки' in text
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
