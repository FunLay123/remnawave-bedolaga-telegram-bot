# План: пополнение — сначала сумма, потом способ оплаты

## Context

Сейчас кнопка «Пополнить» в главном меню (`balance_topup`) сразу открывает список
способов оплаты, а сумму пользователь выбирает только на следующем шаге. Если у
пользователя есть сохранённая корзина с `missing_amount`, сумма подставляется молча
и экран выбора суммы пропускается.

Нужно сделать как на экране «Продлить подписку»: сначала экран выбора суммы
(быстрые суммы + ввод числа в чат), затем список способов оплаты, и уже нажатие
способа создаёт платёж на выбранную сумму.

## Global Constraints

- Глобальный минимум пополнения — **50 ₽ = 5000 копеек** (`DEFAULT_TOPUP_MIN_KOPEKS = 50_00`).
- Быстрые суммы на новом экране: **50, 100, 150, 300, 500 ₽** (без новых кнопок).
- Корзину (`user_cart_service`, `missing_amount`) в новом flow **не читать и не упоминать**.
  Механику авто-покупки после пополнения не трогать.
- Логику создания платежей, вебхуки и провайдеров не менять.
- UI-тексты — по-русски. Другие локали (en/ua/zh/fa) не редактировать.
- Публичные функции, которые используются другими экранами, менять только с
  обратносовместимыми значениями по умолчанию.
- В коммитах **не добавлять** `Co-Authored-By: Claude` и любые AI-трейлеры.
- Файлы писать с LF-переводами строк.
- Перед коммитом прогнать профильные тесты, затем один раз полный `pytest -q`.
- Один коммит на задачу.

## Task 1: Ядро flow «сумма → способ»

### Файлы

- `app/utils/payment_utils.py`
- `app/keyboards/topup_amounts.py`
- `app/keyboards/inline.py`
- `app/services/payment_method_config_service.py`
- `app/handlers/balance/main.py`
- `tests/handlers/test_balance_topup_amount_step.py` (новый)
- `tests/utils/test_payment_screen_texts.py` (при необходимости)

### Требования

1. `app/utils/payment_utils.py`:
   - `DEFAULT_TOPUP_MIN_KOPEKS = 50_00` (было `1_00`).
   - Новая функция `topup_amount_select_text(balance_kopeks, min_amount_kopeks, max_amount_kopeks) -> str`:
     ```
     {PAYMENT_SCREEN_HEADER} <b>Пополнение баланса</b>

     {PAYMENT_BULLET} Баланс: <b>{format_whole_rubles(balance_kopeks)}</b>
     {PAYMENT_SUB_BULLET} Пополнение от {format_min_rubles(min_amount_kopeks)} до {format_max_rubles(max_amount_kopeks)}
     {PAYMENT_SUB_BULLET} Выберите сумму или отправьте число в чат:
     ```
   - `payment_methods_text(balance_kopeks, amount_kopeks=None)`: если `amount_kopeks`
     передан и больше нуля, добавить строку
     `{PAYMENT_SUB_BULLET} К пополнению: <b>{format_whole_rubles(amount_kopeks)}</b>`
     перед строкой «Выберите способ оплаты:».
   - `get_payment_methods_text(language, balance_kopeks=0, *, has_direct_methods=True, amount_kopeks=None)`
     — прокинуть `amount_kopeks` в `payment_methods_text`.

2. `app/keyboards/topup_amounts.py`:
   - Новая функция `get_topup_sum_keyboard(language, amounts, back_callback='back_to_menu') -> InlineKeyboardMarkup`.
   - Кнопки: `format_quick_amount(amount)` (уже есть в модуле), callback
     `topup_sum|<amount_kopeks>`, по две в ряд, стиль через
     `styled_button(f'amount:{amount}', ..., fallback_action='amount:*')`.
   - Последняя строка — «Назад» через `styled_button('nav:back', texts.BACK, callback_data=back_callback)`.

3. `app/keyboards/inline.py`:
   - У `get_payment_methods_keyboard` добавить keyword-only параметр
     `back_callback: str = 'back_to_menu'` и использовать его в последней строке
     с кнопкой «Назад» (сейчас там жёстко `back_to_menu`).
   - Остальные вызовы не менять — значение по умолчанию сохраняет поведение.

4. `app/services/payment_method_config_service.py`:
   - Новая async-функция
     `get_methods_amount_limits(db, config_ids: Iterable[str]) -> dict[str, tuple[int, int]]`.
   - Один запрос `select(PaymentMethodConfig)`; для каждого `config_id` взять
     `config.min_amount_kopeks`/`config.max_amount_kopeks`, а если `None` —
     дефолты из `_get_method_defaults()`. Методы без конфига и без дефолтов
     пропустить. Вернуть эффективные `(min, max)` в копейках.
   - Импортировать `DEFAULT_TOPUP_MIN_KOPEKS`/`DEFAULT_TOPUP_MAX_KOPEKS` из
     `app.utils.payment_utils` для fallback (циклического импорта нет).

5. `app/handlers/balance/main.py`:
   - Импортировать новые функции: `get_topup_sum_keyboard`,
     `topup_amount_select_text`, `get_methods_amount_limits`,
     `DEFAULT_QUICK_AMOUNTS`, `resolve_config_method_id`.
   - Новый обработчик `show_topup_amount_select(callback, db_user, db, state)`
     вместо `show_payment_methods` на callback `balance_topup`:
     - проверка `restriction_topup` — как в текущем `show_payment_methods`;
     - собрать id способов из `get_payment_methods_keyboard(0, language)`
       (callback `topup_<method>`, `topup_support` пропустить);
     - получить лимиты через helper `_get_method_limits_map(db, method_ids)`
       (внутри: `resolve_config_method_id` + `get_methods_amount_limits`);
     - `min_bound = max(DEFAULT_TOPUP_MIN_KOPEKS, min(lo))`, если лимиты есть;
       `max_bound = min(DEFAULT_TOPUP_MAX_KOPEKS, max(hi))`; если лимитов нет —
       глобальные `DEFAULT_TOPUP_MIN_KOPEKS`/`DEFAULT_TOPUP_MAX_KOPEKS`;
       если `min_bound > max_bound` — тоже глобальные;
     - быстрые суммы: `[a for a in DEFAULT_QUICK_AMOUNTS if min_bound <= a <= max_bound]`;
     - отредактировать сообщение в `topup_amount_select_text(...)` с клавиатурой
       `get_topup_sum_keyboard(language, amounts, back_callback='back_to_menu')`;
     - `await state.set_state(BalanceStates.waiting_for_amount)`;
       `await state.update_data(awaiting_topup_amount=True, payment_method=None)`;
     - `await callback.answer()`.
   - Новый обработчик `handle_topup_sum_choice(callback, db_user, db, state)` на
     `topup_sum|`:
     - распарсить сумму, при ошибке/`<= 0` — alert;
     - если сумма `< DEFAULT_TOPUP_MIN_KOPEKS` или `> DEFAULT_TOPUP_MAX_KOPEKS` — alert
       с диапазоном;
     - вызвать `_render_payment_methods(callback.message, db_user, db, amount, edit=True)`;
     - если способов не осталось — `callback.answer('Для этой суммы нет доступных способов оплаты', show_alert=True)`
       и не очищать state;
     - иначе `await state.clear()` и `await callback.answer()`.
   - Текущий `show_payment_methods` переделать в async-helper
     `_render_payment_methods(target, db_user, db, amount_kopeks, *, edit: bool) -> bool`:
     - **убрать** чтение `user_cart_service.get_user_cart`;
     - клавиатура `get_payment_methods_keyboard(amount_kopeks, language, back_callback='balance_topup')`;
     - отфильтровать строки способов по лимитам для `amount_kopeks`: способ
       остаётся, если `lo <= amount <= hi`; строки поддержки/«Назад» сохраняются;
       неизвестные методы (нет лимитов) не фильтровать;
     - убрать пустые строки;
     - если не осталось ни одной кнопки способа (все callback не `topup_*` или
       только `topup_support`) — вернуть `False`, ничего не отправляя;
     - текст `get_payment_methods_text(language, balance, has_direct_methods=..., amount_kopeks=amount_kopeks)`;
     - `edit=True` → `target.edit_text(...)`, иначе `target.answer(...)`;
     - вернуть `True`.
   - `process_topup_amount`: **сигнатуру не менять** (тесты вызывают с тремя аргументами).
     После разбора и глобальной проверки min/max:
     - `data = await state.get_data()`;
     - если `data.get('awaiting_topup_amount')` и нет `data.get('payment_method')`:
       открыть сессию через `AsyncSessionLocal`, вызвать `_render_payment_methods(message, db_user, db, amount_kopeks, edit=False)`;
       при успехе `await state.clear()`, при неудаче отправить
       `payment_amount_error_text('Для этой суммы нет доступных способов оплаты')` с
       `get_back_keyboard(..., 'balance_topup')` и оставить state;
       `return`;
     - иначе — прежнее поведение.
   - Регистрация:
     - `dp.callback_query.register(show_topup_amount_select, F.data == 'balance_topup')`
       (заменяет `show_payment_methods`);
     - `dp.callback_query.register(handle_topup_sum_choice, F.data.startswith('topup_sum|'))`;
     - `handle_topup_amount_callback` на `topup_amount|` оставить.

### Тесты Task 1

Новый `tests/handlers/test_balance_topup_amount_step.py` (AsyncMock/SimpleNamespace,
без реальной БД; monkeypatch лимитов и настроек):

1. `balance_topup` рендерит экран суммы: текст содержит «Пополнение от 50 ₽ до …»,
   в клавиатуре есть `topup_sum|5000`, `topup_sum|10000`, `topup_sum|15000`,
   `topup_sum|30000`, `topup_sum|50000`; state = `waiting_for_amount`,
   `awaiting_topup_amount=True`.
2. `topup_sum|5000` рендерит список способов с callback `topup_amount|yookassa|5000`
   (или другим включённым способом), текст содержит «К пополнению: 50 ₽»;
   state очищен.
3. Ввод `"30"` до выбора способа → сообщение «Минимальная сумма — 50 ₽»,
   список способов не отправляется.
4. Ввод `"50"` до выбора способа → список способов с `topup_amount|...|5000`.
5. Фильтрация: при лимитах yookassa `(5000, 1000000)` и cryptobot `(10000, 10000000)`
   для суммы 5000 в клавиатуре остаётся yookassa и нет cryptobot.
6. Если после фильтрации способов нет — alert/сообщение, state не очищается.

Дополнить `tests/utils/test_payment_screen_texts.py` тестом на
`topup_amount_select_text` и строку «К пополнению» в `payment_methods_text`.

### Acceptance Task 1

- `pytest tests/handlers/test_balance_topup_amount_step.py tests/utils/test_payment_screen_texts.py tests/handlers/test_balance_topup_retry_hint.py -q` — зелёный.
- Полный `pytest -q` — без новых падений.

## Task 2: Тарифные кнопки «Пополнить на N ₽» передают сумму явно

### Файлы

- `app/handlers/subscription/tariff_purchase.py`
- `tests/test_tariff_insufficient_balance_keyboard.py`

### Требования

1. `get_tariff_insufficient_balance_keyboard` и
   `get_tariff_extend_insufficient_balance_keyboard`:
   - кнопка «Пополнить на N ₽» вместо `callback_data='balance_topup'` использует
     `callback_data=f'topup_sum|{missing_kopeks}'` при `missing_kopeks > 0`;
     при `missing_kopeks <= 0` оставить `balance_topup`;
   - текст кнопки, стиль и иконку не менять;
   - обновить docstring: сумма передаётся явно в callback, корзина не используется.
2. Тесты `tests/test_tariff_insufficient_balance_keyboard.py`:
   - заменить ожидания `'balance_topup'` на `f'topup_sum|{missing}'` там, где
     кнопка содержит сумму;
   - проверки отсутствия `topup_amount|` оставить;
   - тест, который ищет кнопку по `callback_data == 'balance_topup'`, перевести на
     поиск по тексту «Пополнить на …» и проверку нового callback.

### Acceptance Task 2

- `pytest tests/test_tariff_insufficient_balance_keyboard.py -q` — зелёный.
- Полный `pytest -q` — без новых падений.
