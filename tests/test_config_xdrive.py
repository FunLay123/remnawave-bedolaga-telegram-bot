"""Настройки xdrive/PasarGuard: по умолчанию фича выключена, конфигурация
считается заданной только при наличии URL, ключа и группы, а проектор получает
безопасные значения интервала и instance id.

``.env.example`` документирует тот же набор ключей: строки закомментированы,
чтобы копирование примера не закрепляло значения в окружении (ENV_OVERRIDE_KEYS).
"""

import re
import socket
from pathlib import Path

from app.config import Settings


ENV_EXAMPLE = Path(__file__).resolve().parents[1] / '.env.example'

XDRIVE_KEYS = (
    'XDRIVE_ENABLED',
    'PASARGUARD_API_URL',
    'PASARGUARD_API_KEY',
    'PASARGUARD_XDRIVE_GROUP_ID',
    'PASARGUARD_USERNAME_PREFIX',
    'PASARGUARD_INSTANCE_ID',
    'PASARGUARD_SYNC_INTERVAL_SECONDS',
    'PASARGUARD_API_TIMEOUT_SECONDS',
    'PASARGUARD_SYNC_BATCH_SIZE',
    'XDRIVE_CONFIG_CACHE_SECONDS',
    'XDRIVE_CLIENT_UA_REGEX',
)


def settings_with(**overrides):
    return Settings(BOT_TOKEN='1:x', **overrides)


def test_xdrive_disabled_by_default():
    assert Settings(BOT_TOKEN='1:x').is_xdrive_enabled() is False


def test_defaults_match_the_interface():
    s = Settings(BOT_TOKEN='1:x')
    assert s.XDRIVE_ENABLED is False
    assert s.PASARGUARD_API_URL is None
    assert s.PASARGUARD_API_KEY is None
    assert s.PASARGUARD_XDRIVE_GROUP_ID is None
    assert s.PASARGUARD_USERNAME_PREFIX == 'bx'
    assert s.PASARGUARD_INSTANCE_ID == ''
    assert s.PASARGUARD_SYNC_INTERVAL_SECONDS == 300
    assert s.PASARGUARD_API_TIMEOUT_SECONDS == 15
    assert s.PASARGUARD_SYNC_BATCH_SIZE == 200
    assert s.XDRIVE_CONFIG_CACHE_SECONDS == 60
    assert s.XDRIVE_CLIENT_UA_REGEX == ''


def test_pasarguard_configured_requires_url_key_and_group():
    assert (
        settings_with(
            PASARGUARD_API_URL='https://panel.example',
            PASARGUARD_API_KEY=None,
            PASARGUARD_XDRIVE_GROUP_ID=None,
        ).is_pasarguard_configured()
        is False
    )
    assert (
        settings_with(
            PASARGUARD_API_URL='https://panel.example',
            PASARGUARD_API_KEY='key',
            PASARGUARD_XDRIVE_GROUP_ID=None,
        ).is_pasarguard_configured()
        is False
    )
    assert (
        settings_with(
            PASARGUARD_API_URL=None,
            PASARGUARD_API_KEY='key',
            PASARGUARD_XDRIVE_GROUP_ID=3,
        ).is_pasarguard_configured()
        is False
    )
    assert (
        settings_with(
            PASARGUARD_API_URL='https://panel.example',
            PASARGUARD_API_KEY='key',
            PASARGUARD_XDRIVE_GROUP_ID=3,
        ).is_pasarguard_configured()
        is True
    )


def test_is_xdrive_enabled_reads_only_the_flag():
    # Флаг — пользовательский рубильник: проектор отдельно смотрит на
    # is_pasarguard_configured(), чтобы flag-off всё равно разводил пользователей.
    assert (
        settings_with(
            XDRIVE_ENABLED=True,
            PASARGUARD_API_URL=None,
            PASARGUARD_API_KEY=None,
            PASARGUARD_XDRIVE_GROUP_ID=None,
        ).is_xdrive_enabled()
        is True
    )
    assert (
        settings_with(
            XDRIVE_ENABLED=False,
            PASARGUARD_API_URL='https://panel.example',
            PASARGUARD_API_KEY='key',
            PASARGUARD_XDRIVE_GROUP_ID=3,
        ).is_xdrive_enabled()
        is False
    )


def test_sync_interval_floor_is_sixty_seconds():
    assert settings_with(PASARGUARD_SYNC_INTERVAL_SECONDS=5).get_xdrive_sync_interval_seconds() == 60
    assert settings_with(PASARGUARD_SYNC_INTERVAL_SECONDS=0).get_xdrive_sync_interval_seconds() == 60
    assert settings_with(PASARGUARD_SYNC_INTERVAL_SECONDS=600).get_xdrive_sync_interval_seconds() == 600


def test_instance_id_falls_back_to_hostname():
    assert settings_with(PASARGUARD_INSTANCE_ID='').get_pasarguard_instance_id() == socket.gethostname()
    assert settings_with(PASARGUARD_INSTANCE_ID='node-1').get_pasarguard_instance_id() == 'node-1'


def test_env_example_documents_commented_defaults():
    active = []
    documented: dict[str, str] = {}
    for raw_line in ENV_EXAMPLE.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        for key in XDRIVE_KEYS:
            if re.match(rf'^{key}=', line):
                active.append(line)
            match = re.match(rf'^#\s*({key})=(.*)$', line)
            if match:
                documented[match.group(1)] = match.group(2).strip()

    assert active == [], f'ключи xdrive не должны быть активны в .env.example: {active}'
    assert '# ===== XDRIVE / PASARGUARD =====' in ENV_EXAMPLE.read_text(encoding='utf-8')

    for key in XDRIVE_KEYS:
        default = Settings.model_fields[key].default
        if default is True:
            expected = 'true'
        elif default is False:
            expected = 'false'
        elif default is None:
            expected = ''
        else:
            expected = str(default)
        assert documented.get(key) == expected, f'{key}: в примере {documented.get(key)!r}, в коде {expected!r}'
