"""Пользовательская докупка премиум-трафика в боте (Task 12): деньги и отказы.

Экран продаёт разрешение сервиса (`premium_traffic_purchase`), а не собственное
мнение о том, можно ли купить. Три гарантии проверяются здесь:

1) рубильник ``PREMIUM_TRAFFIC_ENABLED`` обязан ОТКАЗЫВАТЬ, а не молчать:
   кнопку нельзя ни показать, ни довести нажатие до списания;
2) сквад, закрытый администратором (``closed_at``), не должен даже
   предлагаться к докупке — а если между отрисовкой экрана и нажатием кнопки
   админ успел закрыть доступ, повторная проверка обязана поймать это до
   списания;
3) потолок докупки (``max_topup_gb``) здесь только отображается — решает
   сервис под блокировкой строки (Task 4), экран не повторяет эту проверку на
   своих, потенциально устаревших, данных.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

import app.handlers.subscription.traffic as traffic_mod
from app.config import settings
from app.database.crud.premium_traffic import get_or_create_state, get_state
from app.database.models import ServerSquad, SubscriptionPremiumTraffic, User
from app.keyboards.inline import get_add_traffic_keyboard, get_add_traffic_keyboard_from_tariff
from app.utils.premium_traffic import BYTES_IN_GB, PremiumSquadConfig
from tests.fixtures.sqlite_memory import memory_session


TABLES = (SubscriptionPremiumTraffic.__table__, ServerSquad.__table__, User.__table__)

SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
OTHER = '82a12389-14d6-40c6-b320-4674f6bbb344'
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

WITH_TOPUP = {
    'traffic_limit_gb': 5,
    'name': 'DE-1',
    'topup_enabled': True,
    'topup_packages': {'1': 500, '5': 2000},
    'max_topup_gb': 10,
}


def _subscription(limits=None, connected=(SQUAD,), subscription_id=1, is_trial=False):
    return SimpleNamespace(
        id=subscription_id,
        connected_squads=list(connected),
        tariff=SimpleNamespace(server_traffic_limits=limits if limits is not None else {SQUAD: WITH_TOPUP}),
        is_trial=is_trial,
    )


def _callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.from_user = SimpleNamespace(id=1)
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def _db_user(balance_kopeks: int = 1_000_000) -> SimpleNamespace:
    return SimpleNamespace(id=999, telegram_id=999, language='ru', balance_kopeks=balance_kopeks)


def _patch_resolve(monkeypatch, subscription, sub_id=1):
    async def _fake(callback, db_user, db, state=None):
        return subscription, sub_id

    monkeypatch.setattr(traffic_mod, '_resolve_subscription', _fake)


def _refused_with_alert(callback) -> None:
    """Общая проверка отказа: одно предупреждение, экран не редактируется на «покупку»."""
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get('show_alert') is True
    callback.message.edit_text.assert_not_awaited()


# ============================= Список предложений =============================


class TestPurchasableSquads:
    """`_get_purchasable_premium_squads` — единственный источник того, что показывает экран."""

    async def test_open_squad_with_topup_is_offered(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            rows = await traffic_mod._get_purchasable_premium_squads(db, _subscription())

            assert [row['squad_uuid'] for row in rows] == [SQUAD]

    async def test_kill_switch_hides_the_offer(self, monkeypatch):
        """Рубильник выключен — сквад с полностью настроенной докупкой всё равно не предлагается."""
        monkeypatch.setattr(settings, 'PREMIUM_TRAFFIC_ENABLED', False)
        async with memory_session(monkeypatch, TABLES) as db:
            rows = await traffic_mod._get_purchasable_premium_squads(db, _subscription())

            assert rows == []

    async def test_closed_squad_is_not_offered(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.closed_at = NOW
            state.is_limited = True
            await db.commit()

            rows = await traffic_mod._get_purchasable_premium_squads(db, _subscription())

            assert rows == []

    async def test_reopened_pending_squad_is_still_offered(self, monkeypatch):
        """closed_at снят явным открытием — это НЕ «закрыто», докупка должна остаться доступной.

        Отличать от EXHAUSTED/CLOSED (Task 11) — только ``closed_at`` решает,
        не ``is_limited`` сам по себе.
        """
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.is_limited = True  # ещё не возвращён воркером в панель, но closed_at нет
            await db.commit()

            rows = await traffic_mod._get_purchasable_premium_squads(db, _subscription())

            assert [row['squad_uuid'] for row in rows] == [SQUAD]


# ============================= Видимость кнопки =============================


class TestButtonVisibility:
    """Кнопка «Премиум-трафик» — прямое следствие ``has_premium_topup``, не отдельная проверка рубильника."""

    def test_premium_button_present_when_offered(self):
        keyboard = get_add_traffic_keyboard('ru', has_premium_topup=True)
        texts = [button.text for row in keyboard.inline_keyboard for button in row]

        assert any('Премиум-трафик' in text for text in texts)

    def test_premium_button_absent_when_switch_disabled(self):
        keyboard = get_add_traffic_keyboard('ru', has_premium_topup=False)
        texts = [button.text for row in keyboard.inline_keyboard for button in row]

        assert not any('Премиум-трафик' in text for text in texts)

    def test_tariff_keyboard_premium_button_absent_when_switch_disabled(self):
        keyboard = get_add_traffic_keyboard_from_tariff('ru', {5: 1000}, has_premium_topup=False)
        texts = [button.text for row in keyboard.inline_keyboard for button in row]

        assert not any('Премиум-трафик' in text for text in texts)


# ============================= Точка входа =============================


class TestTopupEntryPoint:
    """`handle_premium_traffic_topup` обязан ОТКАЗАТЬ, а не просто промолчать."""

    async def test_refuses_when_switch_disabled(self, monkeypatch):
        subscription = _subscription()
        _patch_resolve(monkeypatch, subscription)
        monkeypatch.setattr(settings, 'PREMIUM_TRAFFIC_ENABLED', False)

        callback = _callback('premium_traffic_topup')

        # options пуст ещё до похода в БД (см. get_premium_topup_options) —
        # реального db не требуется.
        await traffic_mod.handle_premium_traffic_topup(callback, _db_user(), db=object())

        _refused_with_alert(callback)

    async def test_refuses_when_only_squad_is_closed(self, monkeypatch):
        subscription = _subscription()
        _patch_resolve(monkeypatch, subscription)
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.closed_at = NOW
            await db.commit()

            callback = _callback('premium_traffic_topup')

            await traffic_mod.handle_premium_traffic_topup(callback, _db_user(), db)

            _refused_with_alert(callback)

    async def test_opens_packages_directly_for_a_single_open_squad(self, monkeypatch):
        """Контроль: доступный сквад — экран действительно открывается, тесты выше не тривиальны."""
        subscription = _subscription()
        _patch_resolve(monkeypatch, subscription)
        async with memory_session(monkeypatch, TABLES) as db:
            callback = _callback('premium_traffic_topup')

            await traffic_mod.handle_premium_traffic_topup(callback, _db_user(), db)

            callback.message.edit_text.assert_awaited_once()
            markup = callback.message.edit_text.await_args.kwargs['reply_markup']
            callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
            assert any(cb.startswith('premium_traffic_buy_0_') for cb in callbacks)


# ============================= Выбор сервера =============================


class TestSquadSelection:
    async def test_rejects_stale_index_when_squad_closed_meanwhile(self, monkeypatch):
        """Индекс считался по старому списку — сквад под ним закрыли, пока пользователь смотрел на экран."""
        limits = {SQUAD: WITH_TOPUP, OTHER: {**WITH_TOPUP, 'name': 'FR-1'}}
        subscription = _subscription(limits=limits, connected=(SQUAD, OTHER))
        _patch_resolve(monkeypatch, subscription)
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, OTHER, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.closed_at = NOW
            await db.commit()

            # Индекс 1 раньше указывал на OTHER — теперь список короче на один.
            callback = _callback('premium_traffic_squad_1')

            await traffic_mod.handle_premium_traffic_squad(callback, _db_user(), db)

            _refused_with_alert(callback)


# ============================= Покупка: отказ без списания =============================


class TestBuyRefusesWithoutCharging:
    """Money-критичные отказы: списания быть не должно вообще, а не «списали и потом откатили»."""

    async def test_kill_switch_refuses_before_any_charge(self, monkeypatch):
        subscription = _subscription()
        _patch_resolve(monkeypatch, subscription)
        monkeypatch.setattr(settings, 'PREMIUM_TRAFFIC_ENABLED', False)

        subtract = AsyncMock()
        monkeypatch.setattr(traffic_mod, 'subtract_user_balance', subtract)

        callback = _callback('premium_traffic_buy_0_5')

        await traffic_mod.buy_premium_traffic(callback, _db_user(), db=object())

        subtract.assert_not_awaited()
        _refused_with_alert(callback)

    async def test_closed_squad_refuses_before_any_charge(self, monkeypatch):
        """Обычный путь: сквад закрыт — его вообще нет в списке, индекс сразу невалиден."""
        subscription = _subscription()
        _patch_resolve(monkeypatch, subscription)

        subtract = AsyncMock()
        monkeypatch.setattr(traffic_mod, 'subtract_user_balance', subtract)

        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.closed_at = NOW
            await db.commit()

            callback = _callback('premium_traffic_buy_0_5')

            await traffic_mod.buy_premium_traffic(callback, _db_user(), db)

            subtract.assert_not_awaited()
            _refused_with_alert(callback)

    async def test_closed_squad_refuses_even_when_the_rendered_list_is_stale(self, monkeypatch):
        """Гонка: список построили ДО закрытия, нажатие пришло ПОСЛЕ.

        `_get_purchasable_premium_squads` подделана так, будто список ещё не
        знает о закрытии (ровно то, что видел бы пользователь на уже
        отрисованном экране) — единственная защита здесь — перечитывание
        состояния прямо перед списанием.
        """
        subscription = _subscription()
        _patch_resolve(monkeypatch, subscription)

        stale_row = {
            'squad_uuid': SQUAD,
            'config': PremiumSquadConfig(squad_uuid=SQUAD, limit_gb=5, topup_enabled=True, topup_packages={5: 2000}),
            'state': None,
            'name': 'DE-1',
        }

        async def _stale_rows(db, subscription):
            return [stale_row]

        monkeypatch.setattr(traffic_mod, '_get_purchasable_premium_squads', _stale_rows)

        subtract = AsyncMock()
        monkeypatch.setattr(traffic_mod, 'subtract_user_balance', subtract)

        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.closed_at = NOW
            await db.commit()

            callback = _callback('premium_traffic_buy_0_5')

            await traffic_mod.buy_premium_traffic(callback, _db_user(), db)

            subtract.assert_not_awaited()
            _refused_with_alert(callback)


# ============================= Покупка: деньги и квота =============================


async def _seed_user(db, balance_kopeks: int) -> User:
    user = User(telegram_id=1, username='buyer', language='ru', balance_kopeks=balance_kopeks)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


class TestBuySucceeds:
    """Контрольная группа: без обеих гарантий выше тесты отказа были бы бессмысленны."""

    async def test_purchase_charges_and_grants_extra_bytes(self, monkeypatch):
        """Списание мокируется, как в эталонном тесте кабинета (`test_premium_traffic_purchase_route.py`):

        настоящий `subtract_user_balance` сам блокирует строку пользователя и
        подгружает `User.subscriptions`/промогруппы/реферала — эти таблицы не
        входят в предмет этого теста (списание уже покрыто там, где живёт
        функция), здесь важен порядок вызовов и итог по премиум-состоянию.
        """
        subscription = _subscription()
        _patch_resolve(monkeypatch, subscription)

        transactions: list[dict] = []

        async def _fake_transaction(**kwargs):
            transactions.append(kwargs)

        monkeypatch.setattr(traffic_mod, 'create_transaction', _fake_transaction)

        subtract_kwargs: list[dict] = []

        async def _fake_subtract(db, user, amount, description, **kwargs):
            subtract_kwargs.append(kwargs)
            user.balance_kopeks -= amount
            return True

        monkeypatch.setattr(traffic_mod, 'subtract_user_balance', _fake_subtract)

        async with memory_session(monkeypatch, TABLES) as db:
            user = await _seed_user(db, balance_kopeks=100_000)

            async def _fake_lock(db, user_id):
                return user

            monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', _fake_lock)

            callback = _callback('premium_traffic_buy_0_5')

            await traffic_mod.buy_premium_traffic(callback, user, db)

            assert user.balance_kopeks == 100_000 - 2000, 'пакет 5 ГБ стоит 2000 копеек, скидки нет'
            # Списание идёт без собственного commit — иначе откат при провале
            # начисления (см. тест выше про потолок) не смог бы его унести.
            assert subtract_kwargs == [{'commit': False}]
            assert transactions and transactions[0]['amount_kopeks'] == 2000

            state = await get_state(db, subscription.id, SQUAD)
            assert state is not None
            assert state.extra_bytes == 5 * BYTES_IN_GB

            callback.message.edit_text.assert_awaited_once()
            (success_text,) = callback.message.edit_text.await_args.args[:1]
            assert 'Премиум-трафик докуплен' in success_text

    async def test_insufficient_balance_saves_cart_without_charging(self, monkeypatch):
        subscription = _subscription()
        _patch_resolve(monkeypatch, subscription)

        subtract = AsyncMock()
        monkeypatch.setattr(traffic_mod, 'subtract_user_balance', subtract)

        saved_carts: list[dict] = []

        async def _fake_save_cart(user_id, cart_data):
            saved_carts.append(cart_data)
            return True

        monkeypatch.setattr(traffic_mod.user_cart_service, 'save_user_cart', _fake_save_cart)

        async with memory_session(monkeypatch, TABLES) as db:
            user = await _seed_user(db, balance_kopeks=100)  # меньше цены пакета (2000)

            async def _fake_lock(db, user_id):
                return user

            monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', _fake_lock)

            callback = _callback('premium_traffic_buy_0_5')

            await traffic_mod.buy_premium_traffic(callback, user, db)

            subtract.assert_not_awaited()
            assert user.balance_kopeks == 100, 'баланс не тронут при нехватке средств'
            assert saved_carts and saved_carts[0]['cart_mode'] == 'add_premium_traffic'
            assert saved_carts[0]['squad_uuid'] == SQUAD

            callback.message.edit_text.assert_awaited_once()


class TestBuyRollsBackWhenSomethingFailsAfterTheDebit:
    """Верифицированный money-баг: сбой между списанием и коммитом обязан откатывать всё.

    ``AuthMiddleware`` коммитит сессию хендлера безусловно после его возврата
    (см. ``app/middlewares/auth.py``), даже если хендлер сам поймал исключение,
    залогировал его и вернулся без ошибки. Раньше единственный ``db.rollback()``
    в этой функции стоял только в ветке ``except PremiumTopupError`` — падение
    ``create_transaction`` (или чего угодно ещё в этом окне) уходило в общий
    ``except Exception``, который ничего не откатывал: списание, сделанное с
    ``commit=False``, оставалось висеть в сессии и коммитилось мидлварью как
    единственное подтверждённое действие покупки.

    Проверка нарочно повторяет оба места, где деньги фактически терялись:
    сразу после хендлера (сам он уже обязан быть безопасным) и после коммита
    «как это сделала бы мидлварь» — тест, ограничившийся только первым, был бы
    зелёным и при старом баге, потому что ущерб наносил именно внешний коммит.
    """

    @staticmethod
    async def _balance(db, user_id: int) -> int:
        """Сырое значение из БД, а не атрибут Python-объекта — savepoint должен откатить именно строку."""
        result = await db.execute(select(User.balance_kopeks).where(User.id == user_id))
        return result.scalar_one()

    async def test_failed_transaction_record_does_not_survive_the_middleware_commit(self, monkeypatch):
        subscription = _subscription()
        _patch_resolve(monkeypatch, subscription)

        async def _boom_transaction(**kwargs):
            raise RuntimeError('запись транзакции упала')

        monkeypatch.setattr(traffic_mod, 'create_transaction', _boom_transaction)

        async def _fake_subtract(db, user, amount, description, **kwargs):
            assert kwargs.get('commit') is False, 'списание обязано оставаться незакоммиченным до конца покупки'
            user.balance_kopeks -= amount
            return True

        monkeypatch.setattr(traffic_mod, 'subtract_user_balance', _fake_subtract)

        async with memory_session(monkeypatch, TABLES) as db:
            user = await _seed_user(db, balance_kopeks=100_000)
            # Взят до вызова хендлера: `ROLLBACK TO SAVEPOINT` полностью
            # экспайрит объект, побывавший во вложенной транзакции — включая
            # `id` — и синхронное чтение атрибута после отката падает с
            # `MissingGreenlet` вне await-контекста ORM.
            user_id = user.id

            async def _fake_lock(db, user_id):
                return user

            monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', _fake_lock)

            callback = _callback('premium_traffic_buy_0_5')

            await traffic_mod.buy_premium_traffic(callback, user, db)

            # 1) Сам хендлер обязан быть безопасным без чужой помощи.
            assert await self._balance(db, user_id) == 100_000, (
                'списание должно откатиться сразу после сбоя внутри хендлера'
            )
            callback.message.edit_text.assert_awaited_once()
            (rendered_text,) = callback.message.edit_text.await_args.args[:1]
            assert 'докуплен' not in rendered_text, 'сообщение не должно намекать на успех при провале покупки'

            state = await get_state(db, subscription.id, SQUAD)
            assert state is None or state.extra_bytes == 0, 'трафик не должен начисляться без сохранённой транзакции'

            # 2) То место, где баг фактически причинял ущерб: `AuthMiddleware`
            # коммитит сессию безусловно после хендлера, что бы тот ни поймал.
            await db.commit()

            assert await self._balance(db, user_id) == 100_000, (
                'коммит мидлвари не должен закрепить списание за несостоявшуюся покупку'
            )


# ============================= Текст экрана: состояние сквада =============================


class TestStateWording:
    """Экран не должен путать «исчерпан лимитом» и «открыт админом, но не возвращён» (ревью всей ветки).

    `is_limited` истинен в обоих случаях (сквад снят из ``activeInternalSquads``),
    различает их только ``is_exhausted`` — ровно то же условие, что и
    `PremiumSquadCardState` в `admin/users.py`.
    """

    async def test_exhausted_squad_says_limit_is_exhausted(self, monkeypatch):
        subscription = _subscription()
        _patch_resolve(monkeypatch, subscription)
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.is_limited = True
            state.used_bytes = 5 * BYTES_IN_GB  # лимит выбран целиком
            await db.commit()

            callback = _callback('premium_traffic_topup')
            await traffic_mod.handle_premium_traffic_topup(callback, _db_user(), db)

            (rendered_text,) = callback.message.edit_text.await_args.args[:1]
            assert 'исчерпания лимита' in rendered_text

    async def test_reopened_pending_squad_is_not_called_exhausted(self, monkeypatch):
        """Админ открыл доступ, воркер ещё не вернул сквад в панель — лимит не исчерпан."""
        subscription = _subscription()
        _patch_resolve(monkeypatch, subscription)
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.is_limited = True  # ещё не возвращён воркером, но closed_at нет и лимит не выбран
            await db.commit()

            callback = _callback('premium_traffic_topup')
            await traffic_mod.handle_premium_traffic_topup(callback, _db_user(), db)

            (rendered_text,) = callback.message.edit_text.await_args.args[:1]
            assert 'исчерпания лимита' not in rendered_text
            assert 'открыт администратором' in rendered_text


# ============================= Потолок докупки: только отображение =============================


class TestCeilingIsDisplayOnly:
    """`max_topup_gb` здесь не источник решения — решает сервис под блокировкой (Task 4)."""

    async def test_packages_keyboard_is_not_filtered_by_the_ceiling(self, monkeypatch):
        """Экран не решает сам, что покупка "не влезет" — предлагает все настроенные пакеты.

        Даже когда уже докуплено больше, чем разрешает потолок (гипотетическая
        рассинхронизация или устаревшее чтение), список пакетов не редеет —
        отказ по потолку, если он случится, придёт от `apply_premium_topup`.
        """
        subscription = _subscription()
        _patch_resolve(monkeypatch, subscription)
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.extra_bytes = 9 * BYTES_IN_GB  # уже почти весь потолок (10 ГБ) выбран
            await db.commit()

            callback = _callback('premium_traffic_topup')

            await traffic_mod.handle_premium_traffic_topup(callback, _db_user(), db)

            markup = callback.message.edit_text.await_args.kwargs['reply_markup']
            offered_gb = sorted(
                int(b.callback_data.rsplit('_', 1)[-1])
                for row in markup.inline_keyboard
                for b in row
                if b.callback_data.startswith('premium_traffic_buy_')
            )
            # Оба настроенных пакета (1 ГБ и 5 ГБ) по-прежнему предложены,
            # хотя 5 ГБ вместе с уже докупленными 9 ГБ превысил бы потолок 10 ГБ.
            assert offered_gb == [1, 5]

    async def test_ceiling_text_is_shown_but_purchase_service_has_the_final_word(self, monkeypatch):
        """Текст экрана показывает потолок как справку, а не как собственный вердикт."""
        subscription = _subscription()
        _patch_resolve(monkeypatch, subscription)
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.extra_bytes = 9 * BYTES_IN_GB
            await db.commit()

            callback = _callback('premium_traffic_topup')

            await traffic_mod.handle_premium_traffic_topup(callback, _db_user(), db)

            (rendered_text,) = callback.message.edit_text.await_args.args[:1]
            assert 'Потолок докупки за период: 10 ГБ' in rendered_text
            assert 'уже докуплено 9' in rendered_text

    async def test_service_still_refuses_a_purchase_that_would_cross_the_ceiling(self, monkeypatch):
        """Реальный отказ на потолке приходит от сервиса — а не от того, что экран его скрыл."""
        subscription = _subscription()
        _patch_resolve(monkeypatch, subscription)

        subtract = AsyncMock()
        monkeypatch.setattr(traffic_mod, 'subtract_user_balance', subtract)

        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.extra_bytes = 9 * BYTES_IN_GB  # + 5 ГБ пакет перевалит потолок в 10 ГБ
            await db.commit()

            callback = _callback('premium_traffic_buy_0_5')

            await traffic_mod.buy_premium_traffic(callback, _db_user(), db)

            subtract.assert_not_awaited()
            _refused_with_alert(callback)
            assert 'докупить нельзя' in callback.answer.await_args.args[0]
