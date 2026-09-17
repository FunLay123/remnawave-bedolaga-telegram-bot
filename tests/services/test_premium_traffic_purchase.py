"""Докупка премиум-трафика: правила доступности, цены и потолка."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.config import settings
from app.database.crud.premium_traffic import get_or_create_state, get_state
from app.database.models import SubscriptionPremiumTraffic
from app.services.premium_traffic_purchase import (
    PremiumTopupError,
    apply_premium_topup,
    get_premium_topup_options,
    quote_premium_topup,
)
from app.utils.premium_traffic import BYTES_IN_GB
from tests.fixtures.sqlite_memory import memory_session


TABLES = (SubscriptionPremiumTraffic.__table__,)

SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
OTHER = '82a12389-14d6-40c6-b320-4674f6bbb344'
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)

WITH_TOPUP = {
    'traffic_limit_gb': 5,
    'topup_enabled': True,
    'topup_packages': {'1': 500, '5': 2000},
    'max_topup_gb': 10,
}

# Отдельный тариф для гонки: потолок 100 ГБ и пакет 20 ГБ, чтобы одна покупка
# в потолок укладывалась, а две — нет.
BIG_TOPUP = {
    'traffic_limit_gb': 50,
    'topup_enabled': True,
    'topup_packages': {'20': 4000},
    'max_topup_gb': 100,
}


def _subscription(limits=None, connected=(SQUAD,), subscription_id=1):
    return SimpleNamespace(
        id=subscription_id,
        connected_squads=list(connected),
        tariff=SimpleNamespace(server_traffic_limits=limits if limits is not None else {SQUAD: WITH_TOPUP}),
    )


class TestOptions:
    def test_squad_with_topup_is_offered(self):
        options = get_premium_topup_options(_subscription())

        assert list(options) == [SQUAD]
        assert options[SQUAD].available_packages() == [(1, 500), (5, 2000)]

    def test_topup_disabled_squad_is_not_offered(self):
        options = get_premium_topup_options(_subscription({SQUAD: {'traffic_limit_gb': 5}}))

        assert options == {}

    def test_squad_outside_the_subscription_is_not_offered(self):
        """Платить за трафик по серверу, которого нет в подписке, нельзя."""
        options = get_premium_topup_options(_subscription(connected=(OTHER,)))

        assert options == {}

    def test_tariff_without_premium_offers_nothing(self):
        assert get_premium_topup_options(_subscription({})) == {}

    def test_nothing_is_offered_when_feature_is_disabled(self, monkeypatch):
        """Рубильник выключен — кнопку докупки нигде показывать нельзя."""
        monkeypatch.setattr(settings, 'PREMIUM_TRAFFIC_ENABLED', False)

        assert get_premium_topup_options(_subscription()) == {}


class TestQuote:
    async def test_known_package_is_priced(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            quote = await quote_premium_topup(db, _subscription(), SQUAD, 5)

            assert quote.base_price_kopeks == 2000
            assert quote.bytes == 5 * BYTES_IN_GB

    async def test_unknown_package_is_rejected(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            with pytest.raises(PremiumTopupError) as error:
                await quote_premium_topup(db, _subscription(), SQUAD, 3)

            assert error.value.code == 'package_not_found'

    async def test_squad_without_topup_is_rejected(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            with pytest.raises(PremiumTopupError) as error:
                await quote_premium_topup(db, _subscription({SQUAD: {'traffic_limit_gb': 5}}), SQUAD, 5)

            assert error.value.code == 'topup_unavailable'

    async def test_ceiling_counts_what_was_already_bought(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.extra_bytes = 8 * BYTES_IN_GB
            await db.commit()

            with pytest.raises(PremiumTopupError) as error:
                await quote_premium_topup(db, _subscription(), SQUAD, 5)

            assert error.value.code == 'topup_limit_reached'
            assert '10' in error.value.message

    async def test_purchase_up_to_the_ceiling_is_allowed(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.extra_bytes = 5 * BYTES_IN_GB
            await db.commit()

            quote = await quote_premium_topup(db, _subscription(), SQUAD, 5)

            assert quote.gb == 5

    async def test_quote_refuses_when_feature_is_disabled(self, monkeypatch):
        """Отказ обязан случиться до любого списания или изменения состояния."""
        monkeypatch.setattr(settings, 'PREMIUM_TRAFFIC_ENABLED', False)

        async with memory_session(monkeypatch, TABLES) as db:
            with pytest.raises(PremiumTopupError) as error:
                await quote_premium_topup(db, _subscription(), SQUAD, 5)

            assert error.value.code == 'feature_disabled'
            # Ни одной строки состояния не появилось: сервис не тронул БД вообще,
            # значит и до списания баланса в роутере дело дойти не могло бы.
            assert await get_state(db, 1, SQUAD) is None

    async def test_closed_squad_refuses_the_purchase(self, monkeypatch):
        """Task 13: закрытый администратором сквад нельзя купить ни через бота, ни через кабинет.

        Проверка живёт в сервисе (единая точка входа для обеих поверхностей),
        а не только на экране бота — до этого фикса ровно эта проверка в
        сервисе отсутствовала, и кабинет пропускал оплату закрытого сквада.
        """
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.closed_at = NOW
            await db.commit()

            with pytest.raises(PremiumTopupError) as error:
                await quote_premium_topup(db, _subscription(), SQUAD, 5)

            assert error.value.code == 'squad_closed'

    async def test_reopened_pending_squad_is_still_purchasable(self, monkeypatch):
        """Гейт строго на ``closed_at``, а не на ``is_limited``.

        REOPENED_PENDING: администратор снял закрытие (``closed_at is None``),
        но воркер ещё не вернул сквад в панель, поэтому ``is_limited`` всё ещё
        поднят. Такая покупка обязана пройти — она и есть путь возврата
        доступа, см. `apply_premium_topup`/`restored`.
        """
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.is_limited = True
            state.closed_at = None
            await db.commit()

            quote = await quote_premium_topup(db, _subscription(), SQUAD, 5)

            assert quote.gb == 5

    async def test_zero_ceiling_means_no_limit(self, monkeypatch):
        limits = {SQUAD: {**WITH_TOPUP, 'max_topup_gb': 0}}
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.extra_bytes = 500 * BYTES_IN_GB
            await db.commit()

            quote = await quote_premium_topup(db, _subscription(limits), SQUAD, 5)

            assert quote.gb == 5


class TestApply:
    async def test_bought_volume_is_credited(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            subscription = _subscription()
            quote = await quote_premium_topup(db, subscription, SQUAD, 5)

            state, restored = await apply_premium_topup(db, subscription, quote, period_start_at=NOW)

            assert state.extra_bytes == 5 * BYTES_IN_GB
            assert restored is False

    async def test_state_is_created_when_worker_never_ran(self, monkeypatch):
        """Покупка не должна ждать первого прохода воркера."""
        async with memory_session(monkeypatch, TABLES) as db:
            subscription = _subscription()
            quote = await quote_premium_topup(db, subscription, SQUAD, 1)

            state, _ = await apply_premium_topup(db, subscription, quote, period_start_at=NOW)

            assert state.id is not None
            assert state.limit_bytes == 5 * BYTES_IN_GB

    async def test_limited_squad_is_reported_as_restored(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            existing = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            existing.used_bytes = 5 * BYTES_IN_GB
            existing.is_limited = True
            await db.commit()

            subscription = _subscription()
            quote = await quote_premium_topup(db, subscription, SQUAD, 5)
            state, restored = await apply_premium_topup(db, subscription, quote, period_start_at=NOW)

            assert restored is True
            assert state.is_limited is False

    async def test_topup_smaller_than_overspend_does_not_restore(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            existing = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            existing.used_bytes = 20 * BYTES_IN_GB
            existing.is_limited = True
            await db.commit()

            subscription = _subscription()
            quote = await quote_premium_topup(db, subscription, SQUAD, 1)
            state, restored = await apply_premium_topup(db, subscription, quote, period_start_at=NOW)

            assert restored is False
            assert state.is_limited is True

    async def test_apply_refuses_a_squad_closed_after_the_quote(self, monkeypatch):
        """Перепроверка закрытия под блокировкой строки, симметрично потолку.

        `quote_premium_topup` читает `closed_at` без блокировки; если
        администратор закрывает сквад в промежутке между quote и apply,
        решающая проверка обязана случиться здесь же, а не быть пропущена.
        """
        async with memory_session(monkeypatch, TABLES) as db:
            subscription = _subscription()
            # Состояние заводим заранее, иначе quote его не увидит и не с чем
            # будет сравнивать «до» и «после» закрытия.
            await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            await db.commit()
            quote = await quote_premium_topup(db, subscription, SQUAD, 5)

            state = await get_state(db, 1, SQUAD)
            state.closed_at = NOW
            await db.commit()

            with pytest.raises(PremiumTopupError) as error:
                await apply_premium_topup(db, subscription, quote, period_start_at=NOW)

            assert error.value.code == 'squad_closed'
            state = await get_state(db, 1, SQUAD)
            assert state.extra_bytes == 0

    async def test_second_purchase_adds_up(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            subscription = _subscription()

            first = await quote_premium_topup(db, subscription, SQUAD, 1)
            await apply_premium_topup(db, subscription, first, period_start_at=NOW)
            second = await quote_premium_topup(db, subscription, SQUAD, 5)
            state, _ = await apply_premium_topup(db, subscription, second, period_start_at=NOW)

            assert state.extra_bytes == 6 * BYTES_IN_GB


class TestCeilingUnderConcurrency:
    """Потолок докупки при одновременных запросах.

    Проверка в ``quote_premium_topup`` читает состояние без блокировки, поэтому
    два запроса (двойной тап, повтор из-за таймаута) успевают увидеть один и тот
    же остаток и оба её пройти. Настоящую защиту даёт перепроверка под
    блокировкой строки в ``apply_premium_topup``.

    Здесь запросы разложены в детерминированный порядок: обе котировки — до
    первого начисления. Это в точности то чередование, которое пробивало
    потолок, и SQLite его воспроизводит честно. Чего SQLite показать не может —
    настоящей взаимной блокировки: ``FOR UPDATE`` он молча игнорирует. За это
    отвечает ``tests/database/test_premium_topup_ceiling_lock_postgres.py``.
    """

    async def test_concurrent_topups_do_not_exceed_cap(self, monkeypatch):
        """Два одновременных запроса не должны пробить потолок докупки."""
        async with memory_session(monkeypatch, TABLES) as db:
            subscription = _subscription({SQUAD: BIG_TOPUP})
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=50 * BYTES_IN_GB, period_start_at=NOW)
            state.extra_bytes = 80 * BYTES_IN_GB
            await db.commit()

            # Обе котировки видят «докуплено 80 из 100» и обе проходят: по
            # отдельности каждая покупка в потолок укладывается.
            first = await quote_premium_topup(db, subscription, SQUAD, 20)
            second = await quote_premium_topup(db, subscription, SQUAD, 20)

            successful_purchases = 0
            refused: list[str] = []
            for quote in (first, second):
                try:
                    await apply_premium_topup(db, subscription, quote, period_start_at=NOW)
                except PremiumTopupError as error:
                    refused.append(error.code)
                else:
                    successful_purchases += 1
            await db.commit()

            state = await get_state(db, 1, SQUAD)
            assert state.extra_bytes <= 100 * BYTES_IN_GB
            assert successful_purchases == 1
            assert refused == ['topup_limit_reached']

    async def test_second_topup_is_allowed_when_it_still_fits(self, monkeypatch):
        """Перепроверка не должна отказывать там, где место ещё есть.

        Сторож против «починки» отказом на любую вторую покупку: потолок
        считается за период, а не за запрос.
        """
        async with memory_session(monkeypatch, TABLES) as db:
            subscription = _subscription({SQUAD: BIG_TOPUP})

            first = await quote_premium_topup(db, subscription, SQUAD, 20)
            second = await quote_premium_topup(db, subscription, SQUAD, 20)
            await apply_premium_topup(db, subscription, first, period_start_at=NOW)
            state, _ = await apply_premium_topup(db, subscription, second, period_start_at=NOW)

            assert state.extra_bytes == 40 * BYTES_IN_GB

    async def test_zero_ceiling_is_not_rechecked(self, monkeypatch):
        """Потолок 0 — «без ограничения», и перепроверка его не изобретает."""
        limits = {SQUAD: {**BIG_TOPUP, 'max_topup_gb': 0}}
        async with memory_session(monkeypatch, TABLES) as db:
            subscription = _subscription(limits)
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=50 * BYTES_IN_GB, period_start_at=NOW)
            state.extra_bytes = 500 * BYTES_IN_GB
            await db.commit()

            quote = await quote_premium_topup(db, subscription, SQUAD, 20)
            state, _ = await apply_premium_topup(db, subscription, quote, period_start_at=NOW)

            assert state.extra_bytes == 520 * BYTES_IN_GB
