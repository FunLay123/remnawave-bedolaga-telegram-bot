from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.handlers.referral as referral_handler
from app.config import settings


@pytest.mark.asyncio
async def test_days_only_list_shows_paid_status_and_days(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(
        referral_handler,
        'get_detailed_referral_list',
        AsyncMock(
            return_value={
                'referrals': [
                    {
                        'id': 2,
                        'full_name': 'Ivan',
                        'username': 'ivan',
                        'has_first_purchase': True,
                        'days_earned': 10,
                    },
                    {
                        'id': 3,
                        'full_name': 'Maria',
                        'username': 'maria',
                        'has_first_purchase': False,
                        'days_earned': 0,
                    },
                ],
                'total_count': 2,
                'has_next': False,
                'has_prev': False,
                'current_page': 1,
                'total_pages': 1,
            }
        ),
    )
    monkeypatch.setattr(
        'app.utils.user_utils.get_user_referral_summary',
        AsyncMock(
            return_value={
                'invited_count': 2,
                'paid_referrals_count': 1,
                'total_earned_days': 10,
            }
        ),
    )
    captured = {}

    async def fake_edit(callback, text, keyboard, parse_mode='HTML', *, force_text=False):
        captured['text'] = text
        captured['keyboard'] = keyboard
        captured['force_text'] = force_text

    monkeypatch.setattr(referral_handler, 'edit_or_answer_photo', fake_edit)

    callback = MagicMock()
    callback.answer = AsyncMock()
    db_user = SimpleNamespace(id=1, language='ru')
    db = SimpleNamespace()

    await referral_handler.show_detailed_referral_list(callback, db_user, db)

    assert '@ivan' in captured['text']
    assert 'Оплатил' in captured['text']
    assert '+10 дн.' in captured['text']
    assert '@maria' in captured['text']
    assert 'Пробует' in captured['text']
    assert 'Пополнений:' not in captured['text']
    assert 'Заработано с него' not in captured['text']
    assert captured['force_text'] is True
