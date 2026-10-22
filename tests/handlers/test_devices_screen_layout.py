"""Экран устройств: одна кнопка на устройство, докупка внизу.

Было: на каждое устройство две кнопки в ряду (✏️ переименование и 🔄 сброс) —
поэтому кнопка устройства была узкой. Плюс отдельная кнопка «Сбросить все»
и блок инструкций в тексте.

Стало: устройство — одна кнопка во всю ширину, тап = сброс без подтверждения
(так уже работало), докупка устройств последней строкой.

Переименование из UI убрано, но код хендлеров сохранён.
"""

import pytest

from app.keyboards.inline import get_devices_management_keyboard
from app.utils.pagination import paginate_list


def _pagination(count: int, page: int = 1):
    return paginate_list(
        [{'hwid': f'HW{i}', 'platform': 'iOS', 'deviceModel': f'Device {i}'} for i in range(count)],
        page=page,
        per_page=20,
    )


def _rows(keyboard) -> list[list[str]]:
    return [[b.text for b in row] for row in keyboard.inline_keyboard]


def _callbacks(keyboard) -> list[str | None]:
    return [b.callback_data for row in keyboard.inline_keyboard for b in row]


def test_each_device_is_a_full_width_button():
    """Устройство занимает весь ряд — в ряду ровно одна кнопка."""
    keyboard = get_devices_management_keyboard(_pagination(3).items, _pagination(3), 'ru')

    device_rows = [row for row in keyboard.inline_keyboard if row[0].callback_data.startswith('reset_device_')]
    assert len(device_rows) == 3
    for row in device_rows:
        assert len(row) == 1, row


def test_rename_button_is_gone():
    """Кнопки переименования в UI больше нет."""
    keyboard = get_devices_management_keyboard(_pagination(3).items, _pagination(3), 'ru')

    callbacks = _callbacks(keyboard)
    assert not any(cb and cb.startswith('device_rename_') for cb in callbacks), callbacks


def test_reset_all_button_is_gone():
    """Массовый сброс убран — функция не нужна."""
    keyboard = get_devices_management_keyboard(_pagination(3).items, _pagination(3), 'ru')

    assert 'reset_all_devices' not in _callbacks(keyboard)


def test_no_pagination_row_within_the_limit():
    """20 устройств — предел, страниц не появляется."""
    pagination = _pagination(20)
    keyboard = get_devices_management_keyboard(pagination.items, pagination, 'ru')

    callbacks = _callbacks(keyboard)
    assert not any(cb and cb.startswith('devices_page_') for cb in callbacks), callbacks
    device_rows = [row for row in keyboard.inline_keyboard if row[0].callback_data.startswith('reset_device_')]
    assert len(device_rows) == 20


@pytest.mark.parametrize('page', [1, 2])
def test_pagination_still_works_beyond_the_limit(page):
    """21+ устройств (аномалия панели) — не крах, а вторая страница."""
    pagination = paginate_list(
        [{'hwid': f'HW{i}', 'platform': 'iOS', 'deviceModel': f'D{i}'} for i in range(21)],
        page=page,
        per_page=20,
    )
    keyboard = get_devices_management_keyboard(pagination.items, pagination, 'ru')

    callbacks = _callbacks(keyboard)
    assert any(cb and cb.startswith('devices_page_') for cb in callbacks) or any(
        cb == 'current_page' for cb in callbacks
    ), callbacks


def test_back_is_always_last():
    keyboard = get_devices_management_keyboard(_pagination(3).items, _pagination(3), 'ru')

    assert _callbacks(keyboard)[-1] == 'subscription_settings'
