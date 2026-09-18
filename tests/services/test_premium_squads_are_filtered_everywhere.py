"""Каждая отправка сквадов в панель обязана уважать снятые премиум-сквады.

Сквад, снятый воркером за перерасход премиум-лимита, живёт только в панели: в
`subscription.connected_squads` он остаётся, потому что право на него у подписки
никуда не делось. Значит любое место, которое пишет в панель
``activeInternalSquads``, обязано пропустить набор через
``effective_panel_squads`` (напрямую или через вынесенный помощник
``_without_limited_premium_squads``) — иначе ближайшая синхронизация вернёт
сквад и снимет ограничение.

Два слоя проверки намеренно избыточны: AST-проверка (`test_every_squad_writer_filters_limited_squads`
и соседи) смотрит по имени на два известных писателя в `app/services/panel_sync/writer.py`
и не заметит новый вызов панели где-то ещё; регекс-сканер (`test_every_panel_squad_write_is_guarded`
и соседи) проходит по всему ``app/`` и ловит именно такой дрейф ценой более
грубого правила. Один без другого инвариант не удержит.
"""

from __future__ import annotations

import ast
import pathlib
import re


APP = pathlib.Path(__file__).resolve().parents[2] / 'app'
WRITER = APP / 'services' / 'panel_sync' / 'writer.py'

# Клиент панели — не место отправки, а сама отправка: там kwarg превращается в
# поле запроса, и фильтровать в нём нечего (нет ни подписки, ни сессии БД).
TRANSPORT_LAYER = {'app/external/remnawave_api.py'}

# Grace-механизм отправляет не права подписки, а снимок панельного состояния:
# `overlay.squad_uuids` и `target.squad_uuids` собираются из того, что в панели
# уже было (grace_access_runtime.py, _extract_panel_squads). Если сквад к тому
# моменту снят, в снимок он не попадёт и не вернётся.
#
# Дыра остаётся одна: grace-сессия, открытая ДО снятия, восстановит набор со
# сквадом. Специально не фильтруем — grace сверяет фактическое состояние панели
# с ожидаемым (panel_matches_overlay, _panel_matches_limited_intermediate), и
# подмена отправляемого набора выглядела бы для него конфликтом. Окно закрывает
# сам воркер: следующий проход снимет сквад снова.
GRACE_OVERLAY_SITES = {
    'app/services/grace_access_runtime.py',
}

# `PanelPayload` — структура с полями запроса, а не отправка: `build_panel_payload`
# складывает в неё `connected_squads` как есть, а `create_kwargs`/`update_kwargs`
# перекладывают собственное поле в kwargs. Отфильтровать здесь нечем — сборка
# синхронная, ни подписки в методах, ни сессии БД у неё нет.
#
# Наружу payload уходит ровно двумя дверями, и обе фильтруют:
# `writer.push_subscription` (единственный путь записи состояния подписки) и
# `writer.patch_panel_squads` (сквады тарифа). Единственный внешний сборщик
# payload — `subscription_service.update_remnawave_user` — фильтрует сам, потому
# что его грейс-ветка пишет в панель мимо writer.
PAYLOAD_ASSEMBLY = {'app/services/panel_sync/payload.py'}

# Файл, через который проходит всякая запись сквадов в панель. Раз payload
# выведен из-под проверки, страж обязан стоять здесь — иначе исключение выше
# превращается в дыру.
WRITE_DOOR = 'app/services/panel_sync/writer.py'

ASSIGNMENT = re.compile(r"""active_internal_squads\s*(?:=|'\]\s*=|"\]\s*=|'\s*:|"\s*:)""")

GUARD = 'effective_panel_squads'
# Присваивание может переноситься на следующие строки — ищем страж в пределах
# выражения, а не в одной строке.
STATEMENT_LOOKAHEAD = 4

# Функции пакета, которые отправляют в панель набор сквадов. `patch_panel_account`
# сюда не входит намеренно: он правит карточку человека и сквадов не касается.
SQUAD_WRITERS = ('push_subscription', 'patch_panel_squads')


def _collect_sites() -> list[tuple[str, int, str]]:
    sites: list[tuple[str, int, str]] = []
    for path in sorted(APP.rglob('*.py')):
        rel = path.relative_to(APP.parent).as_posix()
        if rel in TRANSPORT_LAYER:
            continue
        lines = path.read_text(encoding='utf-8').splitlines()
        for number, line in enumerate(lines, 1):
            if ASSIGNMENT.search(line):
                statement = ' '.join(lines[number - 1 : number - 1 + STATEMENT_LOOKAHEAD])
                sites.append((rel, number, statement))
    return sites


def _function(name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(WRITER.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name} не найдена в {WRITER.name}: сторож смотрит не туда')


def _calls_guard(node: ast.AST) -> bool:
    return any(
        isinstance(inner, ast.Call)
        and (
            (isinstance(inner.func, ast.Name) and inner.func.id == GUARD)
            or (isinstance(inner.func, ast.Attribute) and inner.func.attr == GUARD)
            # Вынесенный помощник считается: важно, что фильтр вызывается.
            or (isinstance(inner.func, ast.Name) and inner.func.id.startswith('_without_limited'))
        )
        for inner in ast.walk(node)
    )


def test_every_panel_squad_write_is_guarded():
    exempt = GRACE_OVERLAY_SITES | PAYLOAD_ASSEMBLY
    unguarded = [
        f'{rel}:{number}' for rel, number, statement in _collect_sites() if GUARD not in statement and rel not in exempt
    ]

    assert not unguarded, (
        'Эти места пишут сквады в панель мимо effective_panel_squads — '
        'снятый за перерасход премиум-сквад вернётся пользователю:\n  ' + '\n  '.join(unguarded)
    )


def test_the_guard_is_actually_used():
    """Страховка от обратного: правило есть, а применять его перестали."""
    guarded = [site for site in _collect_sites() if GUARD in site[2]]

    assert guarded, 'Ни одно место не использует effective_panel_squads — фильтр потерян'


def test_grace_exception_list_does_not_rot():
    """Список исключений должен указывать на существующие места отправки."""
    files_with_sites = {rel for rel, _, _ in _collect_sites()}

    stale = (GRACE_OVERLAY_SITES | PAYLOAD_ASSEMBLY) - files_with_sites
    assert not stale, f'В списке исключений файлы без отправки сквадов: {sorted(stale)}'


def test_the_write_door_is_guarded():
    """Исключение для payload держится на том, что фильтр стоит в writer."""
    guarded = {rel for rel, _, statement in _collect_sites() if GUARD in statement}

    assert WRITE_DOOR in guarded, (
        f'{WRITE_DOOR} перестал фильтровать сквады, а {sorted(PAYLOAD_ASSEMBLY)} выведен '
        'из-под проверки на том основании, что фильтрует именно он'
    )


def test_every_squad_writer_filters_limited_squads():
    unguarded = [name for name in SQUAD_WRITERS if not _calls_guard(_function(name))]

    assert not unguarded, (
        'Эти функции отправляют сквады в панель мимо фильтра — снятый за '
        f'перерасход премиум-сквад вернётся пользователю: {unguarded}'
    )


def test_account_patcher_does_not_touch_squads():
    """`patch_panel_account` фильтровать нечего — и он не должен знать о сквадах.

    Если сквады появятся и там, фильтр придётся ставить и туда, а сторож выше
    об этом не узнает: он смотрит на заранее известный список.
    """
    source = ast.unparse(_function('patch_panel_account'))

    assert 'active_internal_squads' not in source, (
        'patch_panel_account начал писать сквады — добавьте его в SQUAD_WRITERS и поставьте фильтр'
    )


def test_guard_is_reachable_from_the_writer():
    """Страховка от обратного: правило есть, а импорт потеряли."""
    assert GUARD in WRITER.read_text(encoding='utf-8'), (
        f'{WRITER.name} перестал ссылаться на {GUARD} — фильтр премиум-сквадов потерян'
    )


def _squad_patch_calls() -> list[tuple[str, ast.Call]]:
    calls = []
    for path in sorted(APP.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name == 'patch_panel_squads':
                calls.append((f'{path.relative_to(APP.parent).as_posix()}:{node.lineno}', node))
    return calls


def test_every_squad_patch_names_its_subscription():
    """Каждый вызов `patch_panel_squads` обязан передать `subscription_id`.

    Параметр обязательный, так что пропуск не обойдёт фильтр молча — вызов упадёт
    с `TypeError`. Но падать он будет в рантайме, а вызывают его фоновые задачи,
    которые ловят исключение и пишут warning: синхронизация сквадов после правки
    тарифа тихо перестала бы работать у всех. Тесты этого не заметят — фоновую
    синхронизацию в них подменяют целиком.

    Так уже чуть не случилось: в 4.9.0 синхронизацию вынесли в
    `tariff_squad_sync`, и новый вызов пришёл без идентификатора подписки.
    """
    calls = _squad_patch_calls()
    missing = [where for where, call in calls if not any(kw.arg == 'subscription_id' for kw in call.keywords)]

    assert not missing, (
        'Вызов patch_panel_squads без subscription_id упадёт в рантайме, а в фоне — '
        f'молча. Передайте id подписки: {missing}'
    )


def test_squad_patch_scan_finds_the_callers():
    """Сторож выше не должен проходить вхолостую, если сканер перестал видеть вызовы.

    Вызовов два: кабинетный роут смены серверов тарифа (`admin_tariffs.py`) и
    фоновая синхронизация (`tariff_squad_sync.py`). Премиум-воркер
    (`premium_traffic_service.py`) сквады тоже переотправляет, но через
    `_push_subscription_squads`/`update_panel_user_grace_safe` напрямую в уже
    открытой сессии — `patch_panel_squads` открыл бы вторую сессию и не увидел
    бы незакоммиченный `is_limited` (см. комментарий у `_push_subscription_squads`).
    """
    assert len(_squad_patch_calls()) >= 2, 'сканер не нашёл вызовов patch_panel_squads — сторож смотрит не туда'
