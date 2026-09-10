"""Рубильник ``PREMIUM_TRAFFIC_ENABLED`` должен соблюдаться на покупке.

При выключенном воркере учёта купленная премиум-квота никем не применяется и не
расходуется — значит списывать за неё деньги нельзя. Отказ живёт в сервисном
слое (``quote_premium_topup``), роут кабинета лишь наследует его через общий
маппинг ``PremiumTopupError`` -> 400. Решающая проверка здесь — что баланс
пользователя в принципе не трогается, а не только то, что запрос падает с
ошибкой (падение могло бы случиться и после списания).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import traffic as traffic_route
from app.cabinet.schemas.subscription import PremiumTrafficPurchaseRequest
from app.config import settings
from app.database.models import User


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
