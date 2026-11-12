import ast
from pathlib import Path

import pytest


EXPECTED_FILES = {
    'app/handlers/subscription/purchase.py',
    'app/handlers/subscription/tariff_purchase.py',
    'app/handlers/simple_subscription.py',
    'app/handlers/menu.py',
    'app/services/subscription_purchase_service.py',
    'app/services/subscription_renewal_service.py',
    'app/services/payment/stars.py',
    'app/services/payment/yookassa.py',
    'app/services/payment/lava.py',
    'app/services/payment/platega.py',
    'app/services/payment/cashera.py',
}


def test_all_purchase_paths_call_the_first_purchase_hook():
    missing = [
        path
        for path in EXPECTED_FILES
        if 'register_first_purchase_after_purchase' not in Path(path).read_text(encoding='utf-8')
    ]
    assert not missing, f'Missing referral hook in: {missing}'


def _function_source(path: str, name: str) -> str:
    source = Path(path).read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None, f'Cannot read source of {name} in {path}'
            return segment
    raise AssertionError(f'{name} not found in {path}')


def test_simple_subscription_extension_calls_the_first_purchase_hook():
    """Хук должен быть именно в теле продления, а не где-то в файле.

    ``handle_simple_subscription_purchase`` ведёт активного/триального
    пользователя в ``_extend_existing_subscription`` — это живая оплата с
    баланса, и маркер первой покупки обязан ставиться и здесь.
    """
    source = _function_source('app/handlers/subscription/purchase.py', '_extend_existing_subscription')

    assert 'register_first_purchase_after_purchase' in source
    assert "source='bot_simple_subscription_extend'" in source
    assert source.index('create_transaction') < source.index('register_first_purchase_after_purchase')


def test_simple_subscription_extension_shows_rewarded_days_in_messages():
    """После награды сообщения показывают срок вместе с бонусными днями.

    ``new_end_date`` считался до хука. Если награда выдана, объект этой сессии
    помнит старый срок, и без перечитывания админ и пользователь увидели бы
    дату без +10 дней, хотя она уже в базе.
    """
    source = _function_source('app/handlers/subscription/purchase.py', '_extend_existing_subscription')

    assert 'first_purchase_result = await register_first_purchase_after_purchase(' in source
    hook_index = source.index('register_first_purchase_after_purchase')
    reward_index = source.index("first_purchase_result.status == 'rewarded'", hook_index)
    refresh_index = source.index('await db.refresh(current_subscription)', reward_index)
    admin_index = source.index('send_subscription_extension_notification', refresh_index)
    message_index = source.index('success_message = (', admin_index)

    assert 'new_end_date = current_subscription.end_date' in source[refresh_index:admin_index]
    assert hook_index < reward_index < refresh_index < admin_index < message_index


MONITORING_FILE = 'app/services/monitoring_service.py'


def test_monitoring_autopay_calls_the_first_purchase_hook():
    """Автоплатёж — такая же оплата и обязан ставить маркер первой покупки.

    Для бесплатно созданной нетриальной подписки (купон/подарок/админка) с
    автоплатежом это первое реальное списание. Хук стоит после
    ``create_transaction`` (транзакция уже закоммичена) и получает бота
    мониторинга. Триальный autopay не затрагивается: запрос в теле функции
    фильтрует ``is_trial == False``.
    """
    function = _function_node(MONITORING_FILE, '_process_autopayments')
    source = _function_source(MONITORING_FILE, '_process_autopayments')

    hooks = _calls(function, (FIRST_PURCHASE_HOOK,))
    assert len(hooks) == 1, f'ожидается один вызов {FIRST_PURCHASE_HOOK}, найдено {len(hooks)}'
    hook = hooks[0]

    assert _keyword_expression(hook, 'referee_id') == 'user.id'
    assert _keyword_expression(hook, 'subscription_id') == 'subscription.id'
    assert _keyword_expression(hook, 'duration_days') == 'autopay_period'
    assert _keyword_expression(hook, 'paid_amount_kopeks') == 'charge_amount'
    assert _keyword_expression(hook, 'source') == repr('monitoring_autopay')
    assert _keyword_expression(hook, 'bot') == 'self.bot'

    except_nodes = _nodes_inside_except_handlers(function)
    commits = [call for call in _calls(function, COMMIT_CALLS) if id(call) not in except_nodes]
    assert commits
    assert hook.lineno > max(call.lineno for call in commits), 'хук обязан стоять после коммита платежа'

    assert 'Subscription.is_trial == False' in source, 'запрос autopay обязан исключать триалы'

    # Награда пишется в другой сессии: уведомления о продлении должны
    # перечитать подписку, иначе покажут срок без бонусных дней.
    hook_index = source.index(FIRST_PURCHASE_HOOK)
    reward_index = source.index("first_purchase_result.status == 'rewarded'", hook_index)
    refresh_index = source.index('await db.refresh(subscription)', reward_index)
    admin_index = source.index('send_subscription_extension_notification', refresh_index)
    email_index = source.index('notify_autopay_success', admin_index)
    assert refresh_index < admin_index < email_index


TARIFF_PURCHASE_FILE = 'app/handlers/subscription/tariff_purchase.py'
FIRST_PURCHASE_HOOK = 'register_first_purchase_after_purchase'

# Последняя точка «деньги списаны и зафиксированы»: create_transaction
# коммитит сессию сама, у суточного переключения перед ней есть ещё явный
# db.commit() смены подписки. Хук обязан стоять после самой поздней из них.
COMMIT_CALLS = ('create_transaction', 'commit')


def _function_node(path: str, name: str) -> ast.AST:
    source = Path(path).read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name} not found in {path}')


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _calls(node: ast.AST, names: tuple[str, ...]) -> list[ast.Call]:
    return [child for child in ast.walk(node) if isinstance(child, ast.Call) and _call_name(child) in names]


def _nodes_inside_except_handlers(function: ast.AST) -> set[int]:
    """Узлы компенсирующих возвратов в ``except`` — это путь ошибки, не покупки.

    У суточного переключения в обработчике исключения есть собственный
    ``db.commit()`` возврата. Он идёт ниже хука по тексту, но к успешной
    оплате отношения не имеет, поэтому при поиске «последнего коммита»
    такие узлы не учитываются.
    """
    inside: set[int] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.ExceptHandler):
            inside.update(id(child) for child in ast.walk(node))
    return inside


def _keyword_expression(call: ast.Call, name: str) -> str:
    for keyword in call.keywords:
        if keyword.arg == name:
            return ast.unparse(keyword.value)
    raise AssertionError(f'{name} не передан в вызове {_call_name(call)}')


@pytest.mark.parametrize(
    ('function_name', 'source', 'duration_days', 'paid_amount_kopeks'),
    [
        ('confirm_tariff_extend', 'bot_tariff_extend', 'period', 'final_price'),
        ('confirm_tariff_switch', 'bot_tariff_switch', 'period', 'final_price'),
        ('confirm_daily_tariff_switch', 'bot_daily_tariff_switch', '1', 'final_daily_price'),
        ('handle_custom_confirm', 'bot_custom_tariff_purchase', 'custom_days', 'total_price'),
    ],
)
def test_tariff_purchase_function_body_registers_first_purchase(
    function_name: str,
    source: str,
    duration_days: str,
    paid_amount_kopeks: str,
) -> None:
    """Проверяем именно тело функции, а не текст модуля.

    Файловая проверка на подстроку пропустила дыру в простом продлении,
    поэтому здесь хук ищется в AST конкретной функции: ровно один вызов,
    после последнего коммита платежа и до уведомления админу.
    """
    function = _function_node(TARIFF_PURCHASE_FILE, function_name)

    hooks = _calls(function, (FIRST_PURCHASE_HOOK,))
    assert len(hooks) == 1, f'{function_name}: ожидается один вызов {FIRST_PURCHASE_HOOK}, найдено {len(hooks)}'
    hook = hooks[0]

    except_nodes = _nodes_inside_except_handlers(function)
    commits = [call for call in _calls(function, COMMIT_CALLS) if id(call) not in except_nodes]
    assert commits, f'{function_name}: не найден ни create_transaction, ни commit'
    last_commit_line = max(call.lineno for call in commits)
    assert hook.lineno > last_commit_line, (
        f'{function_name}: хук на строке {hook.lineno} стоит до последнего коммита платежа (строка {last_commit_line})'
    )

    notifications = _calls(function, ('send_subscription_purchase_notification',))
    assert notifications, f'{function_name}: не найдено уведомление админу'
    first_notification_line = min(call.lineno for call in notifications)
    assert hook.lineno < first_notification_line, (
        f'{function_name}: хук должен стоять до уведомления админу (строка {first_notification_line})'
    )

    assert _keyword_expression(hook, 'referee_id') == 'db_user.id'
    assert _keyword_expression(hook, 'subscription_id') == 'subscription.id'
    assert _keyword_expression(hook, 'duration_days') == duration_days
    assert _keyword_expression(hook, 'paid_amount_kopeks') == paid_amount_kopeks
    assert _keyword_expression(hook, 'source') == repr(source)


def _enclosing_ifs(function: ast.AST, target: ast.AST) -> list[ast.If]:
    """Все ``if`` в функции, внутри тела которых находится target."""
    found: list[ast.If] = []
    for node in ast.walk(function):
        if isinstance(node, ast.If) and any(child is target for child in ast.walk(node)):
            found.append(node)
    return found


def test_menu_quick_activation_finalize_skips_internal_hook() -> None:
    """menu сам зовёт хук после обеих веток — finalize получает skip=True.

    Иначе тот же no-op хук открывал бы вторую сессию сразу после первой.
    """
    function = _function_node('app/handlers/menu.py', 'handle_activate_button')

    finalize_calls = _calls(function, ('finalize',))
    assert len(finalize_calls) == 1, f'ожидается один finalize, найдено {len(finalize_calls)}'
    assert _keyword_expression(finalize_calls[0], 'skip_first_purchase_hook') == 'True'

    # Собственный хук menu остаётся: иначе оплата из меню не поставит маркер.
    assert len(_calls(function, (FIRST_PURCHASE_HOOK,))) == 1


def test_instant_switch_paid_upgrade_calls_first_purchase_hook() -> None:
    """Платное мгновенное переключение — первая покупка; free/downgrade — нет.

    Файловая проверка на подстроку здесь бесполезна: в ``tariff_purchase.py``
    уже есть хуки других флоу. Ищем вызов в теле ``confirm_instant_switch`` и
    требуем, чтобы он стоял внутри ветки ``is_upgrade and upgrade_cost > 0``.
    """
    function = _function_node(TARIFF_PURCHASE_FILE, 'confirm_instant_switch')

    hooks = _calls(function, (FIRST_PURCHASE_HOOK,))
    assert len(hooks) == 1, f'ожидается один вызов {FIRST_PURCHASE_HOOK}, найдено {len(hooks)}'
    hook = hooks[0]

    branch_tests = [ast.unparse(node.test) for node in _enclosing_ifs(function, hook)]
    assert 'is_upgrade and upgrade_cost > 0' in branch_tests, (
        f'хук обязан быть в платной upgrade-ветке, охватывающие if: {branch_tests}'
    )

    assert _keyword_expression(hook, 'referee_id') == 'db_user.id'
    assert _keyword_expression(hook, 'subscription_id') == 'subscription.id'
    assert _keyword_expression(hook, 'duration_days') == 'max(1, remaining_days)'
    assert _keyword_expression(hook, 'paid_amount_kopeks') == 'upgrade_cost'
    assert _keyword_expression(hook, 'source') == repr('bot_instant_switch')

    commits = [call for call in _calls(function, ('create_transaction',)) if call.lineno < hook.lineno]
    assert commits, 'хук обязан стоять после create_transaction платежа'
    assert hook.lineno > max(call.lineno for call in commits)


AUTO_PURCHASE_FILE = 'app/services/subscription_auto_purchase_service.py'
DAILY_SUBSCRIPTION_FILE = 'app/services/daily_subscription_service.py'


def test_auto_extend_expired_after_topup_registers_first_purchase() -> None:
    """Продление истёкшей подписки с баланса после пополнения — живая оплата.

    Для нетриальной подписки, созданной бесплатно (купон/подарок/админка),
    первое реальное списание приходит именно сюда: продление, купленное
    балансом. Хук стоит после ``create_transaction`` (деньги и транзакция уже
    зафиксированы) и до синка RemnaWave. Награда пишется из другой сессии,
    поэтому при ``rewarded`` подписку перечитывают — иначе ``new_end_date``
    и уведомления покажут срок без бонусных дней.
    """
    function = _function_node(AUTO_PURCHASE_FILE, 'try_auto_extend_expired_after_topup')
    source = _function_source(AUTO_PURCHASE_FILE, 'try_auto_extend_expired_after_topup')

    hooks = _calls(function, (FIRST_PURCHASE_HOOK,))
    assert len(hooks) == 1, f'ожидается один вызов {FIRST_PURCHASE_HOOK}, найдено {len(hooks)}'
    hook = hooks[0]

    assert _keyword_expression(hook, 'referee_id') == 'user.id'
    assert _keyword_expression(hook, 'subscription_id') == 'updated_subscription.id'
    assert _keyword_expression(hook, 'duration_days') == 'period_days'
    assert _keyword_expression(hook, 'paid_amount_kopeks') == 'renewal_cost'
    assert _keyword_expression(hook, 'source') == repr('topup_auto_extend_expired')
    assert _keyword_expression(hook, 'bot') == 'bot'

    except_nodes = _nodes_inside_except_handlers(function)
    transactions = [call for call in _calls(function, ('create_transaction',)) if id(call) not in except_nodes]
    assert transactions, 'не найден create_transaction платежа'
    assert hook.lineno > max(call.lineno for call in transactions), 'хук обязан стоять после фиксации платежа'

    hook_index = source.index(FIRST_PURCHASE_HOOK)
    reward_index = source.index("first_purchase_result.status == 'rewarded'", hook_index)
    refresh_index = source.index('await db.refresh(updated_subscription)', reward_index)
    end_date_index = source.index('new_end_date = updated_subscription.end_date', refresh_index)
    assert hook_index < reward_index < refresh_index < end_date_index


def test_daily_resume_after_topup_registers_first_purchase() -> None:
    """Возобновление суточной подписки после пополнения — тоже оплата.

    Списание с баланса и ``update_daily_charge_time`` (+24 часа) уже прошли,
    когда пишется маркер первой покупки. Хук стоит после обоих и до синка
    RemnaWave. При ``rewarded`` подписку перечитывают, чтобы уведомления
    показали срок вместе с бонусными днями.
    """
    function = _function_node(AUTO_PURCHASE_FILE, 'try_resume_disabled_daily_after_topup')
    source = _function_source(AUTO_PURCHASE_FILE, 'try_resume_disabled_daily_after_topup')

    hooks = _calls(function, (FIRST_PURCHASE_HOOK,))
    assert len(hooks) == 1, f'ожидается один вызов {FIRST_PURCHASE_HOOK}, найдено {len(hooks)}'
    hook = hooks[0]

    assert _keyword_expression(hook, 'referee_id') == 'user.id'
    assert _keyword_expression(hook, 'subscription_id') == 'subscription.id'
    assert _keyword_expression(hook, 'duration_days') == '1'
    assert _keyword_expression(hook, 'paid_amount_kopeks') == 'daily_price'
    assert _keyword_expression(hook, 'source') == repr('topup_daily_resume')
    assert _keyword_expression(hook, 'bot') == 'bot'

    except_nodes = _nodes_inside_except_handlers(function)
    charges = [call for call in _calls(function, ('update_daily_charge_time',)) if id(call) not in except_nodes]
    assert charges, 'не найден update_daily_charge_time'
    assert hook.lineno > max(call.lineno for call in charges), 'хук обязан стоять после продления срока'

    transactions = [call for call in _calls(function, ('create_transaction',)) if id(call) not in except_nodes]
    assert transactions, 'не найден create_transaction платежа'
    assert hook.lineno > max(call.lineno for call in transactions), 'хук обязан стоять после фиксации платежа'

    hook_index = source.index(FIRST_PURCHASE_HOOK)
    reward_index = source.index("first_purchase_result.status == 'rewarded'", hook_index)
    refresh_index = source.index('await db.refresh(subscription)', reward_index)
    notification_index = source.index('send_subscription_extension_notification', refresh_index)
    assert hook_index < reward_index < refresh_index < notification_index


def test_daily_scheduler_charge_registers_first_purchase() -> None:
    """Планировщик суточных списаний — та же оплата, что и ручное продление.

    Хук обязан стоять после атомарного коммита (баланс + транзакция +
    charge_time), до синка RemnaWave. При ``rewarded`` подписку перечитывают,
    чтобы ``subscription.end_date`` в уведомлениях и панели включал бонус.
    """
    function = _function_node(DAILY_SUBSCRIPTION_FILE, '_process_single_charge')
    source = _function_source(DAILY_SUBSCRIPTION_FILE, '_process_single_charge')

    hooks = _calls(function, (FIRST_PURCHASE_HOOK,))
    assert len(hooks) == 1, f'ожидается один вызов {FIRST_PURCHASE_HOOK}, найдено {len(hooks)}'
    hook = hooks[0]

    assert _keyword_expression(hook, 'referee_id') == 'user.id'
    assert _keyword_expression(hook, 'subscription_id') == 'subscription.id'
    assert _keyword_expression(hook, 'duration_days') == '1'
    assert _keyword_expression(hook, 'paid_amount_kopeks') == 'daily_price'
    assert _keyword_expression(hook, 'source') == repr('daily_scheduler')
    assert _keyword_expression(hook, 'bot') == 'self._bot'

    except_nodes = _nodes_inside_except_handlers(function)
    commits = [call for call in _calls(function, ('commit',)) if id(call) not in except_nodes]
    assert commits, 'не найден commit списания'
    assert hook.lineno > min(call.lineno for call in commits), 'хук обязан стоять после атомарного коммита'

    transactions = [call for call in _calls(function, ('create_transaction',)) if id(call) not in except_nodes]
    assert transactions, 'не найден create_transaction платежа'
    assert hook.lineno > max(call.lineno for call in transactions), 'хук обязан стоять после создания транзакции'

    hook_index = source.index(FIRST_PURCHASE_HOOK)
    reward_index = source.index("first_purchase_result.status == 'rewarded'", hook_index)
    refresh_index = source.index('await db.refresh(subscription)', reward_index)
    notification_index = source.index('send_subscription_extension_notification', refresh_index)
    assert hook_index < reward_index < refresh_index < notification_index
