"""Одноразовый бэкфилл маркеров первой оплаченной покупки рефералов.

Маркеры нужны схеме ``days_only``: без них покупки, совершённые до включения
схемы, выглядели бы как первые и принесли бы награду повторно. По умолчанию
команда работает как сухой прогон и только считает кандидатов; запись строк
выполняется лишь с явным ``--apply``.

``--apply`` запускайте в окно, когда покупки не совершаются. Бэкфилл берёт
``FOR UPDATE`` на строках кандидатов и терпит конфликты уникального индекса,
поэтому не падает и не плодит дубли, но отличить живую первую покупку от
исторической он не может: покупка, попавшая в окно, получит маркер и
потеряет бонусные дни.

Примеры:
    python -m app.tools.referral_days_only_backfill
    python -m app.tools.referral_days_only_backfill --apply
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import func, select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.services.referral_purchase_service import (
    backfill_first_purchase_markers,
    has_paid_subscription_condition,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Backfill referral first-purchase markers for existing paid referrals.',
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Write markers. Without this flag the command is a dry run.',
    )
    return parser


async def _run(apply: bool) -> int:
    if not settings.is_referral_days_only_scheme():
        print(
            'ERROR: REFERRAL_REWARD_SCHEME must be "days_only": '
            'first-purchase markers are only used by the days-only scheme.'
        )
        return 1

    async with AsyncSessionLocal() as db:
        if not apply:
            # Условие «уже оплатил» берётся из сервиса, а не пишется здесь
            # заново: сухой прогон должен считать тех же кандидатов, которых
            # обойдёт бэкфилл, иначе подарки без оплаты завысили бы оценку.
            # Уже помеченные пользователи в счёт попадают — для операторской
            # оценки это допустимо, а запись идемпотентна.
            result = await db.execute(
                select(func.count(User.id)).where(
                    User.referred_by_id.isnot(None),
                    # Apply пропускает self-referral; счётчик обязан исключать
                    # его тем же условием, иначе покажет кандидата, которого
                    # бэкфилл никогда не тронет.
                    User.referred_by_id != User.id,
                    has_paid_subscription_condition(),
                )
            )
            print(f'Candidates: {int(result.scalar() or 0)}')
            return 0

        # Окно --apply должно быть без живых покупок: FOR UPDATE сериализует
        # бэкфилл с покупкой, но покупка, попавшая в окно, получит маркер и
        # останется без бонусных дней.
        created = await backfill_first_purchase_markers(db)
        print(f'Created markers: {created}')
        return 0


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args.apply)))


if __name__ == '__main__':
    main()
