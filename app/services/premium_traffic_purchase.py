"""Докупка премиум-трафика по отдельному скваду.

Отдельно от обычной докупки: у премиум-сквада своя цена, свои пакеты и свой
потолок, заданные в тарифе (``server_traffic_limits[uuid]``). Общий
``traffic_topup_packages`` тарифа сюда не применяется и наоборот.

Докупленное живёт до конца текущего периода: ``extra_bytes`` обнуляется вместе с
периодом, как и ``used_bytes``. Это осознанно — иначе купленные гигабайты
копились бы из месяца в месяц и лимит перестал бы что-либо ограничивать.

Правила цены здесь, а не в роутере: покупать премиум умеют и кабинет, и бот, а
разойтись в цене они не должны.

Рубильник ``PREMIUM_TRAFFIC_ENABLED`` проверяется здесь же, до какого-либо
списания: без воркера (``PremiumTrafficService``) купленную квоту никто не
применит и не израсходует, а деньги за неё всё равно спишутся. Проверка в
сервисном слое, а не в роутере, — чтобы её унаследовал и будущий бот-флоу
покупки, который появится позже и будет дёргать эти же функции напрямую.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.premium_traffic import add_extra_bytes, get_or_create_state, get_state
from app.utils.premium_traffic import BYTES_IN_GB, PremiumSquadConfig, get_premium_squads_for_tariff


logger = structlog.get_logger(__name__)


class PremiumTopupError(Exception):
    """Докупка невозможна. ``code`` переводится вызывающим в ответ или текст."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class PremiumTopupQuote:
    """Проверенное намерение купить: сквад, объём и цена до скидок."""

    squad_uuid: str
    gb: int
    base_price_kopeks: int
    config: PremiumSquadConfig

    @property
    def bytes(self) -> int:
        return self.gb * BYTES_IN_GB


def _premium_traffic_enabled() -> bool:
    """Тот же рубильник, что у воркера учёта (``premium_traffic_service``).

    Выключен — значит, никто не считает расход и не снимает доступ по квоте:
    продавать её в таком состоянии нельзя, купленное не будет ни применено, ни
    израсходовано.
    """
    return bool(getattr(settings, 'PREMIUM_TRAFFIC_ENABLED', True))


def get_premium_topup_options(subscription) -> dict[str, PremiumSquadConfig]:
    """Сквады подписки, где докупка премиум-трафика включена и есть пакеты."""
    if not _premium_traffic_enabled():
        # Рубильник выключен — докупку нигде не показываем, включая кнопку/пункт
        # в интерфейсе: список вариантов для UI как раз строится отсюда.
        return {}

    configs = get_premium_squads_for_tariff(getattr(subscription, 'tariff', None))
    connected = set(subscription.connected_squads or [])
    return {
        uuid: config
        for uuid, config in configs.items()
        # Право на сквад — обязательное условие: платить за трафик по серверу,
        # которого нет в подписке, пользователь не должен.
        if config.topup_enabled and uuid in connected
    }


def _ensure_topup_fits(config: PremiumSquadConfig, already_bytes: int | None, gb: int) -> None:
    """Проверить потолок докупки за период. Одно правило на оба места проверки.

    ``max_topup_gb == 0`` — «без ограничения»: это настройка тарифа, а не
    нулевой потолок.
    """
    if config.max_topup_gb <= 0:
        return

    already_gb = (already_bytes or 0) / BYTES_IN_GB
    if already_gb + gb > config.max_topup_gb:
        raise PremiumTopupError(
            'topup_limit_reached',
            f'Больше {config.max_topup_gb} ГБ за период докупить нельзя (уже докуплено {already_gb:.0f} ГБ)',
        )


def _ensure_squad_is_not_closed(state) -> None:
    """Отказать, если сквад закрыт администратором (``closed_at``).

    Единая точка проверки для обоих вызовов (``quote_premium_topup`` и
    ``apply_premium_topup``): без неё покупка через веб-кабинет проходила бы
    молча — `quote_premium_topup` цену считает, `apply_premium_topup`
    начисляет `extra_bytes` (Task 9 намеренно не снимает этим закрытие), а
    доступ так и остаётся закрыт. Деньги списаны, ничего не открыто.

    Гейт строго на ``closed_at``, а не на ``is_limited``: сквад в состоянии
    REOPENED_PENDING (администратор снял закрытие, но воркер ещё не вернул
    сквад в панель) имеет ``closed_at is None`` при всё ещё поднятом
    ``is_limited`` — и такую покупку отклонять нельзя, `apply_premium_topup`
    сама снимет ограничение при начислении (см. `test_admin_users_premium.py`,
    `PremiumSquadCardState.REOPENED_PENDING`).
    """
    if state is not None and state.closed_at is not None:
        raise PremiumTopupError(
            'squad_closed',
            'Доступ к этому серверу закрыт администратором, докупка недоступна',
        )


async def quote_premium_topup(
    db: AsyncSession,
    subscription,
    squad_uuid: str,
    gb: int,
) -> PremiumTopupQuote:
    """Проверить возможность покупки и посчитать цену до скидок."""
    if not _premium_traffic_enabled():
        # Отказ обязан случиться раньше любого списания: это единственная точка
        # входа для покупки — и в кабинете, и в будущем боте — так что деньги за
        # неприменимую квоту здесь просто не доходят до списания.
        raise PremiumTopupError(
            'feature_disabled',
            'Докупка премиум-трафика временно отключена',
        )

    options = get_premium_topup_options(subscription)
    config = options.get(squad_uuid)
    if config is None:
        raise PremiumTopupError('topup_unavailable', 'Докупка премиум-трафика для этого сервера недоступна')

    price = config.price_kopeks_for(gb)
    if price is None:
        raise PremiumTopupError('package_not_found', f'Пакет {gb} ГБ не настроен для этого сервера')

    # Состояние читаем один раз для обеих проверок ниже (закрытие и потолок):
    # это единственная точка входа для покупки что в боте, что в кабинете, и
    # закрытие сквада администратором проверяется здесь у самой власти, а не у
    # каждого вызывающего по отдельности (см. Task 13).
    state = await get_state(db, subscription.id, squad_uuid)
    _ensure_squad_is_not_closed(state)

    if config.max_topup_gb > 0:
        # Быстрый отказ до списания: читаем без блокировки, потому что решение
        # по этому чтению не записывается. Окончательная проверка — в
        # apply_premium_topup, под блокировкой строки.
        _ensure_topup_fits(config, state.extra_bytes if state else 0, gb)

    return PremiumTopupQuote(squad_uuid=squad_uuid, gb=gb, base_price_kopeks=price, config=config)


async def apply_premium_topup(
    db: AsyncSession,
    subscription,
    quote: PremiumTopupQuote,
    *,
    period_start_at,
) -> tuple[object, bool]:
    """Начислить купленный объём.

    Возвращает состояние и признак, что сквад был снят и теперь возвращается —
    вызывающему это нужно, чтобы отправить набор сквадов в панель и уведомить
    пользователя.

    Состояние создаётся, если воркер до подписки ещё не дошёл: покупка не должна
    ждать первого прохода.

    Бросает ``PremiumTopupError('topup_limit_reached')``, если к моменту
    начисления потолок периода уже выбран соседней покупкой. Вызывающий обязан
    откатить транзакцию: списание и начисление должны жить и умирать вместе.
    """
    # Сессия живёт с autoflush=False (как в проде), а читать состояние мы будем
    # с populate_existing — то есть поверх объекта в памяти лягут значения из
    # БД. Собственные несохранённые изменения надо отправить в БД до этого,
    # иначе перечитывание их потеряет. Это flush в открытой транзакции, не
    # commit: списание вызывающего по-прежнему можно откатить.
    await db.flush()
    state = await get_or_create_state(
        db,
        subscription.id,
        quote.squad_uuid,
        limit_bytes=quote.config.limit_bytes,
        period_start_at=period_start_at,
        for_update=True,
    )
    # Проверка в quote читала строку без блокировки, и параллельная покупка
    # могла начислить своё уже после неё: обе увидели бы один остаток и обе
    # прошли. Решающая проверка — здесь, по заблокированной строке, в той же
    # транзакции, что и начисление. Тем же приёмом lock_user_for_pricing
    # защищает баланс.
    _ensure_topup_fits(quote.config, state.extra_bytes, quote.gb)
    # То же самое для закрытия: quote проверяла closed_at без блокировки, и
    # администратор мог закрыть сквад в промежутке между quote и apply (списание
    # балансом ещё не значит, что начисление уже случилось). Перечитанное здесь
    # состояние — то же самое, под FOR UPDATE, так что вторая проверка не лишняя.
    _ensure_squad_is_not_closed(state)

    was_limited = bool(state.is_limited)
    add_extra_bytes(state, quote.bytes)
    restored = was_limited and not state.is_limited

    logger.info(
        'Докуплен премиум-трафик',
        subscription_id=subscription.id,
        squad_uuid=quote.squad_uuid,
        gb=quote.gb,
        restored=restored,
    )
    return state, restored
