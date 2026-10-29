from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.handlers.subscription.purchase import activate_trial


def _texts():
    texts = MagicMock()
    texts.t.side_effect = lambda key, default, **kwargs: default
    texts.TRIAL_ACTIVATED = '✅ Триал активирован'
    return texts


@pytest.mark.asyncio
async def test_referral_tail_survives_screen_render_failure(monkeypatch):
    """Сбой перерисовки после успешной активации не должен выглядеть как ошибка триала."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', True)

    callback = MagicMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    callback.bot.get_me = AsyncMock(return_value=SimpleNamespace(username='bot'))

    user = MagicMock()
    user.restriction_subscription = False
    user.auth_type = 'telegram'
    user.language = 'ru'
    user.id = 42
    user.telegram_id = 1001
    user.referral_code = 'ABC123'
    user.is_trial_already_used.return_value = False

    refresh_calls = []

    async def fake_refresh(instance, attribute_names=None):
        refresh_calls.append(attribute_names)
        if attribute_names:
            raise RuntimeError('render failed')

    db = MagicMock()
    db.refresh = AsyncMock(side_effect=fake_refresh)

    with (
        patch('app.services.trial_activation_service.get_trial_activation_charge_amount', return_value=0),
        patch('app.handlers.subscription.purchase.get_texts', return_value=_texts()),
        patch('app.config.Settings.is_trial_disabled_for_user', return_value=False),
        patch('app.config.Settings.is_tariffs_mode', return_value=False),
        patch('app.config.Settings.is_devices_selection_enabled', return_value=True),
        # Импортирован в модуль purchase на верхнем уровне — патчим там.
        patch('app.handlers.subscription.purchase.get_effective_trial_days', return_value=3),
        patch(
            'app.database.crud.server_squad.get_random_trial_squad_uuid',
            new=AsyncMock(return_value='squad-uuid'),
        ),
        patch(
            'app.handlers.subscription.purchase.create_trial_subscription',
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            'app.handlers.subscription.purchase.charge_trial_activation_if_required',
            new=AsyncMock(return_value=0),
        ),
        patch('app.handlers.subscription.purchase.SubscriptionService') as service_cls,
        patch('app.handlers.subscription.purchase.AdminNotificationService') as notify_cls,
        patch('app.handlers.subscription.purchase.get_display_subscription_link', return_value=None),
        patch('app.handlers.subscription.purchase.get_back_keyboard', return_value=MagicMock()),
    ):
        service_cls.return_value.create_remnawave_user = AsyncMock(return_value=MagicMock())
        notify_cls.return_value.send_trial_activation_notification = AsyncMock()

        # Не должно бросить, несмотря на падение db.refresh(['subscriptions']).
        await activate_trial(callback, user, db, from_referral=True)

    # Перерисовка упала до тоста: callback подтверждён без текста.
    callback.answer.assert_awaited_once_with(None)
    assert ['subscriptions'] in refresh_calls
