"""
Тесты для сервиса диагностики реферальной системы.
"""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.referral_diagnostics_service import ReferralDiagnosticsService


@pytest.fixture
def temp_log_file():
    """Создаёт временный лог-файл для тестов."""
    # encoding обязателен: логи бота — utf-8 с эмодзи, а дефолт Path.write_text
    # на Windows это ANSI-кодовая страница (cp1251) -> UnicodeEncodeError
    with tempfile.NamedTemporaryFile(mode='w', suffix='.log', encoding='utf-8', delete=False) as f:
        yield Path(f.name)
    # Cleanup
    Path(f.name).unlink(missing_ok=True)


@pytest.fixture
def sample_log_content():
    """Пример содержимого лог-файла с реферальными событиями."""
    today = datetime.now(UTC).strftime('%Y-%m-%d')
    return f"""
{today} 10:00:00,123 - app.handlers.start - INFO - 📩 Сообщение от ID:123456789 текст /start refABC123
{today} 11:00:00,345 - app.handlers.start - INFO - 📩 Сообщение от ID:987654321 текст /start ref987

{today} 12:00:00,901 - app.handlers.start - INFO - 💾 Сохранен start payload 'ref_refTEST777' для пользователя 555000111

{today} 13:00:00,234 - unrelated module - INFO - Some other log message
"""


@pytest.mark.asyncio
async def test_parse_logs_basic(temp_log_file, sample_log_content):
    """Тест базового парсинга логов."""
    # Записываем тестовые данные в файл
    temp_log_file.write_text(sample_log_content, encoding='utf-8')

    service = ReferralDiagnosticsService(log_path=str(temp_log_file))

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    clicks, total_lines, lines_in_period = await service._parse_clicks(today, tomorrow)

    # Проверяем что нашлись все реф-клики (3 строки с /start ref... или payload)
    assert len(clicks) == 3, f'Expected 3 clicks, found {len(clicks)}'

    # Проверяем telegram_id из распарсенных кликов
    telegram_ids = {c.telegram_id for c in clicks}
    assert telegram_ids == {123456789, 987654321, 555000111}

    # Проверяем что ref_ref-префикс очищается до ref
    payload_click = next(c for c in clicks if c.telegram_id == 555000111)
    assert payload_click.raw_code == 'ref_refTEST777'
    assert payload_click.clean_code == 'refTEST777'


@pytest.mark.asyncio
async def test_analyze_period_with_issues(temp_log_file, sample_log_content):
    """Тест анализа с проблемными случаями."""
    temp_log_file.write_text(sample_log_content, encoding='utf-8')

    service = ReferralDiagnosticsService(log_path=str(temp_log_file))

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    # Используем None вместо db для базового теста парсинга
    from unittest.mock import AsyncMock, MagicMock

    # _find_lost_referrals делает (await db.execute(...)).scalars().all();
    # пустой список => пользователи не в БД => считаются потерянными.
    # Результат должен быть синхронным MagicMock, иначе .scalars() вернёт корутину.
    empty_result = MagicMock()
    empty_result.scalars.return_value.all.return_value = []
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=empty_result)

    report = await service.analyze_period(mock_db, today, tomorrow)

    # Проверяем статистику
    assert report.total_ref_clicks >= 1, 'Should have ref clicks'

    # Проверяем что нашлись проблемные случаи
    # (987654321 пришёл по ссылке, но его нет в БД => потерянный реферал)
    assert any(lr.telegram_id == 987654321 for lr in report.lost_referrals), (
        f'Expected 987654321 in lost_referrals, got: {[lr.telegram_id for lr in report.lost_referrals]}'
    )


@pytest.mark.asyncio
async def test_empty_log_file(temp_log_file):
    """Тест работы с пустым лог-файлом."""
    temp_log_file.write_text('', encoding='utf-8')

    service = ReferralDiagnosticsService(log_path=str(temp_log_file))

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    from unittest.mock import AsyncMock

    mock_db = AsyncMock()

    report = await service.analyze_period(mock_db, today, tomorrow)

    # Проверяем что отчёт пустой
    assert report.total_ref_clicks == 0
    assert report.unique_users_clicked == 0
    assert report.lost_referrals == []
    assert report.total_lines_parsed == 0
    assert report.lines_in_period == 0


@pytest.mark.asyncio
async def test_nonexistent_log_file():
    """Тест работы с несуществующим лог-файлом."""
    service = ReferralDiagnosticsService(log_path='/nonexistent/path/to/log.log')

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    from unittest.mock import AsyncMock

    mock_db = AsyncMock()

    # Не должно быть исключений
    report = await service.analyze_period(mock_db, today, tomorrow)

    assert report.total_ref_clicks == 0
    assert report.unique_users_clicked == 0
    assert report.lost_referrals == []


@pytest.mark.asyncio
async def test_analyze_today(temp_log_file, sample_log_content):
    """Тест метода analyze_today."""
    temp_log_file.write_text(sample_log_content, encoding='utf-8')

    service = ReferralDiagnosticsService(log_path=str(temp_log_file))

    from unittest.mock import AsyncMock, MagicMock

    # Лог содержит реф-клики, поэтому _find_lost_referrals выполнит запросы к БД.
    # Возвращаем пустые наборы (синхронный результат) — конкретика БД здесь не проверяется.
    empty_result = MagicMock()
    empty_result.scalars.return_value.all.return_value = []
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=empty_result)

    report = await service.analyze_today(mock_db)

    # Проверяем что период установлен корректно
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    assert report.analysis_period_start.date() == today.date()


# ---------------------------------------------------------------------------
# Идемпотентность доначислений: нулевая пара-маркер.
#
# Если приглашённому бонус выплачен, а пригласившему платить нечего (0 при
# нулевой комиссии и фикс-бонусе), строка ReferralEarning всё равно обязана
# появиться. Она не выплата, а отметка «пара учтена»: без неё повторный apply
# из Redis/FSM снова начислил бы бонус приглашённому.
# ---------------------------------------------------------------------------


def _diag_user(uid: int, referred_by_id: int | None = None) -> object:
    from app.database.models import User

    return User(
        id=uid,
        telegram_id=1000 + uid,
        first_name=f'U{uid}',
        language='ru',
        balance_kopeks=0,
        referred_by_id=referred_by_id,
    )


def _missing_bonus_zero_referrer():
    from app.services.referral_diagnostics_service import MissingBonus

    return MissingBonus(
        referral_id=2,
        referral_telegram_id=1002,
        referral_username=None,
        referral_full_name='U2',
        referrer_id=1,
        referrer_telegram_id=1001,
        referrer_username=None,
        referrer_full_name='U1',
        first_topup_amount_kopeks=10_000,
        first_topup_date=datetime.now(UTC),
        missing_referral_bonus=True,
        missing_referrer_bonus=False,
        referral_bonus_amount=5_000,
        referrer_bonus_amount=0,
    )


@pytest.mark.asyncio
async def test_fix_missing_bonuses_zero_referrer_bonus_writes_pair_marker(monkeypatch):
    from sqlalchemy import select

    from app.config import settings
    from app.database.models import Base, ReferralEarning, User
    from tests.fixtures.sqlite_memory import memory_session

    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 5_000)
    monkeypatch.setattr(settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 0)
    monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 0)

    service = ReferralDiagnosticsService(log_path='/nonexistent/referral-diagnostics.log')
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        db.add_all([_diag_user(1), _diag_user(2, referred_by_id=1)])
        await db.commit()

        first = await service.fix_missing_bonuses(db, [_missing_bonus_zero_referrer()], apply=True)
        second = await service.fix_missing_bonuses(db, [_missing_bonus_zero_referrer()], apply=True)

        assert first.bonuses_to_referrals == 5_000
        assert first.bonuses_to_referrers == 0
        assert second.bonuses_to_referrals == 0

        referral = await db.get(User, 2)
        assert referral.balance_kopeks == 5_000

        rows = (
            (await db.execute(select(ReferralEarning).where(ReferralEarning.reason == 'referral_first_topup')))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].amount_kopeks == 0
        assert rows[0].user_id == 1
        assert rows[0].referral_id == 2


@pytest.mark.asyncio
async def test_fix_lost_referrals_zero_referrer_bonus_writes_pair_marker(monkeypatch):
    from sqlalchemy import select

    from app.config import settings
    from app.database.models import Base, ReferralEarning, Transaction, TransactionType, User
    from app.services.referral_diagnostics_service import LostReferral
    from tests.fixtures.sqlite_memory import memory_session

    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')
    monkeypatch.setattr(settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 10_000)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 5_000)
    monkeypatch.setattr(settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 0)
    monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 0)

    service = ReferralDiagnosticsService(log_path='/nonexistent/referral-diagnostics.log')
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                _diag_user(1),
                _diag_user(2),
                Transaction(
                    id=100,
                    user_id=2,
                    type=TransactionType.DEPOSIT.value,
                    amount_kopeks=10_000,
                    description='Пополнение',
                    is_completed=True,
                ),
            ]
        )
        await db.commit()

        # Маркер-хелпер после commit привязки работает в отдельной сессии;
        # направляем её в ту же in-memory БД.
        from sqlalchemy.ext.asyncio import async_sessionmaker

        monkeypatch.setattr(
            'app.database.database.AsyncSessionLocal',
            async_sessionmaker(db.bind, expire_on_commit=False),
        )

        def _lost():
            return LostReferral(
                telegram_id=1002,
                username=None,
                full_name='U2',
                referral_code='refABC12345',
                expected_referrer_code='refREF00000',
                expected_referrer_id=1,
                expected_referrer_name='U1',
                click_time=datetime.now(UTC),
                registered=True,
                has_referrer=False,
                current_referrer_id=None,
            )

        first = await service.fix_lost_referrals(db, [_lost()], apply=True)
        second = await service.fix_lost_referrals(db, [_lost()], apply=True)

        assert first.bonuses_to_referrals == 5_000
        assert first.bonuses_to_referrers == 0
        assert second.bonuses_to_referrals == 0

        referral = await db.get(User, 2)
        assert referral.balance_kopeks == 5_000

        rows = (
            (await db.execute(select(ReferralEarning).where(ReferralEarning.reason == 'referral_first_topup')))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].amount_kopeks == 0
