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
