"""Карточка пользователя знает режим продаж бота.

Плитки над вкладками зависят от режима: в классике тарифа нет — подписи «· Командный»
быть не должно; в мультитарифе у человека несколько подписок, и плитки «до / трафик /
устройства» одной из них вводили в заблуждение — там нужна сводка по подпискам.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import event

from app.cabinet.routes.admin_users import _sales_mode_fields, get_user_detail
from app.cabinet.schemas.users import UserDetailResponse
from app.config import settings
from app.database.models import Base, ReferralEarning, User
from app.services.referral_reward_service import (
    REASON_FIRST_PURCHASE_REGISTERED,
    REASON_REGISTRATION_DAYS_ONLY,
)
from tests.fixtures.sqlite_memory import memory_session


@pytest.mark.parametrize(
    ('sales_mode', 'multi', 'expected'),
    [
        ('classic', False, {'sales_mode': 'classic', 'multi_tariff_enabled': False}),
        ('tariffs', False, {'sales_mode': 'tariffs', 'multi_tariff_enabled': False}),
        ('tariffs', True, {'sales_mode': 'tariffs', 'multi_tariff_enabled': True}),
        # Мультитариф без тарифов не работает — флаг не должен врать.
        ('classic', True, {'sales_mode': 'classic', 'multi_tariff_enabled': False}),
    ],
)
def test_sales_mode_fields_follow_settings(
    monkeypatch: pytest.MonkeyPatch, sales_mode: str, multi: bool, expected: dict
) -> None:
    monkeypatch.setattr(settings, 'SALES_MODE', sales_mode)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)

    assert _sales_mode_fields() == expected


def test_detail_response_declares_mode_fields() -> None:
    fields = UserDetailResponse.model_fields
    assert fields['sales_mode'].default == 'tariffs'
    assert fields['multi_tariff_enabled'].default is False


@pytest.mark.asyncio
async def test_user_detail_total_referral_earnings_excludes_zero_rows(monkeypatch) -> None:
    """Сумма в карточке не считает якорь регистрации и маркер первой покупки.

    Обе строки нулевые, поэтому на итог не влияют, но остаются в популяции
    ReferralEarning; проверка query-level — их причины обязаны быть в WHERE.
    """
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        db.add(
            User(
                id=1,
                telegram_id=1,
                first_name='U',
                status='active',
                language='ru',
                balance_kopeks=0,
                created_at=datetime.now(UTC),
            )
        )
        db.add_all(
            [
                ReferralEarning(
                    user_id=1,
                    referral_id=2,
                    amount_kopeks=0,
                    reason=REASON_REGISTRATION_DAYS_ONLY,
                    reward_type='days',
                    level=1,
                    days_granted=0,
                ),
                ReferralEarning(
                    user_id=1,
                    referral_id=2,
                    amount_kopeks=0,
                    reason=REASON_FIRST_PURCHASE_REGISTERED,
                    reward_type='days',
                    level=1,
                    days_granted=0,
                ),
            ]
        )
        await db.commit()

        captured: list[tuple[str, object]] = []

        def _capture(_conn, _cursor, statement, parameters, _context, _executemany):
            captured.append((statement, parameters))

        event.listen(db.bind.sync_engine, 'before_cursor_execute', _capture)
        try:
            response = await get_user_detail(user_id=1, admin=object(), db=db)
        finally:
            event.remove(db.bind.sync_engine, 'before_cursor_execute', _capture)

    earnings_sums = [
        (statement, parameters)
        for statement, parameters in captured
        if 'FROM referral_earnings' in statement and 'sum(' in statement.lower()
    ]
    assert len(earnings_sums) == 1
    parameters = str(earnings_sums[0][1])
    assert REASON_REGISTRATION_DAYS_ONLY in parameters
    assert REASON_FIRST_PURCHASE_REGISTERED in parameters
    assert response.referral.total_earnings_kopeks == 0
