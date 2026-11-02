"""Колонка adblock-DNS у подписки."""

from app.database.models import Subscription


def test_subscription_has_adblock_dns_enabled_column():
    column = Subscription.__table__.columns['adblock_dns_enabled']

    assert column.nullable is False
    assert column.default is not None
    assert column.default.arg is False
    assert column.server_default is not None
