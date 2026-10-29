"""Одноразовый бэкфилл маркеров первой оплаченной покупки рефералов.

Маркеры нужны схеме ``days_only``: без них покупки, совершённые до включения
схемы, выглядели бы как первые и принесли бы награду повторно. По умолчанию
команда работает как сухой прогон и только считает кандидатов; запись строк
выполняется лишь с явным ``--apply``.

Примеры:
    python -m app.tools.referral_days_only_backfill
    python -m app.tools.referral_days_only_backfill --apply
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import exists, func, select

from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, User
from app.services.referral_purchase_service import (
    _already_paid_subscription_condition,
    backfill_first_purchase_markers,
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
    async with AsyncSessionLocal() as db:
        if not apply:
            # Условие «уже оплатил» берётся из сервиса, а не пишется здесь
            # заново: сухой прогон должен считать тех же кандидатов, которых
            # обойдёт бэкфилл, иначе черновики оформления и СБП-заготовки
            # завысили бы оценку. Уже помеченные пользователи в счёт попадают —
            # для операторской оценки это допустимо, а запись идемпотентна.
            result = await db.execute(
                select(func.count(User.id)).where(
                    User.referred_by_id.isnot(None),
                    exists(
                        select(Subscription.id).where(
                            Subscription.user_id == User.id,
                            _already_paid_subscription_condition(),
                        )
                    ),
                )
            )
            print(f'Candidates: {int(result.scalar() or 0)}')
            return 0

        created = await backfill_first_purchase_markers(db)
        print(f'Created markers: {created}')
        return 0


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args.apply)))


if __name__ == '__main__':
    main()
