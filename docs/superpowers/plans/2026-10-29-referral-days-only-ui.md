# Referral Days-Only Bot UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the old multi-screen money referral UI with two minimalist screens: a main referral screen with three stats, a copyable link, native share, and a referrals list. Users without any eligible subscription see a single explanatory blockquote and a trial/subscription CTA.

**Architecture:** A `referral_ui` helper builds the days-only message and keyboard from `get_user_referral_summary`. The existing `show_referral_info` handler branches into it when `settings.is_referral_days_only_scheme()` is true; the legacy screens remain reachable only outside days-only mode. The referral trial CTA reuses `activate_trial` with `from_referral=True`, which edits the current message back to the referral screen and attempts the success toast after activation.

**Tech Stack:** aiogram 3, inline keyboards, HTML parse mode, custom emoji via `<tg-emoji>` in messages and `icon_custom_emoji_id` in buttons, pytest.

**Spec:** `../referral-days-only-design.md`. Final visual decisions: main-menu referral button has no `primary`; the no-subscription screen shows only a blockquote plus one CTA and Back; the CTA is `Начать триал` when trial is available, otherwise `Моя подписка`; the share button remains `primary`.

## Global Constraints

- Russian copy only; do not edit non-Russian locales.
- No money amounts, percentages, levels, withdrawal, QR, analytics, or reward-settings buttons in days-only mode.
- The referral link is inside `<blockquote><code>...</code></blockquote>` so tap-to-copy keeps the whole URL.
- The share button is a URL button to `https://t.me/share/url?...`, not a callback.
- Button text must not contain a leading emoji when `icon_custom_emoji_id` is set.
- Do not add `primary` to the main-menu `Партнёрка` button.
- The toast is best-effort: if the callback query has expired, edit the message and skip the toast instead of failing.
- Keep repository line endings LF.

## Review Focus

- A user with no subscription must not hit a dead-end screen with only Back. Covered by Tasks 2 and 3.
- A user whose trial is available must see `Начать триал`; a user whose trial is unavailable must see `Моя подписка`. Covered by Task 2.
- A user whose trial is expired, or who has an expired paid subscription, must see the full referral screen and a working link. Covered by Task 2.
- The `Пригласить` button must open the native share sheet, and the message link must copy as a whole URL. Covered by Task 2.
- The trial activation toast must not break the callback or leave the old no-subscription screen on screen. Covered by Task 3.

---

### Task 1: Add the full-width referral button to the main menu

**Files:**
- Modify: `app/keyboards/inline.py:633-674`
- Test: `tests/test_main_menu_referral_button.py`

**Interfaces:**
- Produces: a one-button row with callback `menu_referrals` above the support row.
- Consumes: `settings.is_referral_program_enabled()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_main_menu_referral_button.py
from app.config import settings
from app.keyboards.inline import get_main_menu_keyboard


def test_referral_button_is_full_width_without_primary(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', True)
    monkeypatch.setattr(settings, 'SUPPORT_MENU_ENABLED', True)

    keyboard = get_main_menu_keyboard(language='ru')
    rows = keyboard.inline_keyboard
    referral_index = next(
        index for index, row in enumerate(rows) if any(button.callback_data == 'menu_referrals' for button in row)
    )
    referral_row = rows[referral_index]

    assert len(referral_row) == 1
    button = referral_row[0]
    assert button.text == 'Партнёрка'
    assert button.icon_custom_emoji_id == '5343636681473935403'
    assert button.style is None

    support_indexes = [
        index for index, row in enumerate(rows) if any(button.callback_data == 'menu_support' for button in row)
    ]
    if support_indexes:
        assert referral_index < support_indexes[0]
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `pytest tests/test_main_menu_referral_button.py -v`
Expected: FAIL with `StopIteration` because no `menu_referrals` row exists.

- [ ] **Step 3: Insert the row before support**

In `get_main_menu_keyboard`, after the subscription/top-up rows and before the support block:

```python
    if settings.is_referral_program_enabled():
        keyboard.append(
            [
                InlineKeyboardButton(
                    text='Партнёрка',
                    callback_data='menu_referrals',
                    icon_custom_emoji_id='5343636681473935403',
                )
            ]
        )
```

Do not add `style='primary'`.

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_main_menu_referral_button.py tests/test_main_menu_redesign.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/keyboards/inline.py tests/test_main_menu_referral_button.py
git commit -m "referral: add main menu entry"
```

---

### Task 2: Build the days-only referral screen and no-subscription CTA

**Files:**
- Create: `app/utils/referral_ui.py`
- Modify: `app/handlers/referral.py:56-180` (`show_referral_info`)
- Test: `tests/handlers/test_referral_days_only_screen.py`

**Interfaces:**
- Produces: `has_eligible_subscription(user) -> bool`
- Produces: `is_trial_available_for_referral(user) -> bool`
- Produces: `build_days_only_referral_screen(*, user, summary, bot_referral_link, trial_available) -> tuple[str, InlineKeyboardMarkup]`
- `show_referral_info` calls the builder only when `settings.is_referral_days_only_scheme()`.

- [ ] **Step 1: Write the failing screen tests**

```python
# tests/handlers/test_referral_days_only_screen.py
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.handlers.referral as referral_handler
from app.config import settings
from app.database.models import SubscriptionStatus
from app.utils.referral_ui import build_days_only_referral_screen


def _user(*, subscriptions=None):
    return SimpleNamespace(
        id=1,
        telegram_id=1001,
        language='ru',
        referral_code='ABC123',
        subscriptions=subscriptions or [],
        is_trial_already_used=lambda: False,
        auth_type='telegram',
    )


def _sub(status: str, *, trial: bool = False):
    return SimpleNamespace(status=status, is_trial=trial)


def test_full_screen_has_stats_link_share_and_no_old_buttons():
    user = _user(subscriptions=[_sub(SubscriptionStatus.ACTIVE.value)])
    summary = {
        'invited_count': 12,
        'paid_referrals_count': 4,
        'total_earned_days': 40,
        'recent_earnings': [],
    }

    text, keyboard = build_days_only_referral_screen(
        user=user,
        summary=summary,
        bot_referral_link='https://t.me/bot?start=ref_ABC123',
        trial_available=False,
    )

    assert 'Приглашено: <b>12</b>' in text
    assert 'Оплатили: <b>4</b>' in text
    assert 'Получено бонусом: <b>+40 дн.</b>' in text
    assert '<blockquote><code>https://t.me/bot?start=ref_ABC123</code></blockquote>' in text

    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert 'referral_list' in callbacks
    assert 'back_to_menu' in callbacks
    assert 'referral_show_qr' not in callbacks
    assert 'referral_analytics' not in callbacks
    assert 'referral_reward_settings' not in callbacks
    assert 'referral_withdrawal' not in callbacks

    share = keyboard.inline_keyboard[0][0]
    assert share.url.startswith('https://t.me/share/url?')
    assert share.style == 'primary'
    assert share.icon_custom_emoji_id == '5334552404736104314'


def test_no_subscription_screen_has_trial_cta_and_back():
    user = _user(subscriptions=[])
    text, keyboard = build_days_only_referral_screen(
        user=user,
        summary={'invited_count': 0, 'paid_referrals_count': 0, 'total_earned_days': 0},
        bot_referral_link='https://t.me/bot?start=ref_ABC123',
        trial_available=True,
    )

    assert 'Бонусные дни начисляются только' in text
    assert 'при наличии подписки' in text
    assert 'https://t.me/bot?start=ref_ABC123' not in text

    rows = keyboard.inline_keyboard
    assert rows[0][0].text == 'Начать триал'
    assert rows[0][0].callback_data == 'referral_start_trial'
    assert rows[0][0].style == 'primary'
    assert rows[0][0].icon_custom_emoji_id == '5314310000531766389'
    assert rows[-1][0].callback_data == 'back_to_menu'


def test_no_subscription_screen_falls_back_to_subscription_cta():
    user = _user(subscriptions=[])
    text, keyboard = build_days_only_referral_screen(
        user=user,
        summary={'invited_count': 0, 'paid_referrals_count': 0, 'total_earned_days': 0},
        bot_referral_link='https://t.me/bot?start=ref_ABC123',
        trial_available=False,
    )

    assert 'Бонусные дни начисляются только' in text
    assert keyboard.inline_keyboard[0][0].text == 'Моя подписка'
    assert keyboard.inline_keyboard[0][0].callback_data == 'menu_subscription'
    assert keyboard.inline_keyboard[0][0].style == 'primary'
    assert keyboard.inline_keyboard[0][0].icon_custom_emoji_id == '5314310000531766389'
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `pytest tests/handlers/test_referral_days_only_screen.py -v`
Expected: FAIL with `ModuleNotFoundError: app.utils.referral_ui`.

- [ ] **Step 3: Implement the UI builder**

Create `app/utils/referral_ui.py`:

```python
from __future__ import annotations

import html
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.database.models import SubscriptionStatus


HEADER = '<tg-emoji emoji-id="5235695112419303615">🤝</tg-emoji> <b>Партнёрская программа</b>'
INVITED = '<tg-emoji emoji-id="5116368680279606270">👥</tg-emoji>'
PAID = '<tg-emoji emoji-id="5258500422393415126">💳</tg-emoji>'
RECEIVED = '<tg-emoji emoji-id="5123359615727174427">🎁</tg-emoji>'
TERMS = '<tg-emoji emoji-id="5335020345717973061">🎁</tg-emoji>'
SHARE_ICON = '5334552404736104314'
LIST_ICON = '5258274739041883702'
CTA_ICON = '5314310000531766389'
GIFT_ICON = '5359415772844152891'

ELIGIBLE_STATUSES = {
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.TRIAL.value,
    SubscriptionStatus.LIMITED.value,
    SubscriptionStatus.EXPIRED.value,
}


def has_eligible_subscription(user) -> bool:
    return any(
        (getattr(subscription, 'status', '') or '').lower() in ELIGIBLE_STATUSES
        for subscription in (getattr(user, 'subscriptions', None) or [])
    )


def is_trial_available_for_referral(user) -> bool:
    if settings.TRIAL_DURATION_DAYS <= 0:
        return False
    if settings.is_trial_disabled_for_user(getattr(user, 'auth_type', 'telegram')):
        return False
    if user.is_trial_already_used():
        return False
    return not any(
        (getattr(subscription, 'status', '') or '').lower()
        in {SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value, SubscriptionStatus.LIMITED.value}
        for subscription in (getattr(user, 'subscriptions', None) or [])
    )


def _share_url(link: str) -> str:
    return (
        'https://t.me/share/url?url='
        + quote(link, safe='')
        + '&text='
        + quote('Присоединяйся по моей ссылке!', safe='')
    )


def build_days_only_referral_screen(
    *,
    user,
    summary: dict,
    bot_referral_link: str,
    trial_available: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    back = InlineKeyboardButton(text='Назад', callback_data='back_to_menu')

    if not has_eligible_subscription(user):
        text = (
            f'<blockquote><tg-emoji emoji-id="{GIFT_ICON}">🎁</tg-emoji> '
            'Бонусные дни начисляются только\nпри наличии подписки.</blockquote>'
        )
        if trial_available:
            cta = InlineKeyboardButton(
                text='Начать триал',
                callback_data='referral_start_trial',
                icon_custom_emoji_id=CTA_ICON,
                style='primary',
            )
        else:
            cta = InlineKeyboardButton(
                text='Моя подписка',
                callback_data='menu_subscription',
                icon_custom_emoji_id=CTA_ICON,
                style='primary',
            )
        return text, InlineKeyboardMarkup(inline_keyboard=[[cta], [back]])

    text = (
        f'{HEADER}\n\n'
        f'{INVITED} Приглашено: <b>{int(summary.get("invited_count") or 0)}</b>\n'
        f'{PAID} Оплатили: <b>{int(summary.get("paid_referrals_count") or 0)}</b>\n'
        f'{RECEIVED} Получено бонусом: <b>+{int(summary.get("total_earned_days") or 0)} дн.</b>\n\n'
        f'{TERMS} <b>За каждого друга</b>\n'
        '• +3 дня к триалу\n'
        '• +10 дней вам и другу\n'
        '  за первую покупку от 30 дней\n\n'
        f'🔗 Ваша ссылка\n'
        f'<blockquote><code>{html.escape(bot_referral_link)}</code></blockquote>'
    )

    share = InlineKeyboardButton(
        text='Пригласить',
        url=_share_url(bot_referral_link),
        icon_custom_emoji_id=SHARE_ICON,
        style='primary',
    )
    referrals = InlineKeyboardButton(
        text='Мои рефералы',
        callback_data='referral_list',
        icon_custom_emoji_id=LIST_ICON,
    )
    return text, InlineKeyboardMarkup(inline_keyboard=[[share], [referrals], [back]])
```

- [ ] **Step 4: Branch `show_referral_info` into the new screen**

After the referral-code check and before the legacy text is assembled:

```python
    if settings.is_referral_days_only_scheme():
        from app.utils.referral_ui import (
            build_days_only_referral_screen,
            is_trial_available_for_referral,
        )

        bot_username = (await callback.bot.get_me()).username
        bot_referral_link = settings.get_bot_referral_link(db_user.referral_code, bot_username)
        summary = await get_user_referral_summary(db, db_user.id)
        text, keyboard = build_days_only_referral_screen(
            user=db_user,
            summary=summary,
            bot_referral_link=bot_referral_link,
            trial_available=is_trial_available_for_referral(db_user),
        )
        await edit_or_answer_photo(callback, text, keyboard)
        await callback.answer()
        return
```

The branch must run before any legacy level/commission code so no money wording is assembled.

- [ ] **Step 5: Run the screen tests**

Run: `pytest tests/handlers/test_referral_days_only_screen.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/utils/referral_ui.py app/handlers/referral.py tests/handlers/test_referral_days_only_screen.py
git commit -m "referral: add days-only screen"
```

---

### Task 3: Add the referrals list and the trial CTA flow

**Files:**
- Modify: `app/handlers/referral.py:410-525` (`show_detailed_referral_list`)
- Modify: `app/handlers/subscription/purchase.py:797` (`activate_trial` signature and success tail)
- Modify: `app/handlers/referral.py:1052` (`register_handlers`)
- Test: `tests/handlers/test_referral_days_only_list.py`
- Test: `tests/handlers/test_referral_start_trial.py`

**Interfaces:**
- `show_detailed_referral_list` gets a days-only branch and uses `has_first_purchase`.
- `activate_trial(..., from_referral: bool = False)`.
- New callback handler `start_trial_from_referral` for `referral_start_trial`.

- [ ] **Step 1: Write the failing list test**

```python
# tests/handlers/test_referral_days_only_list.py
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

    async def fake_edit(callback, text, keyboard):
        captured['text'] = text
        captured['keyboard'] = keyboard

    monkeypatch.setattr(referral_handler, 'edit_or_answer_photo', fake_edit)

    callback = MagicMock()
    callback.answer = AsyncMock()
    db_user = SimpleNamespace(id=1, language='ru')
    db = SimpleNamespace()

    await referral_handler.show_detailed_referral_list(callback, db_user, db)

    assert '@ivan' in captured['text']
    assert 'оплатил' in captured['text']
    assert '+10 дн.' in captured['text']
    assert '@maria' in captured['text']
    assert 'пробует' in captured['text']
    assert 'Пополнений:' not in captured['text']
    assert 'Заработано с него' not in captured['text']
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `pytest tests/handlers/test_referral_days_only_list.py -v`
Expected: FAIL because the old list still renders top-ups and money.

- [ ] **Step 3: Add the days-only list branch**

At the top of `show_detailed_referral_list` after fetching `referrals_data`:

```python
    if settings.is_referral_days_only_scheme():
        await _show_days_only_referral_list(callback, db_user, db, referrals_data, page)
        return
```

Add the helper in the same module:

```python
async def _show_days_only_referral_list(callback, db_user, db, referrals_data, page: int) -> None:
    from app.utils.user_utils import get_user_referral_summary

    summary = await get_user_referral_summary(db, db_user.id)

    if not referrals_data['referrals']:
        await edit_or_answer_photo(
            callback,
            '<blockquote>👥 У вас пока нет рефералов.</blockquote>',
            InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='Назад', callback_data='menu_referrals')]]
            ),
        )
        await callback.answer()
        return

    text = (
        '<tg-emoji emoji-id="5258274739041883702">👥</tg-emoji> <b>Мои рефералы</b>\n\n'
        f'Приглашено {int(summary.get("invited_count") or 0)} · '
        f'Оплатили {int(summary.get("paid_referrals_count") or 0)}\n'
        f'Получено +{int(summary.get("total_earned_days") or 0)} дн.\n\n'
    )
    for item in referrals_data['referrals']:
        name = f'@{item["username"]}' if item.get('username') else html_escape(str(item.get('full_name') or ''))
        if item.get('has_first_purchase'):
            text += (
                '<tg-emoji emoji-id="5449444442056441600">•</tg-emoji> '
                f'{name} — <tg-emoji emoji-id="5116414868357907335">✅</tg-emoji> оплатил'
            )
            if item.get('days_earned'):
                text += f', +{int(item["days_earned"])} дн.'
        else:
            text += (
                '<tg-emoji emoji-id="5449444442056441600">•</tg-emoji> '
                f'{name} — <tg-emoji emoji-id="5123359615727174427">⏳</tg-emoji> пробует'
            )
        text += '\n'

    rows = []
    navigation = []
    if referrals_data['has_prev']:
        navigation.append(
            InlineKeyboardButton(
                text='←',
                callback_data=f'referral_list_page_{page - 1}',
            )
        )
    if referrals_data['has_next']:
        navigation.append(
            InlineKeyboardButton(
                text='→',
                callback_data=f'referral_list_page_{page + 1}',
            )
        )
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text='Назад', callback_data='menu_referrals')])

    await edit_or_answer_photo(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()
```

- [ ] **Step 4: Add the trial CTA handler**

In `app/handlers/referral.py`, add a module-level import near the existing imports:

```python
from app.utils.referral_ui import is_trial_available_for_referral
```

Then add:

```python
async def start_trial_from_referral(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    if not is_trial_available_for_referral(db_user):
        await callback.answer('Триал недоступен', show_alert=True)
        return

    from app.handlers.subscription.purchase import activate_trial

    await activate_trial(callback, db_user, db, from_referral=True)
```

Register it:

```python
    dp.callback_query.register(start_trial_from_referral, F.data == 'referral_start_trial')
```

- [ ] **Step 5: Add `from_referral` to `activate_trial`**

Change the signature:

```python
async def activate_trial(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    *,
    from_referral: bool = False,
):
```

At the final success point, where the free trial has been fully activated and the success message has been edited, replace the final `await callback.answer()` with:

```python
    if from_referral:
        from app.utils.referral_ui import (
            build_days_only_referral_screen,
            is_trial_available_for_referral,
        )
        from app.utils.user_utils import get_user_referral_summary

        await db.refresh(db_user, ['subscriptions'])
        summary = await get_user_referral_summary(db, db_user.id)
        bot_username = (await callback.bot.get_me()).username
        link = settings.get_bot_referral_link(db_user.referral_code, bot_username)
        text, keyboard = build_days_only_referral_screen(
            user=db_user,
            summary=summary,
            bot_referral_link=link,
            trial_available=is_trial_available_for_referral(db_user),
        )
        await edit_or_answer_photo(callback, text, keyboard)
        try:
            await callback.answer('Триал активирован')
        except Exception:
            # Callback query may have expired after the panel sync; the screen
            # update above is still useful, so do not fail the activation.
            pass
        return

    await callback.answer()
```

Import `edit_or_answer_photo` at the top of `purchase.py` if it is not already imported. Do not add this branch to the paid-trial payment screen; it returns before activation.

- [ ] **Step 6: Write the start-trial tests**

```python
# tests/handlers/test_referral_start_trial.py
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.handlers.referral as referral_handler
from app.config import settings


@pytest.mark.asyncio
async def test_start_trial_rejects_when_trial_is_unavailable(monkeypatch):
    monkeypatch.setattr(referral_handler, 'is_trial_available_for_referral', lambda user: False, raising=False)
    callback = SimpleNamespace(answer=AsyncMock())
    await referral_handler.start_trial_from_referral(callback, SimpleNamespace(), None)
    callback.answer.assert_awaited_once_with('Триал недоступен', show_alert=True)


@pytest.mark.asyncio
async def test_start_trial_calls_activate_trial_from_referral(monkeypatch):
    import app.handlers.subscription.purchase as purchase_handler

    monkeypatch.setattr(referral_handler, 'is_trial_available_for_referral', lambda user: True, raising=False)
    activate = AsyncMock()
    monkeypatch.setattr(purchase_handler, 'activate_trial', activate)

    callback = SimpleNamespace()
    db_user = SimpleNamespace()
    db = SimpleNamespace()

    await referral_handler.start_trial_from_referral(callback, db_user, db)

    activate.assert_awaited_once_with(callback, db_user, db, from_referral=True)
```

The monkeypatch target for `is_trial_available_for_referral` must be the module attribute imported inside the function or patched before import; if the function imports it locally, patch `app.utils.referral_ui.is_trial_available_for_referral` and let the local import pick it up.

- [ ] **Step 7: Run the list and start-trial tests**

Run:

```bash
pytest tests/handlers/test_referral_days_only_list.py tests/handlers/test_referral_start_trial.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/handlers/referral.py app/handlers/subscription/purchase.py tests/handlers/test_referral_days_only_list.py tests/handlers/test_referral_start_trial.py
git commit -m "referral: add days-only list and trial CTA"
```

---

### Task 4: Align mini app and cabinet terms with days-only mode

> **Status: deferred/skipped.** This deployment has no mini app and no cabinet frontend; users only see the Telegram chat and inline buttons. Do not implement this task unless those surfaces are added later.

**Files:**
- Modify: `app/webapi/routes/miniapp.py:2920-3085` (`_build_referral_info`)
- Modify: `app/cabinet/routes/referral.py` (referral info/terms route)
- Test: `tests/webapi/test_miniapp_referral_days_only.py`
- Test: `tests/cabinet/test_referral_days_only_terms.py`

**Interfaces:**
- In days-only mode, the mini app and cabinet must return terms about +3 trial days and +10 first-purchase days, and must not expose commission percentages, levels, or withdrawal.
- `paid_referrals_count` and `total_earned_days` come from the updated summary.

- [ ] **Step 1: Write the failing mini-app test**

```python
# tests/webapi/test_miniapp_referral_days_only.py
from types import SimpleNamespace

import pytest

from app.config import settings
from app.webapi.routes.miniapp import _build_referral_info


@pytest.mark.asyncio
async def test_miniapp_terms_do_not_mention_commission(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(
        'app.webapi.routes.miniapp.get_user_referral_summary',
        lambda db, user_id: {
            'invited_count': 1,
            'paid_referrals_count': 1,
            'total_earned_days': 10,
            'total_earned_kopeks': 0,
            'recent_earnings': [],
            'referrals': [],
        },
    )

    user = SimpleNamespace(
        id=1,
        language='ru',
        referral_code='ABC123',
        referred_by_id=1,
        subscriptions=[],
    )
    info = await _build_referral_info(db=SimpleNamespace(), user=user)

    joined = ' '.join(info.terms or [])
    assert '+3' in joined
    assert '+10' in joined
    assert 'процент' not in joined.lower()
    assert 'комисси' not in joined.lower()
```

The exact `_build_referral_info` signature and return type must be preserved; only the terms/stats construction changes. If the function is not directly importable, use the existing mini-app referral route test fixture instead of calling it directly.

- [ ] **Step 2: Run the test and verify it fails**

Run: `pytest tests/webapi/test_miniapp_referral_days_only.py -v`
Expected: FAIL because the terms still describe commission/levels or the patched summary shape does not match.

- [ ] **Step 3: Add the days-only terms branch**

In `_build_referral_info`, after the referral links are built and before the legacy commission/level block:

```python
    if settings.is_referral_days_only_scheme():
        summary = await get_user_referral_summary(db, user.id)
        terms = [
            '🎁 +3 дня к триалу по вашей ссылке',
            '🎁 +10 дней вам и другу за первую покупку от 30 дней',
        ]
        return MiniAppReferralInfo(
            referral_code=referral_code,
            referral_link=referral_link,
            bot_referral_link=bot_referral_link,
            terms=terms,
            stats=MiniAppReferralStats(
                invited_count=int(summary.get('invited_count') or 0),
                paid_referrals_count=int(summary.get('paid_referrals_count') or 0),
                total_earned_days=int(summary.get('total_earned_days') or 0),
                total_earned_kopeks=0,
            ),
            recent_earnings=[],
            referrals=[],
        )
```

Use the existing schema fields and existing summary fetch; do not duplicate the stats query if the surrounding code already fetched it. The exact schema names are `MiniAppReferralInfo` and `MiniAppReferralStats` in `app/webapi/schemas/miniapp.py`.

- [ ] **Step 4: Add the cabinet terms branch**

In the cabinet referral info route, when days-only is enabled, return the same two terms and hide withdrawal/reward settings. Keep the existing response schema; set money-related fields to zero and days from `get_user_referral_summary`.

Add a test that calls the cabinet route handler or its service function with a days-only settings monkeypatch and asserts the response terms contain `+3`/`+10` and do not contain `процент`/`комисси`/`вывод`.

- [ ] **Step 5: Run the mini-app and cabinet tests**

Run:

```bash
pytest tests/webapi/test_miniapp_referral_days_only.py tests/cabinet/test_referral_days_only_terms.py tests/test_miniapp_payments.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/webapi/routes/miniapp.py app/cabinet/routes/referral.py tests/webapi/test_miniapp_referral_days_only.py tests/cabinet/test_referral_days_only_terms.py
git commit -m "referral: align miniapp and cabinet terms"
```

---

### Task 5: Update the shared top-up success main-menu icon

**Files:**
- Modify: `app/utils/payment_button_styles.py:56-62`
- Modify: `tests/services/test_main_menu_button_cabinet_mode.py:322-350`

**Interfaces:**
- `BUTTON_STYLES['success:main_menu']` stays `primary`; replace the icon `5269735794846769214` with `5314310000531766389`.
- This key is shared by all top-up success screens and is the same icon used on referral notification buttons.

- [ ] **Step 1: Update the regression test first**

In `test_topup_success_keyboard_without_cart_points_to_main_menu`, change the assertion:

```python
    assert button.icon_custom_emoji_id == '5314310000531766389'
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `pytest tests/services/test_main_menu_button_cabinet_mode.py::test_topup_success_keyboard_without_cart_points_to_main_menu -v`
Expected: FAIL with the old icon `5269735794846769214`.

- [ ] **Step 3: Replace the icon in the registry**

```python
    'success:main_menu': ButtonStyle(
        icon_custom_emoji_id='5314310000531766389',
        style='primary',
    ),
```

- [ ] **Step 4: Run the test**

Run: `pytest tests/services/test_main_menu_button_cabinet_mode.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/utils/payment_button_styles.py tests/services/test_main_menu_button_cabinet_mode.py
git commit -m "payments: update success main menu icon"
```

## Final Verification

- [ ] Main menu: `Партнёрка` is one full-width row above support, no `primary`, icon `5343636681473935403`.
- [ ] Eligible user: stats, link in blockquote, share URL button, referrals list, Back.
- [ ] No subscription: blockquote, `Начать триал` or `Моя подписка`, Back. No link and no stats.
- [ ] Expired trial or expired paid subscription: full screen, not the no-subscription blockquote.
- [ ] `Пригласить` opens the native share sheet; link in the message copies as a whole URL.
- [ ] `Начать триал`: on success the current message becomes the full referral screen and the toast `Триал активирован` is attempted; if the callback has expired, the screen still updates.
- [ ] Paid trial: the payment screen is shown; no false success toast.
- [ ] No QR, analytics, reward settings, withdrawal, money, or level buttons in days-only mode.
- [ ] Run `pytest tests/handlers/test_referral_days_only_screen.py tests/handlers/test_referral_days_only_list.py tests/handlers/test_referral_start_trial.py tests/test_main_menu_referral_button.py -v`.
- [ ] Append the UI change set to `../claude-changes.md` with scope, what, and why.
