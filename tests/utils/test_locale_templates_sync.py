"""Локаль из репозитория должна доезжать до бота при каждом запуске.

Локаль грузится в два слоя: файлы репозитория (``app/localization/locales``)
и внешняя копия из ``LOCALES_PATH`` (по умолчанию ``./locales``), причём
внешняя побеждает при слиянии (``_merge_dicts`` в ``loader.py``).

``ensure_locale_templates`` вызывается на каждом старте, но существующие файлы
пропускала — внешняя копия оставалась снапшотом первого запуска. Правки текстов
в репозитории до бота не доходили: правишь локаль, перезапускаешь, а на экране
старый текст. Именно так потерялись ``<tg-emoji>`` в ``AUTOPAY_MENU_TEXT``.

Тест проверяет, что устаревшая копия перезаписывается содержимым репозитория.
"""

import json

import pytest

from app.config import settings
from app.localization import loader


def _repo_locale(name: str) -> dict:
    path = loader._DEFAULT_LOCALES_DIR / name
    return json.loads(path.read_text(encoding='utf-8'))


@pytest.fixture
def locales_dir(tmp_path, monkeypatch):
    """Внешняя папка локалей вместо ./locales — чтобы не трогать рабочую.

    ``LOCALES_PATH`` — поле pydantic-модели, а не атрибут класса, поэтому
    обычный monkeypatch.setattr по классу его не находит.
    """
    monkeypatch.setattr(settings, 'LOCALES_PATH', str(tmp_path))
    return tmp_path


def test_stale_copy_is_overwritten(locales_dir):
    """Копия, отставшая от репозитория, обновляется при старте."""
    stale = locales_dir / 'ru.json'
    stale.write_text('{"AUTOPAY_MENU_TEXT": "старый текст позапрошлого запуска"}', encoding='utf-8')

    loader.ensure_locale_templates()

    assert json.loads(stale.read_text(encoding='utf-8')) == _repo_locale('ru.json')


def test_all_repo_locales_are_copied(locales_dir):
    """Зеркалится весь набор из репозитория, а не только ru/en/fa."""
    loader.ensure_locale_templates()

    for source in loader._DEFAULT_LOCALES_DIR.glob('*.json'):
        target = locales_dir / source.name
        assert target.exists(), f'{source.name} не скопирован'
        assert json.loads(target.read_text(encoding='utf-8')) == _repo_locale(source.name)


def test_foreign_file_is_left_alone(locales_dir):
    """Файл, которого нет в репозитории, — чужой, его не трогаем."""
    custom = locales_dir / 'custom-notes.json'
    custom.write_text('{"my": "заметка оператора"}', encoding='utf-8')

    loader.ensure_locale_templates()

    assert json.loads(custom.read_text(encoding='utf-8')) == {'my': 'заметка оператора'}


def test_missing_dir_is_created(tmp_path, monkeypatch):
    """Папки нет — создаётся вместе с файлами (первый запуск)."""
    target_dir = tmp_path / 'nested' / 'locales'
    monkeypatch.setattr(settings, 'LOCALES_PATH', str(target_dir))

    loader.ensure_locale_templates()

    assert (target_dir / 'ru.json').exists()
