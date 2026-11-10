"""Тексты премиум-трафика должны быть в поддерживаемых языках и с теми же подстановками.

Политика проекта — только русский; файлы en/ua/fa/zh сохранены, но их
содержимое не является контрактом. Пропущенный ключ в ru виден сразу:
`texts.t(key, default)` молча отдаёт запасной текст. Подстановка, которая
разошлась с кодом, ещё хуже: `.format` бросит KeyError в момент отправки, то
есть в проде.

Паритетные проверки включаются автоматически, когда язык добавляется в
``SUPPORTED_LANGUAGES`` (см. ``locale_policy``).
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
from locale_policy import LOCALE_FILES, SUPPORTED_LANGUAGES


LOCALES = pathlib.Path(__file__).resolve().parents[1] / 'app' / 'localization' / 'locales'

PREMIUM_KEYS = (
    'PREMIUM_TRAFFIC_LABEL',
    'PREMIUM_TRAFFIC_WARNING',
    'PREMIUM_TRAFFIC_EXHAUSTED',
    'PREMIUM_TRAFFIC_RESTORED',
)

PLACEHOLDER = re.compile(r'{(\w+)}')
_ONLY_ONE_LANGUAGE = pytest.mark.skipif(
    len(SUPPORTED_LANGUAGES) < 2,
    reason='Поддерживается только ru: остальные файлы локалей сохранены, но не являются контрактом',
)


def _load() -> dict[str, dict[str, str]]:
    return {path.stem: json.loads(path.read_text(encoding='utf-8')) for path in sorted(LOCALES.glob('*.json'))}


def test_all_locale_files_are_present():
    """Пять файлов заявлены в проекте; остальные языки не поддерживаются, но не удалены."""
    assert set(_load()) == set(LOCALE_FILES)


def test_premium_keys_exist_in_supported_languages():
    locales = _load()
    missing = {lang: [key for key in PREMIUM_KEYS if key not in locales[lang]] for lang in SUPPORTED_LANGUAGES}
    missing = {lang: keys for lang, keys in missing.items() if keys}

    assert not missing, f'нет переводов премиум-трафика: {missing}'


@_ONLY_ONE_LANGUAGE
def test_placeholders_match_across_supported_languages():
    """Разошедшиеся подстановки уронят отправку KeyError только на одном языке."""
    locales = _load()
    for key in PREMIUM_KEYS:
        reference = PLACEHOLDER.findall(locales['ru'][key])
        for lang in SUPPORTED_LANGUAGES:
            value = locales[lang].get(key)
            if value is None:
                # Отсутствие ключа ловит test_premium_keys_exist_in_supported_languages.
                continue
            assert PLACEHOLDER.findall(value) == reference, (
                f'{lang}.{key}: подстановки {PLACEHOLDER.findall(value)} не совпадают с русскими {reference}'
            )


@_ONLY_ONE_LANGUAGE
def test_translations_are_not_copies_of_russian():
    """Забытый перевод легко пропустить — он «работает», но не на том языке."""
    locales = _load()
    for lang in SUPPORTED_LANGUAGES:
        if lang == 'ru':
            continue
        for key in PREMIUM_KEYS:
            value = locales[lang].get(key)
            if value is None:
                continue
            assert value != locales['ru'][key], f'{lang}.{key} — копия русского текста'


@_ONLY_ONE_LANGUAGE
def test_key_sets_are_identical_in_supported_languages():
    """Общий инвариант файлов локализации, а не только премиум-ключей."""
    locales = _load()
    reference = set(locales['ru'])
    for lang in SUPPORTED_LANGUAGES:
        texts = locales[lang]
        assert set(texts) == reference, (
            f'{lang}: нет {sorted(reference - set(texts))[:5]}, лишние {sorted(set(texts) - reference)[:5]}'
        )
