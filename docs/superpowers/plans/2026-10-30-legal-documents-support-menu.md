# Юридические документы и меню поддержки — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить в бота оферту, политику возвратов, политику конфиденциальности и правила сервиса; принимать оферту и правила при входе; открывать все документы из нового меню «Техподдержка»; убрать тикетные кнопки из меню и вести кнопку поддержки на @FunLay123.

**Architecture:** Документы — HTML-файлы в `app/legal_documents/` с загрузчиком без БД и админки. Новый handler `app/handlers/legal_documents.py` рисует хаб и просмотр документов. Вход в `start.py` заменяет старый шаг «правила → политика» на один экран с офертой/правилами и пишет согласие в существующую `legal_consents`. Меню поддержки переделывается в `support.py` + `get_support_keyboard`. Старые тикеты, БД-документы и раздел «Инфо» остаются как легаси.

**Tech Stack:** aiogram 3, SQLAlchemy async, существующие `html_to_telegram`/`stored_html_to_telegram_pages`, pytest.

**Spec:** `docs/superpowers/specs/2026-10-30-legal-documents-support-menu-design.md`

## Global Constraints

- Документы и новые экраны — только по-русски; новые ключи в `ru.json` **не добавляем**, тексты живут в коде/HTML, чтобы не ломать паритет локалей.
- Никаких миграций, новых таблиц и админских экранов.
- Старые `PrivacyPolicy`, `PublicOffer`, `ServiceRule`, тикеты и раздел «Инфо» не удаляем и не переписываем.
- Новые тесты не добавляем без отдельной просьбы; проверяем существующими тестами и ручным сценарием. Если существующий тест падает из-за нового поведения — правим тест по факту.
- LF, без AI-трейлеров в коммитах, changelog в `../claude-changes.md` после изменений.

## Review Focus

- Пользователь без аккаунта должен суметь открыть документы до принятия и вернуться на экран входа.
- После «Принимаю» регистрация продолжается ровно как раньше: реферальный код, промокод, завершение.
- `SKIP_RULES_ACCEPT=true` полностью пропускает юридический шаг и не пишет согласие.
- Старые сообщения с `rules_accept`, `privacy_policy_accept`, `create_ticket`, `my_tickets` не падают.
- Согласие пишется только после создания/восстановления пользователя и не роняет регистрацию.

---

### Task 1: Пакет документов и тексты

**Files:**
- Create: `app/legal_documents/__init__.py`
- Create: `app/legal_documents/offer.html`
- Create: `app/legal_documents/refund.html`
- Create: `app/legal_documents/privacy.html`
- Create: `app/legal_documents/rules.html`

**Interfaces:**
- `LegalDocument(key, title, filename, required_at_entry)`
- `DOCUMENTS: tuple[LegalDocument, ...]`, `DOCUMENTS_BY_KEY`
- `EDITION = '30.10.2026'`
- `load_document_text(key: str) -> str`
- `document_pages(key: str, max_length: int = 3500) -> list[str]`
- `entry_gate_text() -> str`

- [ ] **Step 1: Создать пакет с загрузчиком**

```python
"""Юридические документы бота: HTML-файлы без БД и админки."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.utils.telegram_html import stored_html_to_telegram_pages


EDITION = '30.10.2026'
LEGAL_EMOJI_ID = '5359415772844152891'


@dataclass(frozen=True)
class LegalDocument:
    key: str
    title: str
    filename: str
    required_at_entry: bool = False


DOCUMENTS: tuple[LegalDocument, ...] = (
    LegalDocument('offer', 'Публичная оферта', 'offer.html', True),
    LegalDocument('refund', 'Политика возвратов', 'refund.html'),
    LegalDocument('privacy', 'Политика конфиденциальности', 'privacy.html'),
    LegalDocument('rules', 'Правила сервиса', 'rules.html', True),
)
DOCUMENTS_BY_KEY = {document.key: document for document in DOCUMENTS}


def load_document_text(key: str) -> str:
    document = DOCUMENTS_BY_KEY.get(key)
    if document is None:
        return ''
    return (Path(__file__).parent / document.filename).read_text(encoding='utf-8')


def document_pages(key: str, max_length: int = 3500) -> list[str]:
    return stored_html_to_telegram_pages(load_document_text(key), max_length=max_length) or ['']


def entry_gate_text() -> str:
    return (
        f'<tg-emoji emoji-id="{LEGAL_EMOJI_ID}">📄</tg-emoji> <b>Перед началом</b>\n\n'
        'Чтобы пользоваться сервисом, примите:\n'
        '• Публичную оферту\n'
        '• Правила сервиса'
    )
```

- [ ] **Step 2: Создать `offer.html`**

```html
<b>1. Исполнитель</b>
<br>[ИП Фамилия И.О.], ИНН [ИНН], ОГРНИП [ОГРНИП], адрес: [адрес], email: [email], далее — Исполнитель.

<br><br><b>2. Предмет</b>
<br>Исполнитель предоставляет Пользователю доступ к VPN-сервису на условиях выбранного тарифа: выдаёт ключ/ссылку подключения, поддерживает работу сервиса и оказывает поддержку.

<br><br><b>3. Акцепт</b>
<br>Оплата подписки или активация пробного периода означает полное принятие настоящей оферты. Договор действует с момента оплаты/активации до окончания оплаченного периода.

<br><br><b>4. Стоимость и оплата</b>
<br>Стоимость указана в боте и на странице оплаты. Оплата производится через подключённые платёжные сервисы. Исполнитель не хранит данные банковских карт.

<br><br><b>5. Срок и ограничения</b>
<br>Срок подписки, лимит трафика и количество устройств определяются выбранным тарифом. Пользователь обязуется не передавать доступ третьим лицам и не использовать сервис для противоправных действий.

<br><br><b>6. Права и обязанности</b>
<br>Исполнитель обязуется поддерживать работоспособность сервиса и своевременно сообщать о работах. Пользователь обязуется соблюдать правила сервиса и оплачивать выбранный тариф.

<br><br><b>7. Ответственность</b>
<br>Сервис предоставляется «как есть» в пределах, допускаемых законом. Исполнитель не отвечает за блокировки со стороны провайдеров и оборудования Пользователя. Ответственность Исполнителя ограничена суммой последнего платежа.

<br><br><b>8. Возвраты</b>
<br>Возврат средств регулируется Политикой возвратов, доступной в разделе «Документы».

<br><br><b>9. Форс-мажор</b>
<br>Стороны освобождаются от ответственности при обстоятельствах непреодолимой силы.

<br><br><b>10. Изменения</b>
<br>Исполнитель вправе изменять оферту; новая редакция публикуется в боте и действует с даты публикации.

<br><br><b>11. Контакты</b>
<br>Поддержка: @FunLay123
```

- [ ] **Step 3: Создать `refund.html`**

```html
<b>1. Когда возврат возможен</b>
<br>До активации ключа/ссылки подключения Пользователь может запросить возврат в течение 7 календарных дней с момента оплаты.

<br><br><b>2. Когда возврат не производится</b>
<br>После активации ключа/ссылки возврат не производится, кроме подтверждённой технической неисправности по вине Исполнителя.

<br><br><b>3. Как запросить возврат</b>
<br>Напишите в поддержку: @FunLay123. Укажите Telegram ID, дату и сумму платежа, причину возврата.

<br><br><b>4. Срок рассмотрения</b>
<br>Заявка рассматривается до 10 рабочих дней. Возврат выполняется тем же способом, которым была произведена оплата, если платёжный сервис это позволяет.

<br><br><b>5. Злоупотребления</b>
<br>Исполнитель вправе отказать в возврате при признаках злоупотребления, повторных возвратах или нарушении правил сервиса.

<br><br><b>6. Контакты</b>
<br>Поддержка: @FunLay123
```

- [ ] **Step 4: Создать `privacy.html`**

```html
<b>1. Какие данные собираются</b>
<br>Telegram ID, username, имя, язык, данные подписки, сведения о подключённых устройствах, идентификаторы и суммы платежей, сообщения в поддержку, технические логи и IP-адрес.

<br><br><b>2. Зачем</b>
<br>Для предоставления доступа, оплаты, поддержки, защиты сервиса и выполнения требований закона.

<br><br><b>3. Кому передаются</b>
<br>Платёжным провайдерам для проведения оплаты, панели Remnawave и хостингу для работы сервиса. Данные не продаются третьим лицам.

<br><br><b>4. Хранение</b>
<br>Пока действует аккаунт и в течение срока, необходимого по закону. Технические логи хранятся ограниченное время.

<br><br><b>5. Права</b>
<br>Пользователь может запросить доступ, исправление или удаление своих данных через @FunLay123.

<br><br><b>6. Контакты</b>
<br>Поддержка: @FunLay123
```

- [ ] **Step 5: Создать `rules.html`**

```html
<b>1. Допустимое использование</b>
<br>Сервис предназначен для личного законного использования VPN. Пользователь самостоятельно отвечает за соблюдение законов своей страны.

<br><br><b>2. Запрещается</b>
<br>Перепродажа доступа, передача ключа третьим лицам, атаки, спам, распространение вредоносного ПО, торрент-трафик и иные злоупотребления.

<br><br><b>3. Ограничения</b>
<br>При нарушении правил Исполнитель вправе ограничить или заблокировать доступ без возврата средств.

<br><br><b>4. Ответственность</b>
<br>Пользователь отвечает за сохранность ключа и действия, совершённые с его аккаунта.

<br><br><b>5. Изменения</b>
<br>Правила могут обновляться; новая редакция публикуется в боте.

<br><br><b>6. Контакты</b>
<br>Поддержка: @FunLay123
```

- [ ] **Step 6: Проверить загрузку**

Run:

```bash
.venv/Scripts/python.exe - <<'PY'
from app.legal_documents import DOCUMENTS, document_pages
for document in DOCUMENTS:
    pages = document_pages(document.key)
    assert pages and pages[0], document.key
    print(document.key, len(pages))
PY
```

Expected: четыре строки с количеством страниц, без исключений.

---

### Task 2: Клавиатуры и меню поддержки

**Files:**
- Modify: `app/keyboards/inline.py` (`get_support_keyboard`, добавить `get_legal_gate_keyboard`, `get_legal_documents_keyboard`, `get_legal_document_keyboard`)
- Modify: `app/handlers/support.py`
- Locale files **не трогаем**; тексты в коде.

**Interfaces:**
- `LEGAL_EMOJI_ID` импортируется из `app.legal_documents`
- `SUPPORT_URL = 'https://t.me/FunLay123'`
- `get_support_keyboard(language) -> InlineKeyboardMarkup`
- `get_legal_gate_keyboard() -> InlineKeyboardMarkup`
- `get_legal_documents_keyboard(language) -> InlineKeyboardMarkup`
- `get_legal_document_keyboard(key, page, total, origin, language) -> InlineKeyboardMarkup`

- [ ] **Step 1: Переписать `get_support_keyboard`**

```python
from app.legal_documents import LEGAL_EMOJI_ID

SUPPORT_URL = 'https://t.me/FunLay123'
SUPPORT_BUTTON_ICON_ID = '5332588449335628124'


def get_support_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='Документы',
                    callback_data='legal_docs',
                    icon_custom_emoji_id=LEGAL_EMOJI_ID,
                )
            ],
            [
                InlineKeyboardButton(
                    text='Написать в поддержку',
                    url=SUPPORT_URL,
                    icon_custom_emoji_id=SUPPORT_BUTTON_ICON_ID,
                    style='primary',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')],
        ]
    )
```

- [ ] **Step 2: Добавить клавиатуры документов**

```python
def get_legal_gate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='Публичная оферта', callback_data='legal_doc:offer:1:gate'),
                InlineKeyboardButton(text='Правила сервиса', callback_data='legal_doc:rules:1:gate'),
            ],
            [
                InlineKeyboardButton(text='Политика возвратов', callback_data='legal_doc:refund:1:gate'),
                InlineKeyboardButton(text='Политика конфиденциальности', callback_data='legal_doc:privacy:1:gate'),
            ],
            [
                InlineKeyboardButton(
                    text='Принимаю',
                    callback_data='legal_accept',
                    icon_custom_emoji_id='5460980668378931880',
                    style='primary',
                )
            ],
        ]
    )


def get_legal_documents_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='Публичная оферта', callback_data='legal_doc:offer:1:hub')],
            [InlineKeyboardButton(text='Политика возвратов', callback_data='legal_doc:refund:1:hub')],
            [InlineKeyboardButton(text='Политика конфиденциальности', callback_data='legal_doc:privacy:1:hub')],
            [InlineKeyboardButton(text='Правила сервиса', callback_data='legal_doc:rules:1:hub')],
            [InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')],
        ]
    )


def get_legal_document_keyboard(
    key: str, page: int, total: int, origin: str, language: str = DEFAULT_LANGUAGE
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    back = 'legal_gate' if origin == 'gate' else 'legal_docs'
    rows: list[list[InlineKeyboardButton]] = []
    if total > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 1:
            nav.append(
                InlineKeyboardButton(
                    text='‹',
                    callback_data=f'legal_doc:{key}:{page - 1}:{origin}',
                )
            )
        nav.append(InlineKeyboardButton(text=f'{page}/{total}', callback_data='noop'))
        if page < total:
            nav.append(
                InlineKeyboardButton(
                    text='›',
                    callback_data=f'legal_doc:{key}:{page + 1}:{origin}',
                )
            )
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=texts.BACK, callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
```

- [ ] **Step 3: Переписать `show_support_info`**

```python
from app.keyboards.inline import get_support_keyboard
from app.utils.photo_message import edit_or_answer_photo


SUPPORT_TEXT = (
    '<tg-emoji emoji-id="5359415772844152891">🛠</tg-emoji> <b>Техподдержка</b>\n\n'
    '<blockquote>Документы и условия сервиса — ниже.</blockquote>'
)


async def show_support_info(callback: types.CallbackQuery, db_user: User):
    await edit_or_answer_photo(
        callback=callback,
        caption=SUPPORT_TEXT,
        keyboard=get_support_keyboard(db_user.language),
        parse_mode='HTML',
        force_text=True,
    )
    await callback.answer()
```

Удалить неиспользуемый `SupportSettingsService`/`get_texts` импорт, если ruff ругнётся.

- [ ] **Step 4: Проверить**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/test_main_menu_redesign.py tests/handlers/test_start_subid_drain.py -q
```

Expected: существующие тесты зелёные.

---

### Task 3: Handler документов

**Files:**
- Create: `app/handlers/legal_documents.py`
- Modify: `app/bot.py` — импорт и `legal_documents.register_handlers(dp)`

**Interfaces:**
- `show_documents(callback, db_user)`
- `show_document(callback, state, key, page, origin)`
- `show_legal_gate(callback, state)`
- `register_handlers(dp)`

- [ ] **Step 1: Создать handler**

```python
from __future__ import annotations

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext

from app.database.models import User
from app.keyboards.inline import (
    get_legal_document_keyboard,
    get_legal_documents_keyboard,
    get_legal_gate_keyboard,
)
from app.legal_documents import DOCUMENTS_BY_KEY, EDITION, LEGAL_EMOJI_ID, document_pages, entry_gate_text
from app.states import RegistrationStates
from app.utils.photo_message import edit_or_answer_photo


logger = structlog.get_logger(__name__)


async def show_documents(callback: types.CallbackQuery, db_user: User):
    text = (
        f'<tg-emoji emoji-id="{LEGAL_EMOJI_ID}">📄</tg-emoji> <b>Документы</b>\n\n'
        '<blockquote>Оферта, возвраты, конфиденциальность и правила сервиса.</blockquote>'
    )
    await edit_or_answer_photo(
        callback=callback,
        caption=text,
        keyboard=get_legal_documents_keyboard(db_user.language),
        parse_mode='HTML',
        force_text=True,
    )
    await callback.answer()


async def show_legal_gate(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(RegistrationStates.waiting_for_legal_accept)
    await edit_or_answer_photo(
        callback=callback,
        caption=entry_gate_text(),
        keyboard=get_legal_gate_keyboard(),
        parse_mode='HTML',
        force_text=True,
    )
    await callback.answer()


async def show_document(callback: types.CallbackQuery, state: FSMContext):
    parts = (callback.data or '').split(':')
    key = parts[1] if len(parts) > 1 else ''
    document = DOCUMENTS_BY_KEY.get(key)
    if document is None:
        await callback.answer('Документ не найден', show_alert=True)
        return
    try:
        page = max(1, int(parts[2]))
    except (IndexError, ValueError):
        page = 1
    origin = parts[3] if len(parts) > 3 and parts[3] in {'gate', 'hub'} else 'hub'
    pages = document_pages(key)
    page = min(page, len(pages))
    text = f'<b>{document.title}</b>\nРедакция от {EDITION}\n\n{pages[page - 1]}'
    await edit_or_answer_photo(
        callback=callback,
        caption=text,
        keyboard=get_legal_document_keyboard(key, page, len(pages), origin),
        parse_mode='HTML',
        force_text=True,
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_documents, F.data == 'legal_docs')
    dp.callback_query.register(show_legal_gate, F.data == 'legal_gate')
    dp.callback_query.register(show_document, F.data.startswith('legal_doc:'))
```

- [ ] **Step 2: Зарегистрировать в `app/bot.py`**

В импорт из `app.handlers` добавить `legal_documents`; после `start.register_handlers(dp)` (или рядом) вызвать:

```python
legal_documents.register_handlers(dp)
```

`legal_accept` регистрируется в `start.py` (Task 4), поэтому порядок не конфликтует.

- [ ] **Step 3: Проверить**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/test_main_menu_redesign.py -q
```

Expected: зелёные; при старте бота handler импортируется без ошибок.

---

### Task 4: Вход и принятие

**Files:**
- Modify: `app/states.py`
- Modify: `app/handlers/start.py`
- Modify: `app/utils/check_reg_process.py`
- Modify: `app/middlewares/auth.py`

**Interfaces:**
- `RegistrationStates.waiting_for_legal_accept`
- `_send_legal_gate(bot, chat_id, state, *, message=None)`
- `process_legal_accept(callback, state, db)`
- `_record_registration_consent(db, user, data)`

- [ ] **Step 1: Добавить состояние**

```python
class RegistrationStates(StatesGroup):
    waiting_for_language = State()
    waiting_for_rules_accept = State()
    waiting_for_privacy_policy_accept = State()
    waiting_for_legal_accept = State()
    waiting_for_referral_code = State()
```

- [ ] **Step 2: Добавить отправку экрана входа**

В `start.py`:

```python
async def _send_legal_gate(
    bot,
    chat_id: int,
    state: FSMContext,
    *,
    message: types.Message | None = None,
) -> None:
    from app.keyboards.inline import get_legal_gate_keyboard
    from app.legal_documents import entry_gate_text

    text = entry_gate_text()
    if message is not None:
        await message.answer(text, reply_markup=get_legal_gate_keyboard(), parse_mode='HTML')
    else:
        await bot.send_message(chat_id, text, reply_markup=get_legal_gate_keyboard(), parse_mode='HTML')
    await state.set_state(RegistrationStates.waiting_for_legal_accept)
```

- [ ] **Step 3: Заменить четыре отправки правил**

В `_continue_registration_after_language` заменить блок `rules_text = ...`/`answer_long_text`/`state.set_state(waiting_for_rules_accept)` на:

```python
await _send_legal_gate(
    target_message.bot,
    target_message.chat.id,
    state,
    message=target_message,
)
```

В `handle_potential_referral_code` заменить оба блока отправки правил после принятого реферального кода/промокода на:

```python
await _send_legal_gate(message.bot, message.chat.id, state, message=message)
```

В `required_sub_channel_check` (ветка после подписки на канал) заменить отправку правил на:

```python
await _send_legal_gate(bot, query.from_user.id, state)
```

- [ ] **Step 4: Добавить обработчик «Принимаю»**

```python
async def process_legal_accept(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession):
    data = await state.get_data() or {}
    data['legal_accepted'] = True
    await state.set_data(data)
    language = data.get('language', DEFAULT_LANGUAGE)
    try:
        await callback.message.edit_text('✅ Условия приняты', reply_markup=None)
    except Exception as error:
        logger.warning('Не удалось обновить экран принятия условий', error=error)
    await callback.answer()
    await _continue_registration_after_rules(callback, state, db, language)
```

Зарегистрировать:

```python
dp.callback_query.register(
    process_legal_accept,
    F.data == 'legal_accept',
    StateFilter(RegistrationStates.waiting_for_legal_accept),
)
```

Добавить `RegistrationStates.waiting_for_legal_accept` в `StateFilter` у `handle_potential_referral_code`.

- [ ] **Step 5: Разрешить состояние/callback’и неавторизованному**

В `app/utils/check_reg_process.py`:

- добавить `waiting_for_legal_accept` в `registration_states`;
- добавить `legal_accept`, `legal_gate` в `registration_callbacks`;
- добавить `event.data.startswith('legal_doc:')`.

В `app/middlewares/auth.py` в список `registration_states` и в список callback’ов для DELETED-пользователя добавить то же.

- [ ] **Step 6: Проверить**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/contracts/test_public_registration_gate.py tests/handlers/test_start_subid_drain.py -q
```

Expected: зелёные. Вручную: `/start` → язык → экран «Перед началом»; открыть оферту и вернуться; «Принимаю» ведёт к реферальному коду/меню.

---

### Task 5: Запись согласия

**Files:**
- Modify: `app/handlers/start.py`

**Interfaces:**
- `_record_registration_consent(db, user, data)`

- [ ] **Step 1: Добавить helper**

```python
async def _record_registration_consent(db: AsyncSession, user, data: dict) -> None:
    if not data.get('legal_accepted'):
        return
    from app.services.legal_consent_service import record_consent

    await record_consent(
        db,
        user,
        ['public_offer', 'service_rules'],
        source='bot_registration',
    )
```

- [ ] **Step 2: Вызвать после появления пользователя**

В `complete_registration_from_callback` и `complete_registration` после блока, где назначен `user` (новый/восстановленный/обновлённый), перед обработкой реферала добавить:

```python
await _record_registration_consent(db, user, data)
```

`record_consent` сам коммитит и глотает ошибку записи; регистрация не падает.

- [ ] **Step 3: Проверить**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/contracts/test_public_registration_gate.py -q
```

Expected: зелёные. Вручную: после регистрации в `legal_consents` появляются две строки с `source='bot_registration'`.

---

### Task 6: Финальная проверка

- [ ] **Step 1: Прогнать затронутые тесты**

```bash
.venv/Scripts/python.exe -m pytest tests/contracts/test_public_registration_gate.py tests/handlers/test_start_subid_drain.py tests/test_main_menu_redesign.py tests/handlers/test_referral_days_only_list.py tests/handlers/test_referral_days_only_screen.py -q
```

- [ ] **Step 2: Ruff**

```bash
.venv/Scripts/ruff.exe format --check app/legal_documents app/handlers/legal_documents.py app/handlers/support.py app/handlers/start.py app/keyboards/inline.py app/states.py app/utils/check_reg_process.py app/middlewares/auth.py app/bot.py
.venv/Scripts/ruff.exe check app/legal_documents app/handlers/legal_documents.py app/handlers/support.py app/handlers/start.py app/keyboards/inline.py app/states.py app/utils/check_reg_process.py app/middlewares/auth.py app/bot.py
```

- [ ] **Step 3: Structure reference**

```bash
.venv/Scripts/python.exe -m scripts.generate_structure_reference
.venv/Scripts/python.exe -c "from pathlib import Path; p=Path('docs/project_structure_reference.md'); p.write_bytes(p.read_bytes().replace(b'\r\n', b'\n'))"
.venv/Scripts/python.exe -m scripts.generate_structure_reference --check
```

- [ ] **Step 4: Changelog**

Добавить запись в `../claude-changes.md`:

```markdown
## 2026-10-30 — Юридические документы и меню поддержки

**Scope:** `app/legal_documents/*`, `app/handlers/legal_documents.py`, `app/handlers/start.py`, `app/handlers/support.py`, `app/keyboards/inline.py`, `app/states.py`, `app/middlewares/auth.py`, `app/utils/check_reg_process.py`, `app/bot.py`.
**What:** Добавлены оферта, политика возвратов, конфиденциальность и правила в HTML-файлах; вход показывает оферту и правила с кнопкой «Принимаю» и пишет согласие в `legal_consents`; в поддержке появился раздел «Документы», кнопка поддержки ведёт на @FunLay123, тикетные кнопки убраны из меню.
**Why:** Нужен юридический блок для модерации Heleket и понятный вход; документы должны редактироваться в коде без админки.
```
