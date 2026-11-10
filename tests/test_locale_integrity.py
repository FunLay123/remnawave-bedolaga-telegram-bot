"""Rock-solid guards for localization integrity.

The bot supports Russian only; locale files for other languages are kept in the
repo but are not a contract. What still matters:
  * texts.t('KEY') with no fallback for a key absent in ru -> runtime KeyError
  * a {placeholder} that differs between *supported* languages -> .format()
    KeyError / missing data at runtime

Parity checks are skipped while only one language is supported and re-enable
automatically when a language is added to the policy in ``locale_policy``.
"""

import ast
import json
import re
from pathlib import Path

import pytest
from locale_policy import LOCALE_FILES, SUPPORTED_LANGUAGES


LOCALE_DIR = Path(__file__).resolve().parents[1] / 'app' / 'localization' / 'locales'
APP_DIR = Path(__file__).resolve().parents[1] / 'app'
PLACEHOLDER_RE = re.compile(r'\{[^}]*\}')
# Keys resolved dynamically in Texts._get_value (not stored in the JSON files).
DYNAMIC_KEYS = {'RULES_TEXT'}
_ONLY_ONE_LANGUAGE = pytest.mark.skipif(
    len(SUPPORTED_LANGUAGES) < 2,
    reason='Поддерживается только ru: остальные файлы локалей сохранены, но не являются контрактом',
)


@pytest.fixture(scope='module')
def locales():
    return {lang: json.loads((LOCALE_DIR / f'{lang}.json').read_text(encoding='utf-8')) for lang in LOCALE_FILES}


@_ONLY_ONE_LANGUAGE
def test_supported_locales_have_identical_keys(locales):
    ru_keys = set(locales['ru'])
    for lang in SUPPORTED_LANGUAGES:
        if lang == 'ru':
            continue
        keys = set(locales[lang])
        missing = sorted(ru_keys - keys)
        extra = sorted(keys - ru_keys)
        assert not missing, f'{lang}.json is missing {len(missing)} keys present in ru: {missing[:15]}'
        assert not extra, f'{lang}.json has {len(extra)} keys not in ru: {extra[:15]}'


@_ONLY_ONE_LANGUAGE
def test_placeholders_consistent_across_supported_locales(locales):
    """Every {placeholder} must be identical across supported languages — the
    code calls .format() on the result, so a drifted placeholder breaks at
    runtime for users of that language."""
    ru = locales['ru']
    problems = []
    for key, ru_val in ru.items():
        if not isinstance(ru_val, str):
            continue
        ru_ph = sorted(PLACEHOLDER_RE.findall(ru_val))
        for lang in SUPPORTED_LANGUAGES:
            if lang == 'ru':
                continue
            val = locales[lang].get(key)
            if not isinstance(val, str):
                continue
            ph = sorted(PLACEHOLDER_RE.findall(val))
            if ph != ru_ph:
                problems.append(f'{key} [{lang}]: ru={ru_ph} vs {lang}={ph}')
    assert not problems, 'Placeholder mismatches (would break .format()):\n' + '\n'.join(problems[:25])


def _iter_t_calls():
    for path in APP_DIR.rglob('*.py'):
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 't'
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                yield path, node


def test_t_calls_without_default_exist_in_ru(locales):
    """texts.t('KEY') with NO fallback raises KeyError if the key is absent from ru.
    Every such referenced key must exist in ru.json."""
    ru = locales['ru']
    offenders = set()
    for path, node in _iter_t_calls():
        if len(node.args) != 1:
            continue
        key = node.args[0].value
        if key not in DYNAMIC_KEYS and key not in ru:
            offenders.add(f'{key}  ({path.relative_to(APP_DIR.parent)})')
    assert not offenders, 'texts.t(key) with no fallback for a key missing from ru.json:\n' + '\n'.join(
        sorted(offenders)[:25]
    )


def test_t_calls_with_static_default_exist_in_ru(locales):
    """texts.t('KEY', 'статический дефолт') с ключом вне ru.json отдаёт русский
    дефолт ВСЕМ языкам — ключ обязан быть в локалях. Динамические дефолты
    (f-строки с настраиваемыми названиями платёжек и т.п.) — осознанный паттерн
    «locale override поверх настройки», их не проверяем."""
    ru = locales['ru']
    offenders = set()
    for path, node in _iter_t_calls():
        if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
            continue
        if not isinstance(node.args[1].value, str):
            continue
        key = node.args[0].value
        if key not in DYNAMIC_KEYS and key not in ru:
            offenders.add(f'{key}  ({path.relative_to(APP_DIR.parent)})')
    assert not offenders, (
        'texts.t(key, static_default) для ключа, отсутствующего в ru.json '
        '(все языки получают дефолт) — добавьте ключ во все локали:\n' + '\n'.join(sorted(offenders)[:25])
    )


@pytest.mark.parametrize(
    'key',
    [
        'registration_invite_required',
        'registration_check_unavailable',
        'registration_contact_support',
    ],
)
def test_invite_only_keys_exist_in_every_supported_locale(locales, key):
    assert all(key in locales[lang] for lang in SUPPORTED_LANGUAGES)
