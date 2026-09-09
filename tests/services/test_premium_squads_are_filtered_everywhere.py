"""Каждая отправка сквадов в панель обязана уважать снятые премиум-сквады.

Сквад, снятый воркером за перерасход премиум-лимита, живёт только в панели: в
`subscription.connected_squads` он остаётся, потому что право на него у подписки
никуда не делось. Значит любое место, которое пишет в панель
``activeInternalSquads``, обязано пропустить набор через
``effective_panel_squads`` — иначе ближайшая синхронизация вернёт сквад и снимет
ограничение.

Мест таких два десятка, и они расползаются по слоям: сервисы, роуты кабинета,
хендлеры админки. Точечная проверка каждого не удержит инвариант — новый вызов
добавят и не вспомнят. Поэтому проверяется весь ``app/`` целиком.
"""

from __future__ import annotations

import pathlib
import re


APP = pathlib.Path(__file__).resolve().parents[2] / 'app'

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
