"""Белые списки на экране «Информация о подписке».

Раньше экран показывал только общий трафик и приписку «— включая белые списки».
Расход по конкретному белому списку виден не был: пользователь со сгоревшей
квотой одного сервера видел «12.3 / 100 ГБ» и не понимал, почему сервер молчит.

Формат повторяет главное меню (``premium_squad_lines`` в ``rich_menu.py``):
строка на каждый подключённый премиум-сквад, а при безлимитном общем трафике
общая строка уходит вовсе — остаются только белые списки.

Тест гоняет настоящий ``show_subscription_info`` с замоканным вводом-выводом.
"""

from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.handlers.subscription.purchase as purchase_mod


SQUAD_CH = 'squad-che'
SQUAD_NL = 'squad-nl'
BYTES_IN_GB = 1024**3


def _callback():
    cb = MagicMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _user():
    user = MagicMock()
    user.id = 1
    user.language = 'ru'
    user.balance_kopeks = 0
    user.remnawave_id = 4242
    user.telegram_id = 1001
    user.full_name = 'Test'
    return user


def _subscription(*, limit_gb: int, used_gb: float, squads: list[str] | None = None):
    sub = MagicMock()
    sub.id = 7
    sub.is_trial = False
    sub.status = 'active'
    sub.actual_status = 'active'
    sub.tariff_id = 5
    sub.traffic_limit_gb = limit_gb
    sub.traffic_used_gb = used_gb
    sub.device_limit = 3
    sub.remnawave_id = 4242
    sub.remnawave_short_uuid = 'short'
    sub.connected_squads = [SQUAD_CH, SQUAD_NL] if squads is None else squads
    sub.subscription_url = None
    sub.end_date = datetime.now(UTC) + timedelta(days=13)
    return sub


def _tariff():
    """Тариф с двумя белыми списками по 5 ГБ."""
    return SimpleNamespace(
        id=5,
        name='Тест',
        is_daily=False,
        traffic_limit_gb=100,
        device_limit=3,
        can_topup_traffic=lambda: False,
        server_traffic_limits={
            SQUAD_CH: {'traffic_limit_gb': 5},
            SQUAD_NL: {'traffic_limit_gb': 5},
        },
    )


def _states():
    return [
        SimpleNamespace(squad_uuid=SQUAD_CH, used_bytes=2 * BYTES_IN_GB, total_limit_bytes=5 * BYTES_IN_GB),
        SimpleNamespace(squad_uuid=SQUAD_NL, used_bytes=1 * BYTES_IN_GB, total_limit_bytes=5 * BYTES_IN_GB),
    ]


@asynccontextmanager
async def _remnawave_client(total: int, recorded_ids: list[int] | None = None):
    async def _get_user_devices(user_id: int):
        if recorded_ids is not None:
            recorded_ids.append(user_id)
        return {'total': total, 'devices': []}

    api = SimpleNamespace(get_user_devices=AsyncMock(side_effect=_get_user_devices))
    yield api


class _FakeRemnawaveService:
    def __init__(self, total: int):
        self.total = total
        self.device_ids: list[int] = []

    def get_api_client(self):
        return _remnawave_client(self.total, self.device_ids)


async def _render(
    subscription,
    *,
    tariff=None,
    hide_link: bool = True,
    show_devices: bool = False,
    devices_total: int = 0,
    return_keyboard: bool = False,
    return_service: bool = False,
    snapshot_user=None,
    snapshot_devices=None,
    snapshot_error: Exception | None = None,
    devices_service=None,
    user_remnawave_id: int | None = 4242,
    sync_side_effect=None,
):
    """Отрисовать экран подписки и вернуть текст (и клавиатуру/сервис, если нужно)."""
    cb, user = _callback(), _user()
    user.remnawave_id = user_remnawave_id
    user.subscription = subscription
    db = MagicMock()
    db.refresh = AsyncMock()
    # Докупленный трафик не предмет проверки — запрос отдаёт пустой список.
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=list))))

    service = MagicMock()
    service.sync_subscription_usage = AsyncMock()
    service.ensure_subscription_synced = AsyncMock(return_value=(True, None))
    service.apply_panel_user_usage = AsyncMock()
    service.get_panel_snapshot = AsyncMock(return_value=(snapshot_user, snapshot_devices))
    if snapshot_error is not None:
        service.get_panel_snapshot.side_effect = snapshot_error
    if sync_side_effect is not None:

        async def _sync_side_effect(db, *args, **kwargs):
            return sync_side_effect(user, subscription)

        service.ensure_subscription_synced.side_effect = _sync_side_effect

    if devices_service is None:
        devices_service = _FakeRemnawaveService(devices_total)

    patches = [
        patch.object(purchase_mod, 'SubscriptionService', lambda: service),
        patch(
            'app.database.crud.subscription.check_and_update_subscription_status',
            new=AsyncMock(return_value=subscription),
        ),
        patch('app.database.crud.tariff.get_tariff_by_id', new=AsyncMock(return_value=tariff)),
        patch('app.database.crud.premium_traffic.get_states_for_subscription', new=AsyncMock(return_value=_states())),
        patch(
            'app.database.crud.server_squad.get_squad_display_names',
            new=AsyncMock(return_value={SQUAD_CH: 'Швейцария', SQUAD_NL: 'Нидерланды'}),
        ),
        patch('app.services.remnawave_service.RemnaWaveService', lambda: devices_service),
        patch('app.handlers.subscription.traffic._get_purchasable_premium_squads', new=AsyncMock(return_value=[])),
        patch('app.config.Settings.is_multi_tariff_enabled', lambda self: False),
        patch('app.config.Settings.is_tariffs_mode', lambda self: True),
        patch('app.config.Settings.is_devices_selection_enabled', lambda self: show_devices),
        patch('app.config.Settings.should_hide_subscription_link', lambda self: hide_link),
        patch('app.config.Settings.is_happ_cryptolink_mode', lambda self: False),
    ]

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await purchase_mod.show_subscription_info(cb, user, db)

    rendered = cb.message.edit_text.await_args.args[0]
    if return_service:
        return rendered, service
    if return_keyboard:
        return rendered, cb.message.edit_text.await_args.kwargs['reply_markup']
    return rendered


def _squad_lines(text: str) -> list[str]:
    """Строки белых списков: по одной на подключённый премиум-сквад."""
    return [line for line in text.splitlines() if '💠' in line]


@pytest.mark.anyio('asyncio')
async def test_screen_shows_a_line_per_connected_whitelist():
    """Белые списки видны отдельными строками со своим расходом."""
    text = await _render(_subscription(limit_gb=100, used_gb=12.3), tariff=_tariff())

    lines = _squad_lines(text)
    assert len(lines) == 2, text
    # Каждый сквад подписан своим именем и своим расходом (2 ГБ и 1 ГБ).
    assert 'Швейцария' in lines[0] and '2 ГБ' in lines[0], lines
    assert 'Нидерланды' in lines[1] and '1 ГБ' in lines[1], lines


@pytest.mark.anyio('asyncio')
async def test_unlimited_overall_hides_the_total_line_but_keeps_whitelists():
    """Безлимитный общий трафик при белых списках — общая строка уходит."""
    text = await _render(_subscription(limit_gb=0, used_gb=50.0), tariff=_tariff())

    assert 'безлимит' not in text.lower(), text
    assert len(_squad_lines(text)) == 2, text


@pytest.mark.anyio('asyncio')
async def test_no_whitelists_leaves_the_total_line():
    """Без подключённых белых списков общая строка остаётся, как раньше."""
    sub = _subscription(limit_gb=100, used_gb=12.3, squads=[])
    text = await _render(sub, tariff=_tariff())

    assert not _squad_lines(text), text
    assert '12.3' in text and '100' in text, text


@pytest.mark.anyio('asyncio')
async def test_exhausted_general_limit_hides_numbers_and_whitelists():
    """Общий лимит выбран: вместо цифр и сквадов — одна причина."""
    text = await _render(_subscription(limit_gb=12, used_gb=32.7), tariff=_tariff())

    assert 'Лимит трафика исчерпан — белые списки тоже недоступны' in text
    assert '32.7' not in text
    assert not _squad_lines(text), text


@pytest.mark.anyio('asyncio')
async def test_active_subscription_link_renders_without_name_error():
    """Ссылка подписки требует локальный actual_status — регрессия после отката тела."""
    sub = _subscription(limit_gb=100, used_gb=1.0)
    sub.subscription_url = 'https://example.test/sub'

    text = await _render(sub, tariff=_tariff(), hide_link=False)

    assert 'https://example.test/sub' in text


@pytest.mark.anyio('asyncio')
async def test_expired_screen_passes_devices_over_limit_to_keyboard():
    """3 / 1 на истёкшей подписке должен оставлять кнопку «Устройства»."""
    sub = _subscription(limit_gb=100, used_gb=1.0)
    sub.status = 'expired'
    sub.actual_status = 'expired'
    sub.device_limit = 1

    _, keyboard = await _render(sub, tariff=_tariff(), show_devices=True, devices_total=3, return_keyboard=True)
    callbacks = {button.callback_data for row in keyboard.inline_keyboard for button in row}

    assert 'subscription_manage_devices' in callbacks


@pytest.mark.anyio('asyncio')
async def test_expired_screen_hides_devices_without_overrun():
    sub = _subscription(limit_gb=100, used_gb=1.0)
    sub.status = 'expired'
    sub.actual_status = 'expired'
    sub.device_limit = 1

    _, keyboard = await _render(sub, tariff=_tariff(), show_devices=True, devices_total=1, return_keyboard=True)
    callbacks = {button.callback_data for row in keyboard.inline_keyboard for button in row}

    assert 'subscription_manage_devices' not in callbacks


@pytest.mark.anyio('asyncio')
async def test_snapshot_skips_usage_sync_and_devices_fetch():
    """Снимок дал пользователя и устройства — повторных запросов в панель нет."""
    sub = _subscription(limit_gb=100, used_gb=12.3)
    panel_user = SimpleNamespace(used_traffic_bytes=5 * BYTES_IN_GB)
    devices_service = MagicMock()

    text, service = await _render(
        sub,
        tariff=_tariff(),
        show_devices=True,
        snapshot_user=panel_user,
        snapshot_devices={'total': 2, 'devices': []},
        devices_service=devices_service,
        return_service=True,
    )

    service.get_panel_snapshot.assert_awaited_once_with(4242)
    service.sync_subscription_usage.assert_not_awaited()
    service.apply_panel_user_usage.assert_awaited_once()
    assert service.apply_panel_user_usage.await_args.args[2] is panel_user
    assert service.ensure_subscription_synced.await_args.kwargs == {
        'panel_user': panel_user,
        'panel_user_loaded': True,
    }
    # Устройства из снимка — отдельного GET в панель нет.
    devices_service.get_api_client.assert_not_called()
    assert '2 / 3' in text, text


@pytest.mark.anyio('asyncio')
async def test_snapshot_failure_falls_back_to_legacy_panel_calls():
    """Панель не отдала снимок — экран работает по старому пути из трёх запросов."""
    sub = _subscription(limit_gb=100, used_gb=12.3)

    text, service = await _render(
        sub,
        tariff=_tariff(),
        show_devices=True,
        devices_total=3,
        snapshot_error=RuntimeError('panel down'),
        return_service=True,
    )

    service.get_panel_snapshot.assert_awaited_once_with(4242)
    service.sync_subscription_usage.assert_awaited_once()
    service.apply_panel_user_usage.assert_not_awaited()
    assert service.ensure_subscription_synced.await_args.kwargs == {
        'panel_user': None,
        'panel_user_loaded': False,
    }
    assert '3 / 3' in text, text


@pytest.mark.anyio('asyncio')
async def test_devices_fallback_uses_panel_id_assigned_by_sync():
    """Синхронизация завела панельный аккаунт — устройства берём по новому id.

    До синхронизации `db_user.remnawave_id` пуст, поэтому снимок не запрашивался
    и `devices_info` нет. Если бы блок устройств использовал pre-sync id, запрос
    не выполнился бы вовсе и строка осталась бы пустой («Устройства:  / 3»)
    на том самом рендере, который починил аккаунт.
    """
    new_panel_id = 7777
    sub = _subscription(limit_gb=100, used_gb=12.3)
    devices_service = _FakeRemnawaveService(0)

    def _assign_panel_id(user, subscription):
        user.remnawave_id = new_panel_id
        subscription.remnawave_id = new_panel_id
        return (True, None)

    text = await _render(
        sub,
        tariff=_tariff(),
        show_devices=True,
        user_remnawave_id=None,
        sync_side_effect=_assign_panel_id,
        devices_service=devices_service,
    )

    assert devices_service.device_ids == [new_panel_id]
    assert '0 / 3' in text, text
