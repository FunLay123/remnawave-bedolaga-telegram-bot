# Trial Buy Button on Top-Up Success — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Показать триальному пользователю кнопку «Купить подписку» на экране успешного пополнения.

**Architecture:** Одна ветка в общей сборке клавиатуры успеха (`build_topup_success_keyboard`). Триал не продлевается, поэтому вместо скрытой кнопки продления он получает существующий маршрут покупки `subscription_upgrade` с тем же стилем, что и на экране «Подписка».

**Tech Stack:** Python 3.14, aiogram 3, pytest.

**Spec:** `docs/superpowers/specs/2026-11-19-topup-success-trial-buy-design.md`

## Global Constraints

- Не менять тексты локалей: использовать существующий `MENU_BUY_SUBSCRIPTION`.
- Не добавлять новых callback-хендлеров: `subscription_upgrade` уже обрабатывается.
- Не менять поведение для пользователей без подписки и для обычных продлеваемых подписок.
- После изменения добавить запись в `../claude-changes.md`.

## Review Focus

- Триал с активным статусом, но с `tariff_id=None` (классический режим): кнопка должна появляться, потому что условие — только `is_trial`.
- Снапшот пользователя: `_ensure_user_snapshot` обязан сохранять `is_trial`, иначе кнопка исчезнет на пути уведомления.
- Пользователь без подписки (`subscription=None`): кнопка не должна появляться.
- Обычная подписка: должна остаться «Продлить подписку», без дубля «Купить подписку».
- Нет подписки и `is_trial` отсутствует у мока: `getattr(..., False)` не должен падать.

---

### Task 1: Добавить кнопку покупки для триала и покрыть тестами

**Files:**
- Modify: `app/services/payment/common.py:42-95`
- Test: `tests/services/test_payment_common.py:198-223`

**Interfaces:**
- Consumes: `_pick_current_subscription(user) -> Any | None`, `_can_offer_direct_renewal(subscription) -> bool`, `apply_button_style(button, action)`.
- Produces: `build_topup_success_keyboard(user)` возвращает `InlineKeyboardMarkup`; для триала вторая кнопка — `callback_data='subscription_upgrade'`, текст «Купить подписку», `style='primary'`, `icon_custom_emoji_id='5334552404736104314'`.

- [ ] **Step 1: Написать падающий тест**

В `tests/services/test_payment_common.py` добавить после `test_success_keyboard_adds_renew_next_to_main_menu`:

```python
@pytest.mark.asyncio
async def test_success_keyboard_trial_offers_buy_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    """Триал не продлевается, но после пополнения должен вести к покупке подписки."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default')
    subscription = SimpleNamespace(
        id=7,
        is_trial=True,
        is_active=True,
        tariff_id=1,
        tariff=SimpleNamespace(is_active=True, is_daily=False),
    )
    user = SimpleNamespace(id=42, language='ru', subscription=subscription)
    service = PaymentCommonMixin()

    keyboard = await service.build_topup_success_keyboard(user)
    snapshot = await service._ensure_user_snapshot(555, user)
    snapshot_keyboard = await service.build_topup_success_keyboard(snapshot)

    for result in (keyboard, snapshot_keyboard):
        assert _callbacks(result) == ['back_to_menu', 'subscription_upgrade']
        main_menu, buy = result.inline_keyboard[0]
        assert main_menu.callback_data == 'back_to_menu'
        assert buy.text == 'Купить подписку'
        assert buy.style == 'primary'
        assert buy.icon_custom_emoji_id == '5334552404736104314'
```

И убрать `pytest.param(True, 1, SimpleNamespace(is_active=True, is_daily=False), id='trial')` из параметров `test_success_keyboard_omits_renew_when_current_tariff_cannot_be_renewed`.

- [ ] **Step 2: Убедиться, что тест падает**

Run: `python -m pytest tests/services/test_payment_common.py::test_success_keyboard_trial_offers_buy_subscription -v`
Expected: FAIL, в клавиатуре только `back_to_menu` (или отсутствует вторая кнопка).

- [ ] **Step 3: Реализовать ветку в `build_topup_success_keyboard`**

В `app/services/payment/common.py` в `build_topup_success_keyboard` вычислить `is_trial` рядом с `can_renew`:

```python
subscription = _pick_current_subscription(user)
can_renew = _can_offer_direct_renewal(subscription)
is_trial = subscription is not None and bool(getattr(subscription, 'is_trial', False))
```

В `except` добавить `is_trial = False`. После блока `if can_renew:` добавить `elif is_trial:` с кнопкой:

```python
buttons.append(
    apply_button_style(
        InlineKeyboardButton(
            text=texts.MENU_BUY_SUBSCRIPTION,
            callback_data='subscription_upgrade',
        ),
        'subscription:upgrade',
    )
)
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `python -m pytest tests/services/test_payment_common.py -v`
Expected: PASS, включая новый тест и обновлённый параметризованный.

- [ ] **Step 5: Обновить changelog**

Добавить в `../claude-changes.md` запись:

```markdown
## 2026-11-19 — Кнопка «Купить подписку» для триала после пополнения

**Scope:** `app/services/payment/common.py`, `tests/services/test_payment_common.py`.
**What:** На экране «Баланс пополнен» триальный пользователь получает вторую кнопку «Купить подписку» (`subscription_upgrade`, стиль `subscription:upgrade`) вместо скрытой кнопки продления. Обычные подписки и пользователи без подписки не затронуты.
**Why:** Триал нельзя продлить, и без этой кнопки следующий шаг — покупка платной подписки — не виден на экране успеха.
```

- [ ] **Step 6: Пересобрать справку по структуре**

Run: `python scripts/generate_structure_reference.py`
Expected: `docs/project_structure_reference.md` обновлён (новый тест и дизайн-документ).

- [ ] **Step 7: Commit**

```bash
git add app/services/payment/common.py tests/services/test_payment_common.py docs/project_structure_reference.md docs/superpowers/specs/2026-11-19-topup-success-trial-buy-design.md docs/superpowers/plans/2026-11-19-topup-success-trial-buy.md
git commit -m "feat: offer trial users a subscription purchase after top-up"
```
