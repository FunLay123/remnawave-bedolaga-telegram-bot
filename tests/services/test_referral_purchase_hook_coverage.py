import ast
from pathlib import Path


EXPECTED_FILES = {
    'app/handlers/subscription/purchase.py',
    'app/handlers/subscription/tariff_purchase.py',
    'app/handlers/simple_subscription.py',
    'app/handlers/menu.py',
    'app/services/subscription_purchase_service.py',
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
