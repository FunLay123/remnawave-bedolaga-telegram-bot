# Безлимит на премиум-сквад: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать админу в боте кнопку «♾️ Выдать безлимит» на конкретный премиум-сквад подписки и такую же кнопку снятия; безлимит переживает сброс периода и смену тарифа, а `closed_at` остаётся сильнее.

**Architecture:** На строке `subscription_premium_traffic` появляется nullable-timestamp `unlimited_at` (NULL — безлимита нет). Модель трактует `closed_at` как «исчерпан всегда», `unlimited_at` — как «не исчерпан никогда». Воркер не шлёт предупреждения и не снимает сквад; осиротевшие строки сохраняются. Бот показывает новое состояние карточки, меню рисует «♾️», прокси отдаёт `total=0`, покупка докупки блокируется. Кабинетные админ-ручки не трогаем.

**Tech Stack:** Python, aiogram, SQLAlchemy async, Alembic, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-11-22-premium-squad-unlimited-design.md`

## Global Constraints

- Одна nullable-колонка `unlimited_at` (`DateTime(timezone=True)`), без boolean и без чисел в `BigInteger`.
- `closed_at` важнее `unlimited_at`: закрытый сквад остаётся закрытым, выдача безлимита его не открывает.
- `unlimited_at` не сбрасывается `start_new_period`, продлением и сменой тарифа; строка переживает уход сквада из премиум-списка.
- Кнопка не меняет `subscription.traffic_limit_gb`; общий лимит панели остаётся сверху, на экране выдачи — предупреждение.
- Кабинетные админ-ручки `/cabinet/admin/premium-traffic/*` не меняем.
- Комментарии и докстринги — по-русски, коммиты — conventional commits по-английски.
- Линтер: `ruff check` и `ruff format --check` для изменённых файлов; `line-length = 120`.
- После кода — запись в `../claude-changes.md` (вне репозитория).

## Review Focus

- **Выдача до первого прохода воркера:** строки состояния ещё нет. Грант обязан её создать, поставить флаг и разбудить воркер. Тест — Task 6, `test_confirm_grant_creates_missing_state_and_wakes_worker`.
- **Выдача снятому за расход скваду:** `is_limited=True`, сквад отсутствует в панели. Воркер должен вернуть его, а не оставить снятым навсегда. Тест — Task 4, `test_granting_unlimited_restores_a_limited_squad`.
- **Закрытый + безлимит:** `closed_at` обязан выигрывать в модели, воркере и карточке. Тесты — Task 2 `test_closed_wins_over_unlimited`, Task 4 `test_closed_unlimited_squad_is_not_restored`, Task 5 `test_closed_beats_unlimited`.
- **Сквад ушёл из премиум-списка после гранта:** строку нельзя удалять, а `is_limited` нельзя оставлять поднятым — иначе `effective_panel_squads` продолжит вычитать сквад, который больше не премиальный. Тесты — Task 3, `test_unlimited_state_survives_leaving_the_premium_config` и `test_unlimited_orphan_with_removed_squad_is_restored_but_row_kept`.
- **Смешанный набор в прокси:** один безлимитный сквад среди конечных. `download` — фактический расход, `total` — `0` (у клиентов это «без лимита»). Тест — Task 8, `test_premium_usage_unlimited_squad_reports_zero_total`.

---

## File Structure

- `app/database/models.py` — колонка `unlimited_at`, семантика `is_exhausted`.
- `migrations/alembic/versions/0139_subscription_premium_traffic_unlimited.py` — схема.
- `app/database/crud/premium_traffic.py` — `set_unlimited`.
- `app/services/premium_traffic_service.py` — сохранение осиротевших строк, пропуск предупреждений.
- `app/database/crud/subscription.py` — сохранение строки при смене тарифа, снятие `is_limited` у безлимитной осиротевшей строки.
- `app/handlers/admin/users.py` — состояние карточки, рендер, кнопки, выдача/снятие.
- `app/utils/rich_menu.py` — «♾️» в меню и отпечаток живого меню.
- `app/webserver/subscription_proxy.py` — `total=0`.
- `app/services/premium_traffic_purchase.py` — отказ докупки.
- `app/handlers/subscription/traffic.py` — скрытие безлимитного сквада из списка покупок.
- Тесты — рядом с существующими файлами, перечислены в задачах.

---

### Task 1: Колонка и миграция

**Files:**
- Modify: `app/database/models.py` (класс `SubscriptionPremiumTraffic`, рядом с `closed_at`)
- Create: `migrations/alembic/versions/0139_subscription_premium_traffic_unlimited.py`
- Modify: `tests/database/test_premium_traffic_schema_parity.py` (кортеж `MIGRATIONS`)

**Interfaces:**
- Produces: `SubscriptionPremiumTraffic.unlimited_at: datetime | None`.

- [ ] **Step 1: Добавить колонку в модель**

```python
# Выданный вручную безлимит на этот сквад. NULL — безлимита нет; значение —
# когда выдан. Это причина, отдельная от следствия `is_limited`, и она
# переживает смену периода; `closed_at` при этом важнее и проверяется первым.
unlimited_at = Column(AwareDateTime(), nullable=True)
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `pytest tests/database/test_premium_traffic_schema_parity.py -q`
Expected: FAIL — в наборе колонок миграции нет `unlimited_at`.

- [ ] **Step 3: Создать миграцию 0139**

По образцу `0133_premium_traffic_notified_90.py`: `revision = '0139'`,
`down_revision = '0138'`, `TABLE = 'subscription_premium_traffic'`,
`COLUMN = 'unlimited_at'`, idempotent `_has_column()`, `batch.add_column(
sa.Column('unlimited_at', sa.DateTime(timezone=True), nullable=True))`; в
`downgrade()` — `drop_column`. Добавить файл в `MIGRATIONS` теста
`test_premium_traffic_schema_parity.py` после `0133_premium_traffic_notified_90.py`.

- [ ] **Step 4: Проверить схему и цепочку**

Run: `pytest tests/database/test_premium_traffic_schema_parity.py tests/database/test_migration_chain.py -q`
Expected: PASS, один head.

- [ ] **Step 5: Commit**

```bash
git add app/database/models.py migrations/alembic/versions/0139_subscription_premium_traffic_unlimited.py tests/database/test_premium_traffic_schema_parity.py
git commit -m "feat: add unlimited_at to premium traffic state"
```

---

### Task 2: Семантика `is_exhausted` и `set_unlimited`

**Files:**
- Modify: `app/database/models.py` (`SubscriptionPremiumTraffic.is_exhausted`)
- Modify: `app/database/crud/premium_traffic.py` (новая функция)
- Test: `tests/crud/test_premium_traffic_state.py`

**Interfaces:**
- Produces: `set_unlimited(state: SubscriptionPremiumTraffic, granted: bool, *, now: datetime | None = None) -> SubscriptionPremiumTraffic`.
- Produces: `is_exhausted` → `True` при `closed_at`, `False` при `unlimited_at`, иначе прежняя арифметика.

- [ ] **Step 1: Написать падающие тесты**

В `tests/crud/test_premium_traffic_state.py` (импорт `set_unlimited` добавить в
существующий блок импортов; `SimpleNamespace`, `sync_premium_state_limits`,
`timedelta` уже импортированы) добавить тесты по образцу соседних:

- `test_unlimited_is_never_exhausted_even_above_the_limit` — `state.used_bytes = 10 * FIVE_GB`, `set_unlimited(state, True, now=NOW)`, commit, `get_state`; assert `reread.unlimited_at == NOW` и `reread.is_exhausted is False`.
- `test_closed_wins_over_unlimited` — `state.closed_at = NOW`, `set_unlimited(True)`; assert `state.is_exhausted is True`.
- `test_new_period_keeps_unlimited_and_zeroes_usage` — `set_unlimited(True)`, `start_new_period(state, period_start_at=NOW + timedelta(days=30), limit_bytes=FIVE_GB)`; assert `unlimited_at == NOW`, `used_bytes == 0`, `is_limited is False`.
- `test_revoke_clears_unlimited_and_resets_warning_marks` — `set_unlimited(True)`, поднять `notified_80/90`, `set_unlimited(False)`; assert `unlimited_at is None`, оба флага `False`.
- `test_tariff_limit_sync_keeps_unlimited` — `set_unlimited(True)`, commit, `sync_premium_state_limits(db, 1, SimpleNamespace(server_traffic_limits={SQUAD: {'traffic_limit_gb': 10}}))`, `get_state`; assert `unlimited_at == NOW`.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `pytest tests/crud/test_premium_traffic_state.py -q`
Expected: FAIL — `set_unlimited` не импортируется, `is_exhausted` не знает про безлимит.

- [ ] **Step 3: Реализовать**

`SubscriptionPremiumTraffic.is_exhausted`:

```python
@property
def is_exhausted(self) -> bool:
    """Исчерпан ли лимит. Закрытие сильнее безлимита, безлимит — сильнее чисел."""
    if self.closed_at is not None:
        return True
    if self.unlimited_at is not None:
        return False
    if self.total_limit_bytes <= 0:
        return False
    return (self.used_bytes or 0) >= self.total_limit_bytes
```

`app/database/crud/premium_traffic.py`, рядом с `add_extra_bytes`:

```python
def set_unlimited(
    state: SubscriptionPremiumTraffic,
    granted: bool,
    *,
    now: datetime | None = None,
) -> SubscriptionPremiumTraffic:
    """Выдать или снять безлимит на сквад.

    `is_limited` намеренно не трогаем: если сквад был снят за расход, именно
    этот флаг — сигнал воркеру вернуть его (`is_limited and not is_exhausted`).
    Закрытие (`closed_at`) тоже не трогаем: безлимит не открывает закрытый сквад.
    """
    state.unlimited_at = (now or datetime.now(UTC)) if granted else None
    state.notified_80 = False
    state.notified_90 = False
    return state
```

- [ ] **Step 4: Проверить**

Run: `pytest tests/crud/test_premium_traffic_state.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/database/models.py app/database/crud/premium_traffic.py tests/crud/test_premium_traffic_state.py
git commit -m "feat: add unlimited premium state semantics"
```

---

### Task 3: Осиротевшие строки переживают уход сквада из тарифа

**Files:**
- Modify: `app/services/premium_traffic_service.py` (`_Orphan`, `_collect_orphans`, `_clear_orphan`)
- Modify: `app/database/crud/subscription.py` (`_drop_orphan_premium_states`)
- Test: `tests/services/test_premium_traffic_service.py`, `tests/crud/test_drop_orphan_premium_states.py`

**Interfaces:**
- Consumes: `SubscriptionPremiumTraffic.unlimited_at` (Task 1).
- Produces: `_Orphan.unlimited_at: datetime | None`; безлимитная осиротевшая строка не удаляется, а `is_limited` у неё снимается, если сквад ещё подключён.

- [ ] **Step 1: Написать падающие тесты**

В `tests/services/test_premium_traffic_service.py`, в `TestOrphanStates`, по
образцу `test_closed_squad_stays_closed_after_leaving_the_premium_config`
(`_seed_subscription(db, premium_limits={})`, `_seed_state`, ручная установка
`state.unlimited_at = NOW`, `_run_worker_against`, `process_once`):

- `test_unlimited_state_survives_leaving_the_premium_config` — `is_limited=False`; assert `get_state(...).unlimited_at == NOW`, `stats['cleaned'] == 0`, `pushed == []`.
- `test_unlimited_orphan_with_removed_squad_is_restored_but_row_kept` — `is_limited=True`; assert строка осталась с `unlimited_at == NOW`, `is_limited is False`, `stats['cleaned'] == 0`, `pushed == [[SQUAD]]`.

В `tests/crud/test_drop_orphan_premium_states.py`:

- `test_unlimited_state_survives_tariff_switch_that_drops_the_squad` — `state.is_limited = True`, `state.unlimited_at = NOW`, `_drop_orphan_premium_states(db, _subscription(), _tariff({}))`; assert `get_state(...)` не `None`, `unlimited_at == NOW`, `is_limited is False`.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `pytest tests/services/test_premium_traffic_service.py tests/crud/test_drop_orphan_premium_states.py -q`
Expected: FAIL — строки с безлимитом удаляются или остаются с `is_limited=True`.

- [ ] **Step 3: Реализовать в воркере**

`_Orphan`: добавить поле `unlimited_at: datetime | None`; в `_collect_orphans`
передать `unlimited_at=state.unlimited_at`.

`_clear_orphan`: порядок веток —

1. `closed_at` — как сейчас, без изменений.
2. Вычислить `needs_restore` (как сейчас).
3. Если `needs_restore and orphan.panel_user_id is None` — предупреждение и `return False` (как сейчас).
4. Если `orphan.unlimited_at is not None`: строку не удалять. Если
   `needs_restore`, взять состояние через `get_state`, выставить
   `state.is_limited = False`, `flush` и отправить набор сквадов в панель внутри
   `db.begin_nested()` + `_log_panel_failure` (тот же контракт, что у
   `_restore_squad`); после успеха `await db.commit()`. Залогировать
   «безлимит сохранён» и вернуть `False` (строка не «очищена»).
5. Иначе — прежняя ветка удаления.

- [ ] **Step 4: Реализовать в смене тарифа**

`_drop_orphan_premium_states`: разделить сохраняемые строки на
`closed` (как сейчас) и `unlimited` (только где `closed_at is None`); для
`unlimited` выставить `state.is_limited = False` перед тем, как оставить строку.
Залогировать оба набора отдельно; `orphaned = set(not_in_new_tariff) - closed - unlimited`.

- [ ] **Step 5: Проверить**

Run: `pytest tests/services/test_premium_traffic_service.py tests/crud/test_drop_orphan_premium_states.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/premium_traffic_service.py app/database/crud/subscription.py tests/services/test_premium_traffic_service.py tests/crud/test_drop_orphan_premium_states.py
git commit -m "feat: preserve unlimited premium states across tariff changes"
```

---

### Task 4: Воркер не снимает и не предупреждает безлимит

**Files:**
- Modify: `app/services/premium_traffic_service.py` (`_apply_usage`, ветка предупреждения)
- Test: `tests/services/test_premium_traffic_service.py` (`_state`, `TestDecisions`)

**Interfaces:**
- Consumes: `is_exhausted` (Task 2).
- Produces: безлимит не снимается и не получает 90-процентное предупреждение; закрытый безлимит не возвращается.

- [ ] **Step 1: Обновить двойник `_state`**

Добавить параметр `unlimited_at=None`, поле `self.unlimited_at = unlimited_at` и
повторить модель в `is_exhausted`: `closed_at` → `True`, `unlimited_at` → `False`,
иначе прежняя формула.

- [ ] **Step 2: Написать падающие тесты**

В `TestDecisions`, по образцу `test_exhausted_squad_is_limited_and_pushed`:

- `test_unlimited_squad_is_not_limited_above_the_limit` — `_state(limit_gb=5, used_bytes=100 * BYTES_IN_GB, unlimited_at=NOW)`, расход `100 * BYTES_IN_GB`; assert `outcome is None`, `state.is_limited is False`, `pushed == []`.
- `test_unlimited_squad_gets_no_ninety_percent_warning` — `_state(limit_gb=5, unlimited_at=NOW)`, расход `5 * BYTES_IN_GB`; assert `outcome is None`, `warnings.assert_not_awaited()`.
- `test_granting_unlimited_restores_a_limited_squad` — `_state(limit_gb=5, used_bytes=5 * BYTES_IN_GB, is_limited=True, unlimited_at=NOW)`; assert `outcome == 'restored'`, `is_limited is False`, `pushed == [1]`.
- `test_closed_unlimited_squad_is_not_restored` — `closed_at=NOW`, `is_limited=True`, `unlimited_at=NOW`; assert `outcome is None`, `is_limited is True`, `pushed == []`.

- [ ] **Step 3: Убедиться, что тесты падают**

Run: `pytest tests/services/test_premium_traffic_service.py -q`
Expected: FAIL — предупреждение на 90 % всё ещё отправляется безлимитному скваду.

- [ ] **Step 4: Реализовать**

В `_apply_usage`, в ветке предупреждения, добавить условие:
`if not state.is_limited and state.unlimited_at is None:`. Остальные ветки уже
работают через `is_exhausted`; не менять их.

- [ ] **Step 5: Проверить**

Run: `pytest tests/services/test_premium_traffic_service.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/premium_traffic_service.py tests/services/test_premium_traffic_service.py
git commit -m "feat: skip premium limits and warnings for unlimited squads"
```

---

### Task 5: Карточка сквада в боте — состояние, текст, кнопки

**Files:**
- Modify: `app/handlers/admin/users.py` (`PremiumSquadCardState`, `_classify_premium_traffic_state`, `_format_premium_traffic_row`, `_format_premium_traffic_summary`, `_render_premium_squad_detail`)
- Test: `tests/handlers/test_admin_users_premium.py`

**Interfaces:**
- Produces: `PremiumSquadCardState.UNLIMITED`.
- Produces: кнопки `admin_user_premium_unlimited_{user_id}{_sid}_{idx}` и `admin_user_premium_unlimited_revoke_{user_id}{_sid}_{idx}` (обработчики — Task 6).

- [ ] **Step 1: Обновить тестовый двойник**

В `_bare_state` добавить `unlimited_at=None` и передать его в конструктор
`SubscriptionPremiumTraffic`. В докстринге тестового модуля добавить новое
состояние — безлимит.

- [ ] **Step 2: Написать падающие тесты**

По образцу соседних тестов классификации/рендера:

- `test_unlimited_is_classified_before_exhausted` — `_bare_state(is_limited=True, used_bytes=5 * BYTES_IN_GB, unlimited_at=NOW)` → `UNLIMITED`.
- `test_closed_beats_unlimited` — `closed_at=NOW, unlimited_at=NOW` → `CLOSED`.
- `test_row_rendering_unlimited_shows_infinity_and_grant_date` — рендер `UNLIMITED` содержит `♾️`, «безлимит» и имя `DE-1`.
- `test_summary_counts_unlimited` — `_format_premium_traffic_summary` с одной строкой `state.unlimited_at=NOW` содержит `♾️`.
- `test_keyboard_offers_revoke_when_unlimited` — состояние с `unlimited_at=NOW`; в клавиатуре детали есть `admin_user_premium_unlimited_revoke_`, нет `admin_user_premium_grant_`.
- `test_keyboard_offers_grant_when_not_unlimited` — строки без состояния; есть `admin_user_premium_unlimited_`, нет `..._revoke_`.

Для тестов клавиатуры: `_render_premium_squad_detail(callback, db, 1, None, 0)`
с замоканными `_resolve_admin_subscription` и `get_squad_display_names`;
callback'и достать из `callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard`.

- [ ] **Step 3: Убедиться, что тесты падают**

Run: `pytest tests/handlers/test_admin_users_premium.py -q`
Expected: FAIL — нет `UNLIMITED`, рендер и клавиатура не знают о флаге.

- [ ] **Step 4: Реализовать**

- `PremiumSquadCardState.UNLIMITED = 'unlimited'`; обновить докстринг класса.
- `_classify_premium_traffic_state`: после `closed_at` и до `is_limited` —
  `state.unlimited_at is not None` → `UNLIMITED`.
- `_format_premium_traffic_row`: ветка `UNLIMITED` — дата выдачи, израсходовано,
  при `is_limited` строка «воркер вернёт сквад», предупреждение про общий лимит.
- `_format_premium_traffic_summary`: бейдж `♾️ безлимит: N` для строк без
  `closed_at`.
- `_render_premium_squad_detail`: если `state.unlimited_at` и не `CLOSED` —
  «♾️ Снять безлимит»; иначе при `config.limit_gb > 0` — «📊 Выдать ГБ» и
  «♾️ Выдать безлимит». «📊 Выдать ГБ» при безлимите не показывать.
  «🔒 Закрыть доступ» / «🔓 Открыть доступ» — без изменений.

- [ ] **Step 5: Проверить**

Run: `pytest tests/handlers/test_admin_users_premium.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/handlers/admin/users.py tests/handlers/test_admin_users_premium.py
git commit -m "feat: show unlimited premium state in admin card"
```

---

### Task 6: Выдача и снятие безлимита из бота

**Files:**
- Modify: `app/handlers/admin/users.py` (импорты, четыре обработчика, регистрация, guard в `_grant_premium_traffic`)
- Test: `tests/handlers/test_admin_users_premium.py`

**Interfaces:**
- Consumes: `set_unlimited` (Task 2), `premium_traffic_service.request_pass()` (sync, без аргументов), `_render_premium_squad_detail` (Task 5).
- Produces: `ask_grant_premium_unlimited`, `confirm_grant_premium_unlimited`, `ask_revoke_premium_unlimited`, `confirm_revoke_premium_unlimited`.

- [ ] **Step 1: Написать падающие тесты**

Новый класс `TestUnlimitedGrantRevoke`; замокать `_resolve_admin_subscription`,
`get_squad_display_names`, `users_mod.premium_traffic_service.request_pass`.

- `test_confirm_grant_sets_unlimited_and_wakes_worker` — состояние создано заранее, callback `admin_user_premium_unlimited_confirm_1_0`; assert `state.unlimited_at is not None`, `wake.assert_called_once()`.
- `test_confirm_grant_creates_missing_state_and_wakes_worker` — состояния нет; после handler'а `get_state` не `None` и `unlimited_at` стоит.
- `test_confirm_grant_refuses_closed_squad` — `closed_at=NOW`; `unlimited_at is None`; `callback.answer` с `'❌ Сквад закрыт администратором — сначала откройте доступ'`, `show_alert=True`.
- `test_confirm_grant_is_idempotent` — `unlimited_at=NOW`; после handler'а timestamp не изменился, `wake.assert_not_called()`.
- `test_confirm_revoke_clears_unlimited_and_wakes_worker` — callback `admin_user_premium_unlimited_revoke_confirm_1_0`; `unlimited_at is None`, `wake.assert_called_once()`.
- `test_confirm_revoke_when_not_unlimited_does_not_change_state` — `callback.answer` с `'♾️ Безлимит уже снят'`, `show_alert=True`; состояние не изменилось.
- `test_grant_gb_refuses_when_unlimited` — `_grant_premium_traffic(..., gb=10, ...)` при `unlimited_at=NOW`; `ok is False`, в сообщении «безлимит».
- `test_extract_unlimited_callbacks` — `_extract_premium_squad_context` для `admin_user_premium_unlimited_confirm_123_s456_0` и `..._revoke_confirm_...` → `(123, 456, 0)`.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `pytest tests/handlers/test_admin_users_premium.py -q`
Expected: FAIL — обработчиков нет.

- [ ] **Step 3: Реализовать выдачу**

Импортировать `set_unlimited` и `premium_traffic_service` в `users.py`.

`ask_grant_premium_unlimited`: текст с предупреждением про общий лимит,
`get_confirmation_keyboard(confirm_cb, detail_cb, language)`,
`confirm_cb = admin_user_premium_unlimited_confirm_{user_id}{_sid}_{idx}`.

`confirm_grant_premium_unlimited`: загрузить подписку и строки; проверить индекс;
`config.limit_gb > 0`; взять `state` из строки или создать через
`get_or_create_state`; отказ при `closed_at` с текстом
`'❌ Сквад закрыт администратором — сначала откройте доступ'`; отказ при уже
выданном безлимите с текстом `'♾️ Безлимит уже действует'` (без `request_pass`);
`set_unlimited(state, True, now=datetime.now(UTC))`; `commit`;
`premium_traffic_service.request_pass()`; лог; перерисовать карточку
(`callback.data = f'admin_user_premium_sq_{user_id}{_sid}_{idx}'`,
`_render_premium_squad_detail`), затем `callback.answer()`.

- [ ] **Step 4: Реализовать снятие**

`ask_revoke_premium_unlimited`: подтверждение с текстом «вернётся лимит тарифа;
если расход выше — воркер снимет; чтобы отобрать сразу — закройте доступ»,
`confirm_cb = admin_user_premium_unlimited_revoke_confirm_{user_id}{_sid}_{idx}`.

`confirm_revoke_premium_unlimited`: `state` обязана быть и иметь `unlimited_at`,
иначе `'♾️ Безлимит уже снят'` (show_alert, без изменений);
`set_unlimited(state, False)`; `commit`; `request_pass`; лог; перерисовать
карточку.

- [ ] **Step 5: Зарегистрировать callback'и и закрыть докупку**

Регистрация — до общего `show_premium_traffic_screen`, с `_unlimited_` в его
исключениях:

```python
dp.callback_query.register(confirm_grant_premium_unlimited, F.data.startswith('admin_user_premium_unlimited_confirm_'))
dp.callback_query.register(confirm_revoke_premium_unlimited, F.data.startswith('admin_user_premium_unlimited_revoke_confirm_'))
dp.callback_query.register(ask_revoke_premium_unlimited, F.data.startswith('admin_user_premium_unlimited_revoke_') & ~F.data.contains('confirm'))
dp.callback_query.register(ask_grant_premium_unlimited, F.data.startswith('admin_user_premium_unlimited_') & ~F.data.contains('confirm') & ~F.data.contains('revoke'))
```

В `_grant_premium_traffic` после `get_or_create_state`: если
`state.unlimited_at is not None` — вернуть `False, '❌ На этом сервере уже
действует безлимит — докупка не нужна'`.

- [ ] **Step 6: Проверить**

Run: `pytest tests/handlers/test_admin_users_premium.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/handlers/admin/users.py tests/handlers/test_admin_users_premium.py
git commit -m "feat: grant and revoke unlimited premium access in bot"
```

---

### Task 7: Меню пользователя и живое меню

**Files:**
- Modify: `app/utils/rich_menu.py` (`premium_squad_lines`, `live_menu_premium_state`)
- Test: `tests/utils/test_premium_squad_lines.py`, `tests/services/test_live_menu.py`

**Interfaces:**
- Consumes: `unlimited_at` (Task 1).
- Produces: строка `used / ♾️`; пятый элемент отпечатка — `bool(row.unlimited_at)`.

- [ ] **Step 1: Написать падающие тесты**

В `tests/utils/test_premium_squad_lines.py` (добавить `from datetime import UTC, datetime`):

- `test_unlimited_squad_line_shows_infinity` — подписка с `connected_squads=['wl']`, тариф с `{'wl': {'traffic_limit_gb': 5}}`, состояние `used_bytes=2 * 1024**3`, `total_limit_bytes=5 * 1024**3`, `unlimited_at=datetime(2026, 9, 17, tzinfo=UTC)`; замокать `app.database.crud.premium_traffic.get_states_for_subscription` и `app.database.crud.server_squad.get_squad_display_names`; assert одна строка, в ней `WL` и `♾️`.

В `tests/services/test_live_menu.py`:

- Обновить `test_live_menu_premium_state_rounds_usage_to_whole_gigabytes`: добавить `unlimited_at` в строки и пятый элемент в ожидаемый кортеж (`False` / `True`).
- `test_live_menu_fingerprint_tracks_unlimited_flag` — `live_menu_fingerprint` с `((1, 'wl', 3, 10 * GIB, False),)` и `((1, 'wl', 3, 10 * GIB, True),)` различаются.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `pytest tests/utils/test_premium_squad_lines.py tests/services/test_live_menu.py -q`
Expected: FAIL — «♾️» не выводится, отпечаток не различает флаг.

- [ ] **Step 3: Реализовать**

- `premium_squad_lines`: если `state is not None and state.unlimited_at is not None`, строка `used / ♾️`; иначе прежняя формула.
- `live_menu_premium_state`: добавить `bool(row.unlimited_at)` пятым элементом кортежа.

- [ ] **Step 4: Проверить**

Run: `pytest tests/utils/test_premium_squad_lines.py tests/services/test_live_menu.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/utils/rich_menu.py tests/utils/test_premium_squad_lines.py tests/services/test_live_menu.py
git commit -m "feat: render unlimited premium squads as infinity"
```

---

### Task 8: Прокси страницы подписок

**Files:**
- Modify: `app/webserver/subscription_proxy.py` (`premium_usage`)
- Test: `tests/webserver/test_subscription_proxy.py`

**Interfaces:**
- Consumes: `unlimited_at` (Task 1).
- Produces: `(used, 0)` при хотя бы одном безлимитном скваде; иначе прежняя сумма.

- [ ] **Step 1: Написать падающий тест**

Расширить `_state` в тесте параметром `unlimited_at=None`; добавить импорт
`from datetime import UTC, datetime` и тест:

```python
async def test_premium_usage_unlimited_squad_reports_zero_total(premium_env):
    premium_env.extend([_state('wl-1', 3, 10, unlimited_at=datetime(2026, 9, 6, tzinfo=UTC))])

    used, total = await premium_usage(_FakeDb([_subscription()]), ['Ab3dEf9hJk'])

    assert used == 3 * BYTES_IN_GB
    assert total == 0
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `pytest tests/webserver/test_subscription_proxy.py -q`
Expected: FAIL — `total` равен сумме лимитов.

- [ ] **Step 3: Реализовать**

В `premium_usage`: при обходе состояний, если `state.unlimited_at is not None`,
запомнить `unlimited = True` и не добавлять `state.total_limit_bytes` в `total`.
В конце вернуть `(used, 0 if unlimited else total)`.

- [ ] **Step 4: Проверить**

Run: `pytest tests/webserver/test_subscription_proxy.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/webserver/subscription_proxy.py tests/webserver/test_subscription_proxy.py
git commit -m "feat: report unlimited premium traffic in subscription header"
```

---

### Task 9: Докупка не предлагает безлимитный сквад

**Files:**
- Modify: `app/services/premium_traffic_purchase.py` (проверка `unlimited_at`)
- Modify: `app/handlers/subscription/traffic.py` (`_get_purchasable_premium_squads`)
- Test: `tests/services/test_premium_traffic_purchase.py`, `tests/handlers/test_premium_topup_flow.py`

**Interfaces:**
- Consumes: `unlimited_at` (Task 1).
- Produces: `PremiumTopupError('squad_unlimited', ...)`; безлимитный сквад не попадает в список покупок.

- [ ] **Step 1: Написать падающие тесты**

В `tests/services/test_premium_traffic_purchase.py`, по образцу
`test_closed_squad_refuses_the_purchase` и
`test_apply_refuses_a_squad_closed_after_the_quote`:

- `test_quote_refuses_unlimited_squad` — состояние с `unlimited_at=NOW`; `quote_premium_topup(db, _subscription(), SQUAD, 5)` бросает `PremiumTopupError` с `code == 'squad_unlimited'`.
- `test_apply_refuses_unlimited_set_after_the_quote` — quote получен, затем `state.unlimited_at = NOW`; `apply_premium_topup(...)` бросает `squad_unlimited`.

В `tests/handlers/test_premium_topup_flow.py`, в `TestPurchasableSquads`:

- `test_unlimited_squad_is_not_offered` — состояние с `unlimited_at=NOW`; `_get_purchasable_premium_squads` возвращает `[]`.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `pytest tests/services/test_premium_traffic_purchase.py tests/handlers/test_premium_topup_flow.py -q`
Expected: FAIL — покупка проходит, сквад показывается.

- [ ] **Step 3: Реализовать**

- В `premium_traffic_purchase.py` добавить `_ensure_squad_is_not_unlimited(state)` с `PremiumTopupError('squad_unlimited', 'На этом сервере уже действует безлимит, докупка не нужна')`; вызвать в `quote_premium_topup` и `apply_premium_topup` сразу после `_ensure_squad_is_not_closed`.
- В `_get_purchasable_premium_squads` исключить состояния, где `closed_at is not None` или `unlimited_at is not None`.

- [ ] **Step 4: Проверить**

Run: `pytest tests/services/test_premium_traffic_purchase.py tests/handlers/test_premium_topup_flow.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/premium_traffic_purchase.py app/handlers/subscription/traffic.py tests/services/test_premium_traffic_purchase.py tests/handlers/test_premium_topup_flow.py
git commit -m "feat: refuse premium top-up for unlimited squads"
```

---

### Task 10: Документация и changelog

**Files:**
- Modify: `docs/premium-traffic-limits.md`
- Modify: `../claude-changes.md` (вне репозитория)

- [ ] **Step 1: Дописать `docs/premium-traffic-limits.md`**

Новый раздел «Безлимит на сквад»: `unlimited_at`, приоритет `closed_at`,
поведение при сбросе периода, смене тарифа и осиротении, отображение «♾️»,
`total=0` в прокси, отказ докупки и предупреждение про общий лимит панели.
Обновить раздел «Четыре состояния сквада» — добавить `UNLIMITED`.

- [ ] **Step 2: Добавить запись в `../claude-changes.md`**

```markdown
## 2026-11-22 — безлимит на премиум-сквад

**Scope:** `SubscriptionPremiumTraffic`, воркер премиум-трафика, карточка сквада в боте, меню/прокси, докупка.
**What:** добавлен `unlimited_at`; выдача/снятие безлимита в боте; безлимит переживает сброс периода и смену тарифа, `closed_at` остаётся сильнее; меню показывает «♾️», прокси отдаёт `total=0`; докупка для безлимитного сквада отклоняется.
**Why:** нужен штатный способ выдать «своему» человеку безлимит на конкретный премиум-сквад, не меняя тариф и не выдавая астрономические числа.
```

- [ ] **Step 3: Commit**

```bash
git add docs/premium-traffic-limits.md
git commit -m "docs: describe unlimited premium squads"
```

(`../claude-changes.md` лежит вне репозитория и в commit не входит.)

---

### Task 11: Финальная проверка

- [ ] **Step 1: Прогнать затронутые тесты**

```bash
pytest tests/database/test_premium_traffic_schema_parity.py tests/database/test_migration_chain.py tests/crud/test_premium_traffic_state.py tests/crud/test_drop_orphan_premium_states.py tests/services/test_premium_traffic_service.py tests/handlers/test_admin_users_premium.py tests/utils/test_premium_squad_lines.py tests/services/test_live_menu.py tests/webserver/test_subscription_proxy.py tests/services/test_premium_traffic_purchase.py tests/handlers/test_premium_topup_flow.py tests/services/test_premium_squads_are_filtered_everywhere.py tests/services/test_premium_traffic_multi_tariff.py tests/services/test_grace_respects_premium_limits.py tests/cabinet/test_admin_premium_traffic.py -q
```

Expected: PASS.

- [ ] **Step 2: Линтер**

```bash
ruff check app/database/models.py app/database/crud/premium_traffic.py app/database/crud/subscription.py app/services/premium_traffic_service.py app/services/premium_traffic_purchase.py app/handlers/admin/users.py app/handlers/subscription/traffic.py app/utils/rich_menu.py app/webserver/subscription_proxy.py tests/crud/test_premium_traffic_state.py tests/crud/test_drop_orphan_premium_states.py tests/services/test_premium_traffic_service.py tests/handlers/test_admin_users_premium.py tests/utils/test_premium_squad_lines.py tests/services/test_live_menu.py tests/webserver/test_subscription_proxy.py tests/services/test_premium_traffic_purchase.py tests/handlers/test_premium_topup_flow.py
ruff format --check app/database/models.py app/database/crud/premium_traffic.py app/database/crud/subscription.py app/services/premium_traffic_service.py app/services/premium_traffic_purchase.py app/handlers/admin/users.py app/handlers/subscription/traffic.py app/utils/rich_menu.py app/webserver/subscription_proxy.py
```

Expected: без ошибок.
