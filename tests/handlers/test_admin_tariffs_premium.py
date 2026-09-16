"""Тесты экрана премиум-лимитов трафика в админке бота (Task 10).

Контракт задачи: разбор ввода, отказ на мусоре, сохранение с сохранением
чужих полей сквада (слияние по ключу, а не замена карты целиком), и то, что
ноль в лимите не читается и не подаётся как «доступ закрыт».
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.handlers.admin.tariffs as tariffs_mod


SQUAD_A = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
SQUAD_B = 'a1a71cd1-30e0-4b0a-9b28-2c9b6e0a11aa'


def _unwrap(fn):
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    return fn


def _tariff(**overrides):
    values = {
        'id': 7,
        'name': 'Whitelist <test>',
        'server_traffic_limits': {},
        'is_active': True,
        'is_trial_available': False,
        'is_daily': False,
        'is_highlighted': False,
        'period_prices': {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _callback(data: str):
    callback = MagicMock()
    callback.data = data
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def _message(text: str | None):
    message = MagicMock()
    message.text = text
    message.answer = AsyncMock()
    return message


def _state(tariff_id: int = 7, squad_uuid: str = SQUAD_A):
    state = MagicMock()
    state.get_data = AsyncMock(return_value={'tariff_id': tariff_id, 'squad_uuid': squad_uuid, 'language': 'ru'})
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()
    state.clear = AsyncMock()
    return state


def _db_user():
    return SimpleNamespace(language='ru')


def _fake_update_tariff(tariff):
    """Имитирует слияние по ключу сквада, как это делает настоящий update_tariff."""

    async def fake_update(db, target, **kwargs):
        limits = kwargs.pop('server_traffic_limits', None)
        if limits is not None:
            merged = dict(target.server_traffic_limits or {})
            merged.update(limits)
            target.server_traffic_limits = merged
        for key, value in kwargs.items():
            setattr(target, key, value)
        return target

    return fake_update


# ---------------------------- Разбор пакетов докупки ----------------------------


def test_parse_premium_packages_accepts_free_package():
    """В отличие от обычной докупки, премиум-докупка допускает бесплатный пакет."""
    packages = tariffs_mod._parse_premium_squad_topup_packages('5:0, 10:9000')

    assert packages == {5: 0, 10: 9000}


def test_parse_premium_packages_rejects_negative_price():
    packages = tariffs_mod._parse_premium_squad_topup_packages('5:-100')

    assert packages == {}


def test_parse_premium_packages_ignores_garbage_tokens():
    packages = tariffs_mod._parse_premium_squad_topup_packages('avocado, 10:9000, :5000, 0:100')

    assert packages == {10: 9000}


# ---------------------------- Ноль ГБ != доступ закрыт ----------------------------


def test_zero_limit_formats_as_no_separate_limit_not_closed_access():
    rendered = tariffs_mod._format_premium_squad_limit(0)

    assert 'закры' not in rendered.lower()
    assert 'без отдельного лимита' in rendered.lower()


def test_premium_squad_screen_reads_through_shared_parser_not_raw_dict():
    """Три исторические формы должны читаться одинаково через parse_premium_squads."""
    tariff = _tariff(server_traffic_limits={SQUAD_A: 5})
    rendered = tariffs_mod.format_premium_squad_settings(tariff, SQUAD_A, server_name='DE-1')

    assert '5 ГБ' in rendered


def test_premium_squad_screen_marks_zero_limit_as_not_premium_and_never_closed():
    tariff = _tariff(server_traffic_limits={SQUAD_A: {'traffic_limit_gb': 0, 'topup_enabled': True}})
    rendered = tariffs_mod.format_premium_squad_settings(tariff, SQUAD_A, server_name='DE-1')

    assert 'без отдельного лимита' in rendered.lower()
    assert 'закры' not in rendered.lower()


# ---------------------------- Кнопка топ-апа при выключенном рубильнике ----------------------------


def test_topup_status_does_not_imply_availability_when_kill_switch_is_off(monkeypatch):
    monkeypatch.setattr(tariffs_mod.settings, 'PREMIUM_TRAFFIC_ENABLED', False, raising=False)
    tariff = _tariff(
        server_traffic_limits={
            SQUAD_A: {
                'traffic_limit_gb': 10,
                'topup_enabled': True,
                'topup_packages': {'5': 5000},
            }
        }
    )

    rendered = tariffs_mod.format_premium_squad_settings(tariff, SQUAD_A, server_name='DE-1')

    assert 'отключена глобально' in rendered.lower()


# ---------------------------- Сохранение: слияние по ключу сквада ----------------------------


async def test_saving_limit_for_one_squad_preserves_other_squad_settings(monkeypatch):
    """Партиальное обновление не должно стирать настройки другого сквада."""
    tariff = _tariff(
        server_traffic_limits={
            SQUAD_A: {
                'traffic_limit_gb': 1,
                'name': 'Мобильный',
                'topup_enabled': True,
                'topup_packages': {'5': 1000},
            },
            SQUAD_B: {'traffic_limit_gb': 20, 'name': 'Резерв', 'topup_packages': {'5': 500}},
        }
    )
    message = _message('15')
    state = _state(tariff_id=7, squad_uuid=SQUAD_A)

    monkeypatch.setattr(tariffs_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(tariffs_mod, 'update_tariff', _fake_update_tariff(tariff))
    monkeypatch.setattr(tariffs_mod, 'get_server_squad_by_uuid', AsyncMock(return_value=None))

    await _unwrap(tariffs_mod.process_edit_premium_squad_limit)(message, _db_user(), MagicMock(), state)

    assert tariff.server_traffic_limits[SQUAD_A]['traffic_limit_gb'] == 15
    # Правка лимита не должна стереть остальные поля ЭТОГО ЖЕ сквада —
    # `update_tariff` сливает карту по ключу сквада целиком, а не по полям
    # внутри записи, так что при записи мы обязаны сами сохранить остальное.
    assert tariff.server_traffic_limits[SQUAD_A]['name'] == 'Мобильный'
    assert tariff.server_traffic_limits[SQUAD_A]['topup_packages'] == {'5': 1000}
    # Сосед не пострадал.
    assert tariff.server_traffic_limits[SQUAD_B]['traffic_limit_gb'] == 20
    assert tariff.server_traffic_limits[SQUAD_B]['name'] == 'Резерв'
    assert tariff.server_traffic_limits[SQUAD_B]['topup_packages'] == {'5': 500}
    state.clear.assert_awaited_once()


async def test_saving_packages_preserves_own_squads_limit_and_name(monkeypatch):
    """Правка одного поля сквада не должна стирать остальные поля того же сквада."""
    tariff = _tariff(server_traffic_limits={SQUAD_A: {'traffic_limit_gb': 30, 'name': 'Мобильный', 'sort_order': 2}})
    message = _message('5:1000, 10:1800')
    state = _state(tariff_id=7, squad_uuid=SQUAD_A)

    monkeypatch.setattr(tariffs_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(tariffs_mod, 'update_tariff', _fake_update_tariff(tariff))
    monkeypatch.setattr(tariffs_mod, 'get_server_squad_by_uuid', AsyncMock(return_value=None))

    await _unwrap(tariffs_mod.process_edit_premium_squad_topup_packages)(message, _db_user(), MagicMock(), state)

    record = tariff.server_traffic_limits[SQUAD_A]
    assert record['topup_packages'] == {'5': 1000, '10': 1800}
    assert record['traffic_limit_gb'] == 30
    assert record['name'] == 'Мобильный'
    assert record['sort_order'] == 2


async def test_editing_other_field_after_zeroing_limit_preserves_stored_name_and_packages(monkeypatch):
    """Обнуление лимита не должно стирать остальные поля при следующей правке.

    `parse_premium_squad` намеренно отбрасывает записи с limit_gb <= 0 (сквад
    временно не премиумный) — это верно для воркера и кабинета, которым нужен
    только список *активных* премиум-сквадов. Но экран редактирования должен
    видеть полную хранимую запись даже для временно обнулённого сквада: иначе
    правка любого другого поля перезапишет её синтетической пустышкой и молча
    сотрёт имя/пакеты/сортировку, которые оператор уже настроил.
    """
    tariff = _tariff(
        server_traffic_limits={
            SQUAD_A: {
                'traffic_limit_gb': 0,
                'name': 'Мобильный',
                'sort_order': 3,
                'topup_packages': {'5': 1000},
                'max_topup_gb': 20,
            }
        }
    )
    message = _message('7')
    state = _state(tariff_id=7, squad_uuid=SQUAD_A)

    monkeypatch.setattr(tariffs_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(tariffs_mod, 'update_tariff', _fake_update_tariff(tariff))
    monkeypatch.setattr(tariffs_mod, 'get_server_squad_by_uuid', AsyncMock(return_value=None))

    # Правим порядок сортировки — лимит трогать не должны, но squad всё ещё
    # «не премиумный» (limit_gb == 0) в момент чтения текущей конфигурации.
    await _unwrap(tariffs_mod.process_edit_premium_squad_sort_order)(message, _db_user(), MagicMock(), state)

    record = tariff.server_traffic_limits[SQUAD_A]
    assert record['sort_order'] == 7
    assert record['traffic_limit_gb'] == 0
    assert record['name'] == 'Мобильный'
    assert record['topup_packages'] == {'5': 1000}
    assert record['max_topup_gb'] == 20


async def test_editing_other_field_after_zeroing_limit_preserves_legacy_shape_without_limit_key(monkeypatch):
    """То же самое для исторической формы записи без ключа `traffic_limit_gb`."""
    tariff = _tariff(
        server_traffic_limits={
            SQUAD_A: {
                'name': 'Резервный',
                'topup_packages': {'10': 2000},
            }
        }
    )
    message = _message('Обновлённое имя')
    state = _state(tariff_id=7, squad_uuid=SQUAD_A)

    monkeypatch.setattr(tariffs_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(tariffs_mod, 'update_tariff', _fake_update_tariff(tariff))
    monkeypatch.setattr(tariffs_mod, 'get_server_squad_by_uuid', AsyncMock(return_value=None))

    await _unwrap(tariffs_mod.process_edit_premium_squad_name)(message, _db_user(), MagicMock(), state)

    record = tariff.server_traffic_limits[SQUAD_A]
    assert record['name'] == 'Обновлённое имя'
    assert record['topup_packages'] == {'10': 2000}


async def test_removing_squad_from_premium_set_zeroes_limit_key_stays(monkeypatch):
    """Снятие премиума со сквада — это лимит=0, а не удаление ключа из карты."""
    tariff = _tariff(server_traffic_limits={SQUAD_A: {'traffic_limit_gb': 50}})
    message = _message('0')
    state = _state(tariff_id=7, squad_uuid=SQUAD_A)

    monkeypatch.setattr(tariffs_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(tariffs_mod, 'update_tariff', _fake_update_tariff(tariff))
    monkeypatch.setattr(tariffs_mod, 'get_server_squad_by_uuid', AsyncMock(return_value=None))

    await _unwrap(tariffs_mod.process_edit_premium_squad_limit)(message, _db_user(), MagicMock(), state)

    assert SQUAD_A in tariff.server_traffic_limits
    assert tariff.server_traffic_limits[SQUAD_A]['traffic_limit_gb'] == 0


# ---------------------------- Отказ на мусоре ----------------------------


async def test_garbage_limit_input_is_rejected_without_write(monkeypatch):
    tariff = _tariff(server_traffic_limits={SQUAD_A: {'traffic_limit_gb': 10}})
    message = _message('много')
    state = _state(tariff_id=7, squad_uuid=SQUAD_A)
    update = AsyncMock()

    monkeypatch.setattr(tariffs_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(tariffs_mod, 'update_tariff', update)

    await _unwrap(tariffs_mod.process_edit_premium_squad_limit)(message, _db_user(), MagicMock(), state)

    update.assert_not_awaited()
    state.clear.assert_not_awaited()
    assert 'некорректное значение' in message.answer.await_args.args[0].lower()


async def test_negative_limit_input_is_rejected_without_write(monkeypatch):
    tariff = _tariff(server_traffic_limits={SQUAD_A: {'traffic_limit_gb': 10}})
    message = _message('-5')
    state = _state(tariff_id=7, squad_uuid=SQUAD_A)
    update = AsyncMock()

    monkeypatch.setattr(tariffs_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(tariffs_mod, 'update_tariff', update)

    await _unwrap(tariffs_mod.process_edit_premium_squad_limit)(message, _db_user(), MagicMock(), state)

    update.assert_not_awaited()


async def test_all_garbage_packages_input_is_rejected_without_write(monkeypatch):
    tariff = _tariff(server_traffic_limits={SQUAD_A: {'traffic_limit_gb': 10}})
    message = _message('совсем не то')
    state = _state(tariff_id=7, squad_uuid=SQUAD_A)
    update = AsyncMock()

    monkeypatch.setattr(tariffs_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(tariffs_mod, 'update_tariff', update)

    await _unwrap(tariffs_mod.process_edit_premium_squad_topup_packages)(message, _db_user(), MagicMock(), state)

    update.assert_not_awaited()
    state.clear.assert_not_awaited()


async def test_overlong_name_input_is_rejected_without_write(monkeypatch):
    tariff = _tariff(server_traffic_limits={SQUAD_A: {'traffic_limit_gb': 10}})
    message = _message('x' * 65)
    state = _state(tariff_id=7, squad_uuid=SQUAD_A)
    update = AsyncMock()

    monkeypatch.setattr(tariffs_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(tariffs_mod, 'update_tariff', update)

    await _unwrap(tariffs_mod.process_edit_premium_squad_name)(message, _db_user(), MagicMock(), state)

    update.assert_not_awaited()
    state.clear.assert_not_awaited()


# ---------------------------- Тумблер докупки против пустых пакетов ----------------------------


async def test_enabling_topup_without_packages_is_rejected(monkeypatch):
    tariff = _tariff(server_traffic_limits={SQUAD_A: {'traffic_limit_gb': 10}})
    callback = _callback(f'trf_psq_topup:7:{SQUAD_A}')
    update = AsyncMock()

    monkeypatch.setattr(tariffs_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(tariffs_mod, 'update_tariff', update)

    await _unwrap(tariffs_mod.toggle_premium_squad_topup)(callback, _db_user(), MagicMock())

    update.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get('show_alert') is True


async def test_toggle_topup_persists_only_topup_flag_when_packages_exist(monkeypatch):
    tariff = _tariff(
        server_traffic_limits={SQUAD_A: {'traffic_limit_gb': 10, 'topup_enabled': False, 'topup_packages': {'5': 1000}}}
    )
    callback = _callback(f'trf_psq_topup:7:{SQUAD_A}')

    monkeypatch.setattr(tariffs_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(tariffs_mod, 'update_tariff', _fake_update_tariff(tariff))
    monkeypatch.setattr(tariffs_mod, 'get_server_squad_by_uuid', AsyncMock(return_value=None))

    await _unwrap(tariffs_mod.toggle_premium_squad_topup)(callback, _db_user(), MagicMock())

    record = tariff.server_traffic_limits[SQUAD_A]
    assert record['topup_enabled'] is True
    assert record['traffic_limit_gb'] == 10
    assert record['topup_packages'] == {'5': 1000}


# ---------------------------- Клавиатуры / навигация ----------------------------


def test_tariff_view_keyboard_has_premium_squads_entry_point():
    tariff = _tariff(is_active=True, is_trial_available=False, is_daily=False, is_highlighted=False, tier_level=1)
    callbacks = [
        button.callback_data
        for row in tariffs_mod.get_tariff_view_keyboard(tariff, 'ru').inline_keyboard
        for button in row
    ]

    assert 'admin_tariff_premium_squads:7' in callbacks


def test_premium_squad_keyboard_uses_short_callback_data_within_telegram_limit():
    keyboard = tariffs_mod.get_premium_squad_keyboard(7, SQUAD_A, 'ru')

    for row in keyboard.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode('utf-8')) <= 64
