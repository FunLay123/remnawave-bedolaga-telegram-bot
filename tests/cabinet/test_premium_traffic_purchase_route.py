"""Деньги в роуте покупки премиум-трафика: за что списываем и что откатываем.

Рубильник ``PREMIUM_TRAFFIC_ENABLED``: при выключенном воркере учёта купленная
премиум-квота никем не применяется и не расходуется — значит списывать за неё
деньги нельзя. Отказ живёт в сервисном слое (``quote_premium_topup``), роут
кабинета лишь наследует его через общий маппинг ``PremiumTopupError`` -> 400.
Решающая проверка здесь — что баланс пользователя в принципе не трогается, а не
только то, что запрос падает с ошибкой (падение могло бы случиться и после
списания).

Потолок докупки перепроверяется под блокировкой строки уже после списания, и
отказ на этой перепроверке обязан унести списание с собой: одна транзакция,
общий откат. Молча оставить деньги себе нельзя.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import traffic as traffic_route
from app.cabinet.schemas.subscription import PremiumTrafficPurchaseRequest
from app.config import settings
from app.database.crud import user as user_crud
from app.database.models import User
from app.services import premium_traffic_purchase as purchase_service
from app.utils.premium_traffic import PremiumSquadConfig


SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'

WITH_TOPUP = {
    'traffic_limit_gb': 5,
    'topup_enabled': True,
    'topup_packages': {'1': 500, '5': 2000},
    'max_topup_gb': 10,
}


class _FakeTariff:
    server_traffic_limits = {SQUAD: WITH_TOPUP}


class _FakeSubscription:
    id = 10
    connected_squads = [SQUAD]
    tariff = _FakeTariff()


def _make_user() -> User:
    return User(id=1, telegram_id=123, balance_kopeks=1_000_000)


@pytest.mark.asyncio
async def test_purchase_route_refuses_when_feature_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'PREMIUM_TRAFFIC_ENABLED', False)

    async def _fake_resolve(db, user, subscription_id):
        return _FakeSubscription()

    monkeypatch.setattr(traffic_route, 'resolve_subscription', _fake_resolve)

    subtract_calls: list[int] = []

    async def _fake_subtract(db, user, amount, description):
        subtract_calls.append(amount)
        return True

    monkeypatch.setattr(traffic_route, 'subtract_user_balance', _fake_subtract)

    user = _make_user()
    request = PremiumTrafficPurchaseRequest(squad_uuid=SQUAD, gb=5)

    with pytest.raises(HTTPException) as error:
        await traffic_route.purchase_premium_traffic(
            request,
            user=user,
            db=object(),
            subscription_id=None,
        )

    # Тот же маппинг, что и для остальных PremiumTopupError в этом роуте:
    # 400 с {'code', 'message'} — не изобретаем новый статус ради этого случая.
    assert error.value.status_code == 400
    assert error.value.detail['code'] == 'feature_disabled'

    # Решающая проверка: списания не было вообще.
    assert subtract_calls == []
    assert user.balance_kopeks == 1_000_000


class _FakeSession:
    """Сессия, которая помнит, чем закончилась транзакция."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def commit(self) -> None:
        self.calls.append('commit')

    async def rollback(self) -> None:
        self.calls.append('rollback')

    async def refresh(self, _obj) -> None:
        self.calls.append('refresh')


@pytest.mark.asyncio
async def test_purchase_route_rolls_back_the_charge_when_the_cap_is_hit(monkeypatch):
    """Отказ по потолку после списания обязан откатить и списание.

    Перепроверка потолка живёт под блокировкой строки состояния, то есть
    случается уже после ``subtract_user_balance``. Единственный допустимый исход
    — общий откат: списание и начисление в одной транзакции.
    """

    async def _fake_resolve(db, user, subscription_id):
        return _FakeSubscription()

    monkeypatch.setattr(traffic_route, 'resolve_subscription', _fake_resolve)

    async def _fake_lock(db, user_id):
        return user

    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', _fake_lock)

    config = PremiumSquadConfig(
        squad_uuid=SQUAD,
        limit_gb=5,
        topup_enabled=True,
        topup_packages={5: 2000},
        max_topup_gb=10,
    )

    async def _fake_quote(db, subscription, squad_uuid, gb):
        return purchase_service.PremiumTopupQuote(
            squad_uuid=squad_uuid,
            gb=gb,
            base_price_kopeks=2000,
            config=config,
        )

    monkeypatch.setattr(purchase_service, 'quote_premium_topup', _fake_quote)

    subtract_kwargs: list[dict] = []

    async def _fake_subtract(db, user, amount, description, **kwargs):
        subtract_kwargs.append(kwargs)
        user.balance_kopeks -= amount
        return True

    monkeypatch.setattr(traffic_route, 'subtract_user_balance', _fake_subtract)

    async def _fake_apply(db, subscription, quote, *, period_start_at):
        raise purchase_service.PremiumTopupError('topup_limit_reached', 'Больше 10 ГБ за период докупить нельзя')

    monkeypatch.setattr(purchase_service, 'apply_premium_topup', _fake_apply)

    transactions: list[int] = []

    async def _fake_transaction(**kwargs):
        transactions.append(kwargs['amount_kopeks'])

    monkeypatch.setattr(traffic_route, 'create_transaction', _fake_transaction)

    user = _make_user()
    db = _FakeSession()
    request = PremiumTrafficPurchaseRequest(squad_uuid=SQUAD, gb=5)

    with pytest.raises(HTTPException) as error:
        await traffic_route.purchase_premium_traffic(request, user=user, db=db, subscription_id=None)

    assert error.value.status_code == 400
    assert error.value.detail['code'] == 'topup_limit_reached'

    # Списание шло без собственного commit — иначе откатывать было бы нечего.
    assert subtract_kwargs == [{'commit': False}]
    # Транзакция закрыта откатом, и ни одного commit по пути не случилось.
    assert db.calls == ['rollback']
    # Проводки на неслучившуюся покупку тоже быть не должно.
    assert transactions == []
