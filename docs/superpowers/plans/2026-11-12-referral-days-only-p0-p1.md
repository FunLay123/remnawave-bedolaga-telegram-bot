# План: referral days_only — P0 и P1

## Контекст

Ветка `redesign`. В рабочем дереве уже лежат незакоммиченные изменения аудита
(маркер поздней привязки, якорь регистрации, eligibility, бэкфилл и т.д.).
Пользователь не хочет коммитить до завершения глобального раунда поиска багов:
все изменения должны остаться в рабочем дереве и быть влиты одним раундом позже.

P0 меняет продуктовую логику: daily-покупка больше не считается первой покупкой
и не сжигает бонус; дни настраиваются отдельно для приглашённого и пригласившего.
P1 — страховка от редких повторных начислений: идемпотентный маркер, уникальный
индекс, ретраи на админских путях, изоляция тестов.

## Global Constraints

- **Не коммитить.** Никаких `git commit`, `git add -A`, `git stash`, `git reset
  --hard`, `git checkout -- .` и других операций, меняющих baseline. Рабочее
  дерево содержит чужие незакоммиченные изменения — их нельзя терять или
  перезаписывать.
- Менять только файлы, перечисленные в задаче. Не переформатировать соседний код.
- Python: `.venv/Scripts/python.exe -m pytest ...`.
- Стиль и язык комментариев — как в окружающем коде (в основном русский).
- После изменений добавить запись в `../claude-changes.md` (файл вне репозитория)
  по формату проекта. Без секретов и персональных данных.
- В прикладной логике не читать `REFERRAL_FIRST_PURCHASE_BONUS_DAYS` напрямую,
  кроме fallback внутри `app/config.py`. Использовать эффективные геттеры.
- Текущие значения пользователя: `REFERRAL_FIRST_PURCHASE_BONUS_DAYS=10`,
  `REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS=30`, `REFERRAL_REWARD_SCHEME=days_only`,
  `SALES_MODE=tariffs`. Суточные описания транзакций — захардкоженные русские
  литералы.

---

## Task 1 — P0: daily не считается первой покупкой, раздельные дни сторон

### Цель

1. Любая daily-покупка (первый день суточного тарифа, ежедневное списание,
   возобновление, авто-возобновление) **не** ставит маркер первой покупки и
   **не** сжигает бонус.
2. Не-daily покупка короче `REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS` по-прежнему
   ставит маркер без дней (поведение не меняется).
3. Не-daily покупка от минимального срока — маркер + дни.
4. Поздняя привязка: если до неё были только daily-платежи, пользователь не
   считается «уже платившим», маркер не пишется. Если есть хотя бы один
   не-daily `SUBSCRIPTION_PAYMENT` — маркер пишется.
5. Дни настраиваются раздельно: `REFERRAL_FIRST_PURCHASE_REFEREE_DAYS` и
   `REFERRAL_FIRST_PURCHASE_REFERRER_DAYS`; общий
   `REFERRAL_FIRST_PURCHASE_BONUS_DAYS` остаётся fallback.

### Файлы

- `app/config.py`
- `app/database/crud/transaction.py`
- `app/services/referral_purchase_service.py`
- `app/services/referral_service.py`
- `app/utils/referral_ui.py`
- `.env.example`
- `../claude-changes.md` (вне репозитория)
- тесты, перечисленные ниже

### Требования

#### 1. Конфиг

В `app/config.py` рядом с существующими `REFERRAL_FIRST_PURCHASE_*`:

- `REFERRAL_FIRST_PURCHASE_BONUS_DAYS: int = 10` — оставить как fallback.
- `REFERRAL_FIRST_PURCHASE_REFEREE_DAYS: int | None = None`.
- `REFERRAL_FIRST_PURCHASE_REFERRER_DAYS: int | None = None`.
- `REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS: int = 30` — оставить.

Добавить методы эффективных значений (рядом с другими referral-геттерами):

- `get_referral_first_purchase_referee_days() -> int`: если новый ключ `None`,
  вернуть `REFERRAL_FIRST_PURCHASE_BONUS_DAYS`; отрицательные значения
  нормализовать в `0`.
- `get_referral_first_purchase_referrer_days() -> int`: аналогично.

В словаре referral-настроек (там, где сейчас `first_purchase_bonus_days` и
`first_purchase_min_period_days`, около строки 4064) добавить эффективные
`first_purchase_referee_days` и `first_purchase_referrer_days`; старый
`first_purchase_bonus_days` сохранить для обратной совместимости.

В `.env.example` рядом с существующими ключами `REFERRAL_FIRST_PURCHASE_*`
добавить/обновить закомментированные строки:

```
# REFERRAL_FIRST_PURCHASE_REFEREE_DAYS=10
# REFERRAL_FIRST_PURCHASE_REFERRER_DAYS=10
# REFERRAL_FIRST_PURCHASE_BONUS_DAYS=10   # fallback, если два ключа выше не заданы
# REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS=30
```

Не дублировать существующие строки.

#### 2. Daily-платежи в предикате «уже платил»

В `app/database/crud/transaction.py` рядом с `ADDON_DESCRIPTION_PATTERNS`:

- `DAILY_CHARGE_PATTERNS = ('%суточного тарифа%', '%суточная оплата тарифа%')`.
- `daily_charge_clause(description_column)` — SQL-условие `OR ILIKE` по этим
  паттернам, по образцу `addon_description_clause`.
- Комментарий: daily-списания переиспользуют `SUBSCRIPTION_PAYMENT`, описания —
  захардкоженные русские литералы; это известное текстовое ограничение, при
  появлении нового daily-пути паттерн нужно добавить. Надёжнее был бы отдельный
  тип/флаг транзакции, но в этой задаче — паттерны.

В `app/services/referral_purchase_service.py::_real_subscription_payment_exists`
исключить daily-описания так же, как допы: транзакция считается оплатой подписки,
только если `description IS NULL` или описание не совпадает ни с допом, ни с
daily-паттерном.

Обновить докстринги `has_paid_subscription_condition` и
`has_paid_subscription_before_attach`: daily-списание не является покупкой для
маркера первой покупки.

Это автоматически влияет на `ensure_marker_for_already_paid_referral`,
`backfill_first_purchase_markers` и dry-run бэкфилла — отдельных правок там не
нужно.

#### 3. `register_first_purchase`

В `app/services/referral_purchase_service.py`:

- Добавить `'daily_not_counted'` в `FirstPurchaseStatus`.
- После проверки `PENDING`/`DISABLED` и загрузки подписки:
  - если `is_daily_referral_tariff(referee_sub)` — вернуть
    `FirstPurchaseResult(status='daily_not_counted')`, **не** добавляя маркер;
  - если `not is_referral_reward_target(referee_sub)` — вернуть `skipped`
    (EXPIRED-пустышка и прочие неподходящие цели).
- Удалить старую ветку `daily_first_purchase`, которая писала маркер без дней.
- `duration_days < REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS` — оставить как есть:
  маркер, без дней, статус `registered`.
- Заменить общий `bonus` на:
  - `referee_bonus = settings.get_referral_first_purchase_referee_days()`
  - `referrer_bonus = settings.get_referral_first_purchase_referrer_days()`
- Если оба бонуса `<= 0`: маркер + commit, статус `registered`, дни 0.
- Если `referee_bonus > 0`: начислить `add_reward_days(referee_sub,
  referee_bonus)`; при `None` — rollback и `skipped`, как сейчас. Добавить
  `REASON_FIRST_PURCHASE_REFEREE` с `days_granted=referee_bonus`.
- Если `referee_bonus <= 0`: не вызывать `add_reward_days` для приглашённого,
  перейти к пригласившему.
- Если `referrer_bonus > 0`: `_lock_referrer_target`, `add_reward_days` на
  `referrer_bonus`, `REASON_FIRST_PURCHASE` с `days_granted=referrer_bonus`.
- `FirstPurchaseResult.referee_days` / `referrer_days` — только реально
  выданные дни.
- Маркерная строка (`REASON_FIRST_PURCHASE_REGISTERED`, `days_granted=0`)
  остаётся в той же транзакции, что и награды.
- Статус `rewarded`, если выданы хотя бы одни дни; иначе `registered`.

#### 4. `_post_commit_effects`

В `app/services/referral_purchase_service.py::_post_commit_effects`:

- Вместо множества `subscription_ids` и общего
  `settings.REFERRAL_FIRST_PURCHASE_BONUS_DAYS` для Lava использовать отображение
  `subscription_id -> фактические дни`:
  - `result.referee_subscription_id` → `result.referee_days`;
  - `result.referrer_subscription_id` → `result.referrer_days`.
- Для каждой подписки синхронизировать Remnawave и сдвигать Lava на её
  фактическое количество дней. Подписки с `None` id не включать.

#### 5. Обещания в UI и уведомлениях

- `app/services/referral_service.py` (около строки 830): обещание пригласившему
  считать по `settings.get_referral_first_purchase_referrer_days()`; если 0 —
  не обещать. Текст «вы получите +N дней» — по этой же величине.
- `app/utils/referral_ui.py` (строки ~140-151):
  - `referee_days = settings.get_referral_first_purchase_referee_days()`;
  - `referrer_days = settings.get_referral_first_purchase_referrer_days()`;
  - если оба 0 — строку о первой покупке не показывать;
  - если значения равны — сохранить текущую формулировку «+N дней вам и другу»;
  - если разные — показать обе стороны раздельно, например
    `+{referrer} ... вам и +{referee} ... другу за первую покупку от M ...`;
    использовать существующий `_days_word`.
- Других прямых использований `REFERRAL_FIRST_PURCHASE_BONUS_DAYS` в прикладной
  логике быть не должно (проверить `rg`).

#### 6. Тесты

Обновить/добавить:

- `tests/services/test_referral_first_purchase_service.py`:
  - daily-покупка → статус `daily_not_counted`, маркера нет, дней нет,
    `end_date` не изменился;
  - daily-покупка → затем не-daily 30 дней → `rewarded`, маркер есть, дни есть;
  - раздельные дни: `REFEREE_DAYS=7`, `REFERRER_DAYS=3` → у сторон 7 и 3,
    ledger-строки с 7 и 3, `FirstPurchaseResult` с 7 и 3;
  - fallback: оба новых ключа `None`, общий 10 → обе стороны 10;
  - `REFEREE_DAYS=0`, `REFERRER_DAYS=5` → приглашённый 0, пригласивший 5,
    маркер есть;
  - существующий `test_daily_tariff_is_not_a_referrer_target` не ломать.
- `tests/services/test_referral_first_purchase_marker.py`:
  - `_payment` должен принимать `description`;
  - daily-описания (`Суточная оплата тарифа «X»`, `Покупка суточного тарифа X
    (первый день)`) не делают пользователя «paid»;
  - обычное описание и `description=None` — делают;
  - `ensure_marker_for_already_paid_referral` с daily-only транзакцией маркер не
    пишет; с обычной — пишет.
- `tests/services/test_referral_admin_attach_marker.py`:
  - daily-only транзакция → админские пути и `process_referral_registration`
    маркер не пишут;
  - обычная транзакция → пишут (существующие тесты).
- `tests/services/test_referral_retroactive_promises.py`:
  - daily-only оплата до привязки → `already_paid=False`, пригласившему
    обещается бонус, маркера нет. Тест должен патчить `AsyncSessionLocal` на
    тестовую фабрику (см. `_use_test_session_factory` в
    `test_referral_admin_attach_marker.py`).
- `tests/services/test_referral_days_only_contract.py`:
  - дефолты новых ключей `None`, эффективные значения 10; min period 30.
- `tests/handlers/test_referral_days_only_screen.py`:
  - раздельные значения 7/3 отображаются обеими сторонами;
  - равные значения сохраняют прежнюю формулировку.
- `tests/tools/test_referral_days_only_backfill.py`:
  - daily-only кандидат не попадает в dry-run и не получает маркер;
  - обычная оплата по-прежнему попадает.
- Проверить и обновить `tests/services/test_referral_days_only_legacy_disabled.py`,
  `tests/database/test_referral_first_purchase_postgres.py` и другие тесты,
  которые ожидают daily-маркер или monkeypatch общего ключа.
- Запустить:
  `.venv/Scripts/python.exe -m pytest -q tests/services/test_referral_first_purchase_service.py tests/services/test_referral_first_purchase_marker.py tests/services/test_referral_admin_attach_marker.py tests/services/test_referral_retroactive_promises.py tests/services/test_referral_days_only_contract.py tests/handlers/test_referral_days_only_screen.py tests/tools/test_referral_days_only_backfill.py tests/services/test_referral_days_only_legacy_disabled.py tests/services/test_referral_purchase_hook_coverage.py`

#### 7. Критерии приёмки

- Daily-покупка не создаёт `referral_first_purchase_registered` и не выдаёт дни.
- Повторная daily-покупка и последующая не-daily 30 дней дают бонус один раз.
- Daily-only оплата до поздней привязки не считается «уже платил».
- Раздельные дни применяются к обеим сторонам и корректно отражаются в
  `FirstPurchaseResult`, ledger, Lava-сдвиге и уведомлениях.
- Fallback на общий ключ работает.
- Тесты зелёные; вывод без лишних warning.
- В `../claude-changes.md` добавлена запись.
- Коммитов нет.

---

## Task 2 — P1: идемпотентный маркер первой покупки

### Цель

Убрать редкие сценарии, когда маркер первой покупки теряется или дублируется:
повторная регистрация после сбоя, повторная админская привязка, параллельные
записи, тесты, уходящие в реальную БД.

### Файлы

- `app/database/models.py`
- `app/services/referral_purchase_service.py`
- `app/services/referral_service.py`
- `app/services/user_service.py`
- `app/services/referral_diagnostics_service.py`
- `migrations/alembic/versions/0136_referral_first_purchase_marker_unique.py`
  (новый)
- `.env.example` — не требуется
- `../claude-changes.md` (вне репозитория)
- тесты, перечисленные ниже

### Требования

#### 1. Уникальный индекс маркера

- В `ReferralEarning.__table_args__` (`app/database/models.py`) добавить рядом с
  существующим индексом якоря:
  - имя `uq_referral_earnings_first_purchase_registered`;
  - колонка `user_id`;
  - `unique=True`;
  - `postgresql_where=text("reason = 'referral_first_purchase_registered'")`;
  - `sqlite_where=text("reason = 'referral_first_purchase_registered'")`.
- Новая миграция `migrations/alembic/versions/0136_referral_first_purchase_marker_unique.py`:
  - `revision = '0136'`, `down_revision = '0135'`;
  - upgrade:
    1. удалить дубли маркера, оставив `MIN(id)` на каждый `user_id`:
       `DELETE FROM referral_earnings WHERE reason = 'referral_first_purchase_registered' AND id NOT IN (SELECT MIN(id) FROM referral_earnings WHERE reason = 'referral_first_purchase_registered' GROUP BY user_id);`
    2. `CREATE UNIQUE INDEX IF NOT EXISTS uq_referral_earnings_first_purchase_registered ON referral_earnings (user_id) WHERE reason = 'referral_first_purchase_registered';`
  - downgrade: `DROP INDEX IF EXISTS ...`;
  - комментарий в стиле 0135: зачем индекс, почему партиальный, что чистка
    идемпотентна.
- Проверить, что `Base.metadata.create_all` на свежей SQLite создаёт индекс, а
  миграция с `IF NOT EXISTS` не падает.

#### 2. Безопасная запись маркера

В `app/services/referral_purchase_service.py::ensure_marker_for_already_paid_referral`:

- Читать пользователя под блокировкой:
  `select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)`.
- Оставить проверку `has_first_purchase_marker` и `has_paid_subscription_before_attach`.
- Вставку + `commit` обернуть в `try/except IntegrityError` (из
  `sqlalchemy.exc`): при конфликте уникального индекса сделать `rollback` и
  вернуть `False` (маркер уже есть).
- Комментарий: уникальный индекс — структурная защита от гонки; блокировка
  сериализует проверку и вставку.
- `ensure_marker_for_already_paid_referral_in_new_session` не менять, кроме
  докстринга при необходимости.

#### 3. Повторная регистрация после сбоя

В `app/services/referral_service.py::process_referral_registration`:

- При существующем якоре (`existing_anchor is not None`) перед `return True`
  вызвать `ensure_marker_for_already_paid_referral_in_new_session(new_user_id)`
  best-effort, с логированием ошибки. Это ретрай маркера при повторной
  обработке (DELETED-restore), если первая запись упала.
- Не дублировать уведомления/contest-событие: существующий ранний return
  сохраняется.
- Вынести вызов в маленький локальный helper или аккуратно продублировать
  try/except, чтобы не разъезжались сообщения логов.
- Добавить тест: первый вызов `process_referral_registration` с падающим
  маркером (patch helper → raise), затем второй вызов с рабочим helper и
  оплатой; маркер появляется, уведомления не дублируются.

#### 4. Ретраи на админских путях

- `app/services/user_service.py::update_user_referrals`:
  - сейчас маркер пишется только для `to_add`; писать для всех `to_assign`
    (это `unique_ids`, то есть все текущие привязки) — helper идемпотентен.
  - Комментарий: повторный запуск админской привязки должен ретраить
    ранее упавший маркер, поэтому обходим всех привязанных, а не только новых.
- `app/services/referral_diagnostics_service.py::fix_lost_referrals`:
  - сейчас маркер пишется только для `newly_attached_user_ids`; собрать
    `attached_user_ids` для всех обработанных пользователей, у которых после
    привязки `referred_by_id` не `None`, и писать маркер для них.
  - Не трогать пользователей без привязки.
  - Сохранить best-effort логирование.
- Тесты:
  - `update_user_referrals`: первый вызов с падающим helper → второй вызов с
    рабочим helper пишет маркер для уже привязанного пользователя.
  - `fix_lost_referrals`: аналогично; существующий
    `test_fix_lost_referrals_skips_unchanged_attachment` обновить так, чтобы он
    проверял ретрай маркера, а не отсутствие вызова.

#### 5. Изоляция тестов

- `tests/services/test_referral_retroactive_promises.py`: patch
  `app.database.database.AsyncSessionLocal` на тестовую фабрику (как в
  `test_referral_admin_attach_marker.py::_use_test_session_factory`) во всех
  тестах, где `process_referral_registration`/`attach_referrer_if_missing`
  доходят до маркера. Утверждать наличие/отсутствие маркера, а не только текст.
- `tests/services/test_referral_levels_safety.py::test_lost_referrals_apply_restores_binding_without_paying_on_days_only_scheme`:
  не допускать реального `AsyncSessionLocal`: либо patch на тестовую фабрику,
  либо patch `ensure_marker_for_already_paid_referral_in_new_session` на
  `AsyncMock` и утверждать вызов для привязанного пользователя.
- Проверить, что ни один тест в затронутых файлах не ходит в `localhost/test_db`.

#### 6. Тесты P1

- `tests/services/test_referral_admin_attach_marker.py`:
  - повторная запись маркера не создаёт вторую строку (уникальный индекс);
  - `ensure_marker` возвращает `False` при существующем маркере;
  - ретрай после сбоя на `process_referral_registration`;
  - ретрай на `update_user_referrals` и `fix_lost_referrals`.
- `tests/database/test_referral_first_purchase_postgres.py` или новый тест:
  - модель содержит партиальный уникальный индекс;
  - миграция 0136 применяется на временной SQLite/PostgreSQL, повторный
    `CREATE UNIQUE INDEX IF NOT EXISTS` не падает. Если полноценный прогон
    миграций в тестах недоступен, минимум — проверить наличие индекса в
    метаданных и синтаксис миграции.
- Запустить:
  `.venv/Scripts/python.exe -m pytest -q tests/services/test_referral_admin_attach_marker.py tests/services/test_referral_retroactive_promises.py tests/services/test_referral_levels_safety.py tests/services/test_referral_first_purchase_service.py tests/services/test_referral_first_purchase_marker.py tests/database/test_referral_first_purchase_postgres.py`
  и дополнительные тесты, которые затронуты.

#### 7. Критерии приёмки

- Уникальный индекс не даёт создать второй маркер на пользователя.
- Параллельная/повторная запись маркера не падает наружу и не дублирует строки.
- Повторная регистрация после сбоя дописывает маркер.
- Повторная админская привязка дописывает ранее упавший маркер.
- Тесты не ходят в реальную БД.
- В `../claude-changes.md` добавлена запись.
- Коммитов нет.

---

## Финальное ревью

После Task 2 провести whole-branch review по diff от baseline-снимка перед
Task 1 до финального рабочего дерева. Проверить оба P0 и P1 вместе, deferred
minor из ledger, отсутствие регрессий в затронутых путях.
