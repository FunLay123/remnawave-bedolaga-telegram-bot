# Referral Trial Carry-Over Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Переносить при первой покупке любого тарифа только неиспользованные реферальные дни `days_only`-триала, не перенося базовый бесплатный срок.

**Architecture:** Хранить дату окончания базового триала отдельно от общего `end_date`. Один помощник вычисляет переносимый остаток; все существующие пути конвертации используют его, не меняя остальные правила покупки.

**Tech Stack:** Python 3.14, SQLAlchemy async, Alembic, aiogram 3, FastAPI, pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-11-15-referral-trial-carry-over-design.md`

## Global Constraints

- Точная формула для новой `days_only`-строки: `max(0, end_date - max(now, base_trial_end_date))`, без округления до суток.
- Перенос действует при покупке любого тарифа; текущий приёмочный сценарий — один активный тариф, отдельная поддержка multi-tariff не расширяется. Новых env-настроек нет.
- Не менять `is_trial`, автоплатёж, трафик, устройства, сквады или panel identity при реферальном продлении.
- Старые строки с `base_trial_end_date IS NULL` сохраняют прежнее поведение; не делать backfill по ledger.
- Follow TDD: сначала запустить названный failing test, затем реализацию и повторный запуск.
- После каждой задачи добавить одну запись в `E:/Misc/repository/bedolaga/claude-changes.md`; секреты и персональные данные не записывать.
- Не делать commit/push/deploy без отдельной просьбы пользователя; не добавлять attribution trailers.
- Триал в этой установке бесплатный: `TRIAL_ACTIVATION_PRICE=0`; не менять спящие paid-trial ветки ради этой задачи.
- `create_trial_subscription` получает `duration_days` из двух видов вызовов: referral-aware вызовы уже применили `get_effective_trial_days`, а admin/webapi/promocode передают базовый срок без бонуса. Добавить keyword-only `duration_days_include_referral_bonus: bool = False`; пять referral-aware вызовов передают `True`, остальные сохраняют базовый срок без вычитания бонуса.
- `deactivate_user_trial_subscriptions` ставит `is_trial=False`. Для дополнительных `killed_trials` вычислить `get_trial_carry_over_delta` ДО деактивации (снимок живых триалов через существующий `get_active_subscriptions_by_user_id`, фильтр `is_trial` и exclude id), затем сопоставить дельты с возвращёнными записями по id; не ослаблять правило «не-trial → нулевой остаток».

## Review Focus

- Покупка того же тарифа не должна обходить новый расчёт через ветку обычного продления (Task 2 tests).
- Повторная активация PENDING-триала не должна считать реферальный бонус базовым сроком (Task 1 tests).
- Истёкший триал с ещё живым реферальным хвостом должен переносить хвост от `now`, а не от старого `end_date` (Task 2 tests).
- Дополнительные multi-tariff триалы не добавляются в этот сценарий: при одном тарифе конвертируется одна текущая подписка, без повторного переноса её хвоста (Task 3 tests).
- `NULL` на старой записи и схема `levels` должны сохранять прежнее поведение (Task 1 tests).

---

### Task 1: Базовая дата триала и единый расчёт переносимого остатка

**Files:**
- Modify: `app/database/models.py:2644-2649`
- Modify: `app/database/crud/subscription.py:176-287,1090-1116,1195-1244`
- Modify: `app/services/trial_activation_service.py:52-87`
- Modify referral-aware callers to pass `duration_days_include_referral_bonus=True`: `app/handlers/subscription/purchase.py` (2 calls), `app/handlers/start.py`, `app/webapi/routes/miniapp.py`, `app/cabinet/routes/subscription_modules/purchase.py`
- Create: `migrations/alembic/versions/0137_subscription_base_trial_end_date.py`
- Test: `tests/crud/test_referral_trial_carry_over.py`
- Test: `tests/test_trial_activation_referral.py`
- Changelog: `E:/Misc/repository/bedolaga/claude-changes.md`

**Interfaces:**
- Produces: `Subscription.base_trial_end_date: datetime | None`.
- Produces: `get_base_trial_duration_days(user: User, total_days: int) -> int` — вычитает фактически применимый `REFERRAL_TRIAL_BONUS_DAYS` из полного срока, не опуская результат ниже 0.
- Produces: `get_trial_carry_over_delta(subscription: Subscription, now: datetime) -> timedelta` — общий расчёт для всех путей покупки.
- Consumes: существующие `get_effective_trial_days`, `settings.is_referral_days_only_scheme()`, `should_carry_trial_remaining_days()`.

- [ ] **Step 1: Написать failing tests создания триала и границ расчёта**

В `tests/crud/test_referral_trial_carry_over.py` проверить на реальной in-memory модели:

```python
# base=3 дня, referral bonus=20, полный end_date=now+23 дня:
assert created.base_trial_end_date == created.start_date + timedelta(days=3)
assert get_trial_carry_over_delta(created, created.start_date) == timedelta(days=20)
assert get_trial_carry_over_delta(created, created.base_trial_end_date + timedelta(days=5)) == timedelta(days=15)
assert get_trial_carry_over_delta(created, created.end_date) == timedelta()
```

Добавить тесты: обычный приглашённый триал без бонуса имеет `base_trial_end_date == end_date`; повторная активация PENDING пересчитывает базовую дату от нового `start_date`; `add_reward_days` меняет только общий `end_date`; для `levels` и для `base_trial_end_date=None` помощник использует прежний `should_carry_trial_remaining_days()`. Отдельно проверить, что base-only вызов для приглашённого пользователя (`duration_days_include_referral_bonus=False`, значение по умолчанию) НЕ вычитает бонус, а referral-aware вызов с `True` вычитает фактически применимый бонус.

- [ ] **Step 2: Запустить tests и убедиться в ожидаемом FAIL**

Run: `uv run pytest tests/crud/test_referral_trial_carry_over.py tests/test_trial_activation_referral.py -q`
Expected: FAIL из-за отсутствия поля и новых помощников.

- [ ] **Step 3: Добавить модель, миграцию и инициализацию базового срока**

Добавить nullable `base_trial_end_date = Column(AwareDateTime(), nullable=True)` рядом с `start_date`/`end_date`. Миграция `0137`, `down_revision='0136'`, добавляет `sa.DateTime(timezone=True)` через `batch_alter_table`, без server default и backfill; downgrade удаляет колонку. Проверка существования колонки следует шаблону `0134`.

В `create_trial_subscription` получить привязку пользователя. Рассчитать базовую длительность через `get_base_trial_duration_days` только при `duration_days_include_referral_bonus=True`; иначе базовой считается фактически переданный `duration_days`, потому что admin/webapi/promocode не применяют реферальный бонус. Пять перечисленных выше referral-aware вызовов передать `True`. Для нового и PENDING-триала установить `base_trial_end_date = start_date + timedelta(days=base_days)`. Не добавлять второй бонус и не менять общий `end_date`.

- [ ] **Step 4: Реализовать общий помощник остатка**

`get_trial_carry_over_delta` возвращает нулевой `timedelta` для не-trial или отсутствующего `end_date`. Для `days_only` с известной базовой датой применяет формулу из Global Constraints. Иначе возвращает положительный `end_date-now`, только если `should_carry_trial_remaining_days()` истинно. Не изменять `add_reward_days` или `grant_reward_days` для сохранения базовой даты.

- [ ] **Step 5: Проверить задачу и записать changelog**

Run: `uv run pytest tests/crud/test_referral_trial_carry_over.py tests/test_trial_activation_referral.py tests/crud/test_add_reward_days.py tests/crud/test_trial_subscription_idempotency.py -q`
Expected: all PASS. Добавить запись о модели, миграции и помощнике во внешний changelog; не коммитить без просьбы пользователя.

### Task 2: Центральная конверсия и покупка после истечения базового триала

**Files:**
- Modify: `app/database/crud/subscription.py:346-426,459-531,1247-1416`
- Test: `tests/crud/test_referral_trial_carry_over.py`
- Test: `tests/crud/test_subscription_revive.py`
- Test: `tests/crud/test_trial_conversion_on_paid_purchase.py`
- Test: `tests/crud/test_free_tariff_day_reset.py`
- Changelog: `E:/Misc/repository/bedolaga/claude-changes.md`

**Interfaces:**
- Consumes: `get_trial_carry_over_delta(subscription, now) -> timedelta` и `Subscription.base_trial_end_date` из Task 1.
- Produces: `extend_subscription(...)` переносит реферальный остаток один раз при конверсии любого живого триала, независимо от смены `tariff_id`.
- Produces: `_revive_paid_subscription(...)` сохраняет только живой реферальный хвост истёкшего триала и стартует платный период от `now`.

- [ ] **Step 1: Написать failing tests центральных путей**

Добавить проверку покупки текущего единственного тарифа: `3 базовых +20 реферальных +30 купленных = now+50 дней`; остаток не считается дважды. Отдельно проверить, что при `convert_trial=False` бесплатное продление по-прежнему использует старое поведение. Существующие тесты другого тарифа остаются регрессией, но не являются отдельной новой функциональностью.

В `tests/crud/test_subscription_revive.py` создать EXPIRED-триал, у которого базовая дата в прошлом, а общий `end_date` на 10 дней впереди. После покупки 30 дней ожидается `now+40`, `is_trial=False` и только один перенос хвоста. Полностью истёкший хвост даёт ровно 30 дней. Существующий тест живой платной подписки должен по-прежнему продлевать от её `end_date`.

- [ ] **Step 2: Запустить tests и убедиться в FAIL**

Run: `uv run pytest tests/crud/test_referral_trial_carry_over.py tests/crud/test_subscription_revive.py -q`
Expected: FAIL: same-tariff conversion включает базовый срок; revive продлевает от старого общего `end_date`.

- [ ] **Step 3: Внедрить расчёт в конвертацию и revive**

В `extend_subscription` отдельно определить настоящую конверсию: `subscription.is_trial and convert_trial and tariff_id is not None`. До изменения флага вычислить `get_trial_carry_over_delta`. Специальную ветку `start_date=now`, `end_date=now+purchased_days+carry_over` до generic same-tariff/tariff-change веток применять ТОЛЬКО для `days_only` с известной `base_trial_end_date`. Для legacy `NULL` и `levels` сохранить прежние generic ветки, включая same-tariff продление от прежнего `end_date` независимо от carry-флага; это требование Spec перекрывает более общую формулировку «всегда от now». Платные подписки и `convert_trial=False` оставить на прежних ветках. Добавить failing регрессии legacy `NULL` и `levels` с выключенным carry-флагом.

В `_revive_paid_subscription` проверить исходный `is_trial` до его сброса. Расчёт от `now` плюс помощник применять только для `days_only` с известной базовой датой: при живом реферальном хвосте сохранить его, при полностью истёкшем — только покупка. Для legacy `NULL` и `levels` оставить прежний revive от живого общего `end_date`; не менять платный revive и существующие side effects. Добавить соответствующие failing регрессии.

- [ ] **Step 4: Запустить центральные регрессии**

Run: `uv run pytest tests/crud/test_referral_trial_carry_over.py tests/crud/test_subscription_revive.py tests/crud/test_trial_conversion_on_paid_purchase.py tests/crud/test_free_tariff_day_reset.py tests/crud/test_extend_expired_keeps_tariff_traffic_limit.py -q`
Expected: all PASS; отсутствие двойного переноса подтверждено точными `end_date`.

- [ ] **Step 5: Записать changelog**

Описать единый расчёт same/other tariff и revive; не коммитить без просьбы пользователя.

### Task 3: Прямые пути бота, Mini App и кабинета

**Files:**
- Modify: `app/handlers/subscription/purchase.py:2570-2667`
- Modify: `app/services/subscription_purchase_service.py:1083-1177`
- Modify: `app/cabinet/routes/subscription_modules/purchase.py:1025-1149`
- Test: `tests/services/test_trial_conversion_carry_over.py`
- Test: `tests/services/test_referral_trial_carry_over_paths.py`
- Changelog: `E:/Misc/repository/bedolaga/claude-changes.md`

**Interfaces:**
- Consumes: `get_trial_carry_over_delta(subscription, now)` из Task 1 и исправленный `create_paid_subscription` из Task 2.
- Produces: все перечисленные entry points используют одинаковый остаток для текущей единственной подписки; существующие multi-tariff guard'ы остаются без расширения.

- [ ] **Step 1: Написать failing tests прямых путей**

Расширить `test_trial_conversion_carry_over.py`: Mini App при `days_only`, базовом остатке 1 день и реферальном остатке 19 дней переносит ровно 19; при `TRIAL_ADD_REMAINING_DAYS_TO_PAID=false` реферальный хвост всё равно переносится; платное продление остаётся без изменений.

В новом `test_referral_trial_carry_over_paths.py` проверить bot `confirm_purchase` с теми же числами и cabinet-покупку единственного тарифа: текущая подписка получает только реферальный хвост один раз. Использовать существующие mocks, без Telegram/RemnaWave/PostgreSQL и без создания дополнительных триалов.

- [ ] **Step 2: Запустить tests и убедиться в FAIL**

Run: `uv run pytest tests/services/test_trial_conversion_carry_over.py tests/services/test_referral_trial_carry_over_paths.py -q`
Expected: FAIL: прямые пути либо сгорают весь остаток по старому флагу, либо переносят базовый триал.

- [ ] **Step 3: Заменить локальные вычисления общим помощником**

В bot и Mini App вместо чтения полного `end_date-now` по `should_carry_trial_remaining_days()` заполнять `bonus_period` через `get_trial_carry_over_delta`; база конверсии остаётся `now`.

В cabinet и Mini App до `deactivate_user_trial_subscriptions` получить живые подписки через существующий `get_active_subscriptions_by_user_id`, отфильтровать `is_trial` и исключённую текущую/conversion подписку, вычислить словарь `id → get_trial_carry_over_delta(trial, now)`. После деактивации суммировать сохранённые дельты только для возвращённых `killed_trials` по id: функция деактивации уже сбросила `is_trial=False`, поэтому вызывать помощник для `_kt` после неё нельзя. Не включать `_conversion_trial` в этот список: его хвост обрабатывает Task 2. Не менять оплату, ledger первой покупки, панель или UI. Добавить регрессионный тест, который использует реальную деактивацию либо повторяет её сброс `is_trial=False` и доказывает, что предварительно сохранённый хвост не теряется.

- [ ] **Step 4: Запустить полный набор регрессий**

Run: `uv run pytest tests/services/test_trial_conversion_carry_over.py tests/services/test_referral_trial_carry_over_paths.py tests/crud/test_referral_trial_carry_over.py tests/crud/test_subscription_revive.py tests/crud/test_trial_conversion_on_paid_purchase.py tests/crud/test_add_reward_days.py tests/services/test_referral_first_purchase_service.py tests/test_trial_activation_referral.py -q`
Expected: all PASS.

Run: `uv run pytest tests/handlers/test_referral_days_only_screen.py tests/services/test_referral_post_commit.py tests/services/test_subscription_renewal_first_purchase_hook.py -q`
Expected: all PASS; награды за покупку и их post-commit эффекты не изменились.

- [ ] **Step 5: Записать финальный changelog**

Одна запись о покрытии bot/Mini App/cabinet и результатах тестов. Не коммитить, не запускать миграцию на рабочей БД и не deploy без отдельной просьбы пользователя.
