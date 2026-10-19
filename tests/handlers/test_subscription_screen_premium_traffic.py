"""Белые списки на экране «Информация о подписке».

Раньше экран показывал только общий трафик и приписку «— включая белые списки».
Расход по конкретному белому списку виден не был: пользователь со сгоревшей
квотой одного сервера видел «12.3 / 100 ГБ» и не понимал, почему сервер молчит.

Формат повторяет главное меню (``premium_squad_lines`` в ``rich_menu.py``):
строка на каждый подключённый премиум-сквад, а при безлимитном общем трафике
общая строка уходит вовсе — остаются только белые списки.

Тест гоняет настоящий ``show_subscription_info`` с замоканным вводом-выводом.
"""

from contextlib import ExitStack
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


async def _render(subscription, *, tariff=None):
    """Отрисовать экран подписки и вернуть текст сообщения."""
    cb, user = _callback(), _user()
    user.subscription = subscription
    db = MagicMock()
    db.refresh = AsyncMock()
    # Докупленный трафик не предмет проверки — запрос отдаёт пустой список.
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=list))))

    service = MagicMock()
    service.sync_subscription_usage = AsyncMock()
    service.ensure_subscription_synced = AsyncMock(return_value=(True, None))

    patches = [
        patch.object(purchase_mod, 'SubscriptionService', lambda: service),
        patch.object(purchase_mod, 'get_servers_display_names', new=AsyncMock(return_value='')),
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
        patch('app.handlers.subscription.traffic._get_purchasable_premium_squads', new=AsyncMock(return_value=[])),
        patch('app.config.Settings.is_multi_tariff_enabled', lambda self: False),
        patch('app.config.Settings.is_tariffs_mode', lambda self: True),
        patch('app.config.Settings.is_devices_selection_enabled', lambda self: False),
        patch('app.config.Settings.should_hide_subscription_link', lambda self: True),
        patch('app.config.Settings.is_happ_cryptolink_mode', lambda self: False),
    ]

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await purchase_mod.show_subscription_info(cb, user, db)

    return cb.message.edit_text.await_args.args[0]


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
