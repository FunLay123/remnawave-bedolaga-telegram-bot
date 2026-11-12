import asyncio

import pytest

import app.tools.referral_days_only_backfill as backfill_tool
from app.config import settings
from app.tools.referral_days_only_backfill import build_parser


def test_parser_requires_explicit_apply_flag():
    parser = build_parser()
    args = parser.parse_args([])
    assert args.apply is False

    args = parser.parse_args(['--apply'])
    assert args.apply is True


@pytest.mark.parametrize('apply', [False, True])
def test_non_days_only_scheme_fails_before_opening_a_session(monkeypatch, capsys, apply):
    """Вне days_only маркеры никому не нужны: команда обязана отказать, не трогая БД."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')

    def explode():
        raise AssertionError('CLI не должен открывать сессию вне схемы days_only')

    monkeypatch.setattr(backfill_tool, 'AsyncSessionLocal', explode)

    assert asyncio.run(backfill_tool._run(apply=apply)) == 1
    captured = capsys.readouterr()
    assert 'ERROR' in captured.out
    assert 'days_only' in captured.out


@pytest.mark.asyncio
async def test_days_only_dry_run_excludes_self_referral(monkeypatch, capsys):
    """Сухой прогон считает тех же кандидатов, что и apply.

    Apply пропускает self-referral (``referrer_id == user_id``), поэтому
    счётчик обязан исключать его тем же условием — иначе оператор видит
    кандидата, которого бэкфилл никогда не тронет.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database.models import Base, Transaction, TransactionType, User
    from tests.fixtures.sqlite_memory import memory_session

    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')

    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(id=1, telegram_id=1001, first_name='Self', language='ru', referred_by_id=1),
                User(id=2, telegram_id=1002, first_name='Referred', language='ru', referred_by_id=1),
                Transaction(
                    id=100,
                    user_id=1,
                    type=TransactionType.SUBSCRIPTION_PAYMENT.value,
                    amount_kopeks=-10_000,
                    description='Оплата подписки',
                    is_completed=True,
                ),
                Transaction(
                    id=101,
                    user_id=2,
                    type=TransactionType.SUBSCRIPTION_PAYMENT.value,
                    amount_kopeks=-10_000,
                    description='Оплата подписки',
                    is_completed=True,
                ),
            ]
        )
        await db.commit()

        monkeypatch.setattr(
            backfill_tool,
            'AsyncSessionLocal',
            async_sessionmaker(db.bind, expire_on_commit=False),
        )

        assert await backfill_tool._run(apply=False) == 0

    assert 'Candidates: 1' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_daily_only_payments_are_not_backfill_candidates(monkeypatch, capsys):
    """Daily-списания не «оплата»: ни в dry-run, ни в apply маркер не появляется."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database.models import Base, ReferralEarning, Transaction, TransactionType, User
    from tests.fixtures.sqlite_memory import memory_session

    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')

    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(id=1, telegram_id=1001, first_name='Referrer', language='ru'),
                User(id=2, telegram_id=1002, first_name='DailyOnly', language='ru', referred_by_id=1),
                User(id=3, telegram_id=1003, first_name='Regular', language='ru', referred_by_id=1),
                Transaction(
                    id=100,
                    user_id=2,
                    type=TransactionType.SUBSCRIPTION_PAYMENT.value,
                    amount_kopeks=-1_000,
                    description='Суточная оплата тарифа «Базовый»',
                    is_completed=True,
                ),
                Transaction(
                    id=101,
                    user_id=3,
                    type=TransactionType.SUBSCRIPTION_PAYMENT.value,
                    amount_kopeks=-10_000,
                    description='Оплата подписки',
                    is_completed=True,
                ),
            ]
        )
        await db.commit()

        monkeypatch.setattr(
            backfill_tool,
            'AsyncSessionLocal',
            async_sessionmaker(db.bind, expire_on_commit=False),
        )

        assert await backfill_tool._run(apply=False) == 0
        assert 'Candidates: 1' in capsys.readouterr().out

        assert await backfill_tool._run(apply=True) == 0
        assert 'Created markers: 1' in capsys.readouterr().out

        marked_user_ids = set((await db.execute(select(ReferralEarning.user_id))).scalars().all())
        assert marked_user_ids == {3}


def test_days_only_dry_run_still_counts_candidates(monkeypatch, capsys):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')

    class _Result:
        def scalar(self):
            return 0

    class _FakeSession:
        async def execute(self, _query):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(backfill_tool, 'AsyncSessionLocal', _FakeSession)

    assert asyncio.run(backfill_tool._run(apply=False)) == 0
    assert 'Candidates: 0' in capsys.readouterr().out
