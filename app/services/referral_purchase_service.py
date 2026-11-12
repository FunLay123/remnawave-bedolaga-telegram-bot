"""Маркер первой оплаченной покупки реферала.

Маркер — строка ledger'а с нулевыми деньгами и нулевыми днями, поэтому ни
статистика, ни вывод реферального баланса её не видят. Наличие строки означает
ровно одно: первая оплаченная покупка этого приглашённого уже учтена. Без неё
первая покупка, совершённая до или после привязки реферала, могла бы быть
посчитана дважды — и наградой днями, и поздним бэкфиллом.

Новых колонок и миграций нет намеренно: ledger уже хранит получателя, вторую
сторону пары, причину и время, чего для «одна покупка — один маркер» хватает.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.subscription import add_reward_days
from app.database.crud.transaction import addon_description_clause, daily_charge_clause
from app.database.models import (
    ReferralEarning,
    ReferralRewardType,
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    User,
)
from app.services.referral_reward_service import (
    REASON_FIRST_PURCHASE,
    REASON_FIRST_PURCHASE_REFEREE,
    REASON_FIRST_PURCHASE_REGISTERED,
)
from app.utils.referral_eligibility import is_daily_referral_tariff, is_referral_reward_target


logger = structlog.get_logger(__name__)


def _real_subscription_payment_exists(user_id_column):
    """SQL-условие: у пользователя есть завершённая реальная оплата подписки.

    ``create_transaction`` хранит списание отрицательным, но старые и
    провайдерные строки могли сохранить сумму положительной — поэтому
    проверяется ненулевая сумма, а не знак. Допы и daily-списания
    переиспользуют ``SUBSCRIPTION_PAYMENT`` и отличаются только описанием,
    их исключаем: daily-покупка повторяется каждый день и не является
    «первой покупкой» реферала. NULL-описание отрицанием не отсекается: у
    настоящих оплат его может не быть, а допы и daily всегда подписаны.
    """
    return exists(
        select(Transaction.id).where(
            Transaction.user_id == user_id_column,
            Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            Transaction.is_completed.is_(True),
            Transaction.amount_kopeks != 0,
            or_(
                Transaction.description.is_(None),
                and_(
                    ~addon_description_clause(Transaction.description),
                    ~daily_charge_clause(Transaction.description),
                ),
            ),
        )
    )


def has_paid_subscription_condition():
    """SQL-условие «пользователь уже платил за подписку».

    Признак оплаты — только завершённая транзакция ``SUBSCRIPTION_PAYMENT``
    (не доп и не daily-списание). Daily-оплата переиспользует этот тип, но
    повторяется каждый день и покупкой для маркера первой покупки не является:
    иначе первый же день суточного тарифа сжёг бы бонус.
    Флаг ``has_had_paid_subscription`` намеренно не используется:
    его выставляют купон/промокод, админская конверсия trial→paid и удаление
    любой нетриальной подписки, поэтому он не доказывает оплату. Сама
    подписка оплатой тоже не считается: подарок и админская выдача
    нетриальны, и маркер на них лишил бы человека награды за настоящую
    первую покупку.

    Legacy-оговорка: пользователи, оплатившие до появления транзакционной
    истории (или через путь, не писавший ``SUBSCRIPTION_PAYMENT``), останутся
    без маркера. Это принятое ограничение: ложный маркер отнимает награду
    за реальную первую покупку, поэтому неоплаченный флаг оплатой не считаем.
    """
    return _real_subscription_payment_exists(User.id)


async def has_first_purchase_marker(db: AsyncSession, referee_id: int) -> bool:
    """Есть ли у приглашённого маркер уже учтённой первой покупки."""
    result = await db.execute(
        select(ReferralEarning.id)
        .where(
            ReferralEarning.user_id == referee_id,
            ReferralEarning.reason == REASON_FIRST_PURCHASE_REGISTERED,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def has_paid_subscription_before_attach(db: AsyncSession, user_id: int) -> bool:
    """Платил ли пользователь за подписку к моменту поздней привязки.

    Единый предикат «оплата уже была»: им пользуется и запись маркера, и выбор
    текста уведомлений при регистрации реферала, чтобы обе стороны не
    разошлись в том, считается ли покупка уже учтённой. Оплатой считается
    только завершённая транзакция ``SUBSCRIPTION_PAYMENT`` без признаков допа
    или daily-списания; флаг и сама подписка оплатой не считаются (флаг
    выставляют купон, админская конверсия и удаление подписки).
    """
    paid_exists = await db.execute(
        select(User.id)
        .where(
            User.id == user_id,
            has_paid_subscription_condition(),
        )
        .limit(1)
    )
    return paid_exists.scalar_one_or_none() is not None


async def ensure_marker_for_already_paid_referral(db: AsyncSession, user_id: int, *, lock_nowait: bool = False) -> bool:
    """Записать маркер рефералу, который уже оплатил подписку до привязки.

    Вызывается после поздней привязки пригласившего: покупка к этому моменту
    могла состояться, и награда за неё уже не придёт, а маркер не даст выдать её
    повторно. Подарок, купон и админская выдача оплатой не считаются — иначе
    настоящая первая покупка осталась бы без награды.

    ``lock_nowait`` берёт ``FOR UPDATE NOWAIT``: ретрай из отдельной сессии не
    должен ждать строку, которую caller уже держит в своей незакоммиченной
    транзакции. Занятый лок — не ошибка: возвращается ``False``, маркер
    допишется позже. Обычные (блокирующие) вызовы ошибки БД не глотают.

    Возвращает ``True``, только если строка действительно создана.
    """
    # Маркер — часть схемы «только дни»: в legacy/levels награда за первую
    # покупку не начисляется днями, и маркер там не нужен. Запись маркера в
    # чужой схеме ещё и испортила бы счётчик оплативших при переключении схемы.
    if not settings.is_referral_days_only_scheme():
        return False

    # populate_existing обязателен: админские привязки пишут referred_by_id
    # массовым UPDATE, и в identity map сессии может лежать старое значение.
    # Маркер должен видеть привязку, уже зафиксированную в БД.
    #
    # FOR UPDATE сериализует проверку и вставку между транзакциями: без него
    # две сессии могли бы одновременно увидеть маркер отсутствующим. Лок —
    # строка пользователя: его берут регистрация, админская привязка и
    # диагностика, а бэкфилл — кандидатным запросом
    # ``backfill_first_purchase_markers`` (тот же ряд ``users``, только
    # пачкой). Остаточную гонку, если лок не взят (``nowait``), ловит
    # уникальный индекс ниже.
    try:
        user_result = await db.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update(nowait=lock_nowait)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as error:
        if not lock_nowait:
            raise
        # Глотаем РОВНО lock_not_available (SQLSTATE 55P03): asyncpg отдаёт его
        # и как OperationalError, и как общий DBAPIError. Любой другой код —
        # deadlock 40P01, statement_timeout 57014, обрыв соединения или None —
        # настоящий сбой БД и обязан дойти до вызывающего.
        pgcode = getattr(error.orig, 'pgcode', None) or getattr(error.orig, 'sqlstate', None)
        if pgcode != '55P03':
            raise
        # Строку держит caller (кампания/кабинет) в незакоммиченной
        # транзакции. Ждать её из второй сессии нельзя, а маркер — не
        # обязательство этого вызова: следующий ретрай допишет его.
        await db.rollback()
        # Стабильное имя события для подсчёта в логах: пропуск маркера из-за
        # чужого незакоммиченного лока — не ошибка, маркер допишет ретрай.
        logger.info(
            'Referral first-purchase marker skipped: user row is locked',
            user_id=user_id,
        )
        return False
    user = user_result.scalar_one_or_none()
    if user is None or user.referred_by_id is None:
        return False
    # referred_by_id в БД — целое число. Всё иное (например, неполный объект)
    # привязкой не является и записывать маркер не даёт.
    if not isinstance(user.referred_by_id, int):
        return False
    if await has_first_purchase_marker(db, user_id):
        return False

    if not await has_paid_subscription_before_attach(db, user_id):
        return False

    try:
        db.add(
            ReferralEarning(
                user_id=user_id,
                referral_id=user.referred_by_id,
                amount_kopeks=0,
                reason=REASON_FIRST_PURCHASE_REGISTERED,
                reward_type=ReferralRewardType.DAYS.value,
                level=1,
                days_granted=0,
            )
        )
        await db.commit()
    except IntegrityError:
        # Уникальный индекс — структурная защита от гонки: параллельная запись
        # могла успеть вставить маркер между SELECT и INSERT. Для вызывающего
        # это не ошибка, а «маркер уже есть»; rollback возвращает сессию в
        # рабочее состояние. Стабильное имя события: по нему считают, как часто
        # маркер проигрывает гонку.
        logger.debug(
            'Referral first-purchase marker insert raced, marker already exists',
            user_id=user_id,
        )
        await db.rollback()
        return False
    return True


async def ensure_marker_for_already_paid_referral_in_new_session(user_id: int, *, lock_nowait: bool = False) -> bool:
    """Записать маркер в отдельной сессии, не трогая request-сессию caller'а.

    Нужна админским поздним привязкам: ``ensure_marker_for_already_paid_referral``
    внутри коммитит, а commit в общей сессии экспайрит объекты, которые caller
    читает после вызова (``db_user.language``), что даёт MissingGreenlet.
    Отдельная сессия изолирует и commit, и возможный сбой; ошибки здесь не
    глотаются — caller логирует их сам.

    Вне схемы ``days_only`` сессия не открывается вовсе: маркер там не пишется,
    и лишнее подключение к БД на горячем пути привязки ни к чему.
    """
    if not settings.is_referral_days_only_scheme():
        return False

    from app.database.database import AsyncSessionLocal

    async with AsyncSessionLocal() as marker_db:
        return await ensure_marker_for_already_paid_referral(marker_db, user_id, lock_nowait=lock_nowait)


async def ensure_markers_for_already_paid_referrals_in_new_session(user_ids: list[int]) -> int:
    """Проставить маркеры пачке рефералов в ОДНОЙ отдельной сессии.

    Админская привязка на длинном списке открывала отдельное подключение на
    каждого непомеченного реферала. Здесь одна сессия и коммит на каждого
    пользователя: коммит заодно снимает ``FOR UPDATE``, который helper берёт
    по строке users и не снимает на ранних возвратах (маркер уже есть / ещё не
    платил). Ошибка одного id логируется и не мешает остальным — ретрай
    допишет его позже.

    Возвращает число реально созданных маркеров.
    """
    if not settings.is_referral_days_only_scheme():
        return 0

    unique_ids = list(dict.fromkeys(user_ids))
    if not unique_ids:
        return 0

    from app.database.database import AsyncSessionLocal

    created = 0
    async with AsyncSessionLocal() as marker_db:
        for user_id in unique_ids:
            try:
                if await ensure_marker_for_already_paid_referral(marker_db, user_id):
                    created += 1
                await marker_db.commit()
            except Exception as error:
                logger.error(
                    'Failed to register first-purchase marker in batch',
                    user_id=user_id,
                    error=str(error),
                )
                try:
                    await marker_db.rollback()
                except Exception:
                    pass
    return created


async def backfill_first_purchase_markers(db: AsyncSession) -> int:
    """Проставить маркеры всем рефералам, которые уже покупали подписку.

    Бэкфилл нужен при первом включении схемы: без него покупки, сделанные до
    обновления, выглядели бы как первые и принесли бы награду повторно.
    Кандидаты — те же, кого считает ``has_paid_subscription_condition``:
    завершённая транзакция ``SUBSCRIPTION_PAYMENT`` без признаков допа.
    Подарки и купоны без оплаты пропускаются — маркер на них лишил бы
    пользователя настоящей награды. Legacy-оговорка: оплатившие до появления
    транзакционной истории без такой транзакции не помечаются — флаг
    ``has_had_paid_subscription`` оплатой не считается. Идемпотентен — уже
    помеченные строки пропускаются. Возвращает число созданных маркеров.

    Маркер принадлежит только схеме ``days_only``: на legacy/levels он не нужен
    и исказил бы счётчик оплативших, поэтому вне неё функция не делает запросов
    и возвращает 0.

    Безопасность на живой базе: кандидаты читаются под ``FOR UPDATE`` на строке
    пользователя — это тот же лок, что берут регистрация и
    ``ensure_marker_for_already_paid_referral``, поэтому бэкфилл сериализуется
    с живыми покупками. Каждая вставка идёт в собственном SAVEPOINT: маркер,
    появившийся после bulk-проверки (живая покупка или второй бэкфилл),
    ловится ``IntegrityError`` и не отменяет остальные строки. Отличить живую
    первую покупку от исторической бэкфилл всё равно не может, поэтому
    ``--apply`` следует запускать в окно, когда покупки не совершаются.
    """
    if not settings.is_referral_days_only_scheme():
        logger.warning('Referral first-purchase backfill skipped: scheme is not days_only')
        return 0

    paid_referrals = (
        await db.execute(
            select(User.id, User.referred_by_id)
            .where(
                User.referred_by_id.isnot(None),
                has_paid_subscription_condition(),
            )
            .with_for_update()
        )
    ).all()

    candidate_ids = [
        user_id for user_id, referrer_id in paid_referrals if referrer_id is not None and referrer_id != user_id
    ]
    marked_user_ids: set[int] = set()
    if candidate_ids:
        marked_user_ids = set(
            (
                await db.execute(
                    select(ReferralEarning.user_id).where(
                        ReferralEarning.user_id.in_(candidate_ids),
                        ReferralEarning.reason == REASON_FIRST_PURCHASE_REGISTERED,
                    )
                )
            )
            .scalars()
            .all()
        )

    created = 0
    for user_id, referrer_id in paid_referrals:
        if referrer_id is None or referrer_id == user_id or user_id in marked_user_ids:
            continue
        try:
            # SAVEPOINT на кандидата: конфликт уникального индекса откатывает
            # только эту вставку, не снимая уже добавленные строки и не
            # прерывая внешнюю транзакцию.
            async with db.begin_nested():
                db.add(
                    ReferralEarning(
                        user_id=user_id,
                        referral_id=referrer_id,
                        amount_kopeks=0,
                        reason=REASON_FIRST_PURCHASE_REGISTERED,
                        reward_type=ReferralRewardType.DAYS.value,
                        level=1,
                        days_granted=0,
                    )
                )
        except IntegrityError:
            logger.warning(
                'Referral first-purchase marker backfill conflict, candidate skipped',
                user_id=user_id,
                referral_id=referrer_id,
            )
            continue
        created += 1

    await db.commit()
    if created:
        logger.info('Referral first-purchase markers backfilled', count=created)
    return created


# ---------------------------------------------------------------------------
# Награда за первую оплаченную покупку приглашённого (схема 'days_only').
#
# Награда за первую покупку — не уровни и не проценты: обе стороны получают
# фиксированные дни. Факт учёта покупки фиксируется отдельным маркером, и
# маркер пишется на ЛЮБУЮ первую оплаченную покупку, даже если её срок меньше
# бонусного порога. Иначе вторая, длинная покупка считалась бы первой и
# принесла бы награду, которую программа обещает только за самую первую.
# ---------------------------------------------------------------------------

FirstPurchaseStatus = Literal[
    'skipped',
    'not_referred',
    'already_registered',
    'registered',
    'rewarded',
    'error',
    'daily_not_counted',
]


@dataclass(frozen=True, slots=True)
class FirstPurchaseResult:
    """Итог учёта первой покупки. Нули означают «ничего не выдано»."""

    status: FirstPurchaseStatus
    referee_days: int = 0
    referrer_days: int = 0
    referee_id: int | None = None
    referrer_id: int | None = None
    referee_subscription_id: int | None = None
    referrer_subscription_id: int | None = None


async def _lock_user(db: AsyncSession, user_id: int) -> User | None:
    """Прочитать пользователя под блокировкой строки."""
    result = await db.execute(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _lock_subscription(db: AsyncSession, subscription_id: int, user_id: int) -> Subscription | None:
    """Прочитать подписку под блокировкой, только если она принадлежит пользователю.

    ``tariff`` загружается сразу: от ``is_daily`` зависит, можно ли класть в эту
    подписку бонусные дни. Ленивая подгрузка в async-сессии упала бы
    MissingGreenlet уже внутри проверки.
    """
    result = await db.execute(
        select(Subscription)
        .where(Subscription.id == subscription_id, Subscription.user_id == user_id)
        .with_for_update()
        .options(selectinload(Subscription.tariff))
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _lock_referrer_target(db: AsyncSession, referrer_id: int) -> Subscription | None:
    """Подписка пригласившего, в которую лягут дни.

    Порядок выбора: живая платная (ACTIVE/LIMITED, не триал), затем живой триал,
    затем истёкшая платная, затем истёкший триал; внутри класса — более поздний
    срок, затем меньший id. Живая подписка впереди истёкшей: дни к ней не
    сдвигают часовой пояс доступа, а ``add_reward_days`` истёкшую поднимает.
    Истёкшая подписка тоже годится, кроме заготовки СБП: у неё
    ``start_date == end_date``, она ни разу не оплачена, и реактивация выдала бы
    пригласившему живые дни бесплатно.

    Суточные тарифы исключаются: ``add_reward_days`` продлит ``end_date``, но
    ежедневное списание продолжит идти, и обещанные дни пригласивший всё равно
    оплатит из баланса. Награда днями имеет смысл только для подписок с
    оплаченным сроком.

    ``None`` — подписки, куда можно положить награду, нет вовсе. Это
    единственный разрешённый случай «пригласивший без подписки»: пропускается
    только его сторона, приглашённый свою награду получает.
    """
    # Один SELECT вместо «список id + FOR UPDATE на каждую подписку»: у
    # пригласившего с несколькими подписками N+1 бил по горячему пути награды.
    locked = (
        (
            await db.execute(
                select(Subscription)
                .where(Subscription.user_id == referrer_id)
                .order_by(Subscription.id.asc())
                .with_for_update()
                .options(selectinload(Subscription.tariff))
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )

    def _priority(sub: Subscription) -> int:
        """Класс подписки в порядке дизайна: живая платная → живой триал →
        истёкшая платная → истёкший триал."""
        status = (sub.status or '').lower()
        trial = bool(sub.is_trial) or status == SubscriptionStatus.TRIAL.value
        if status == SubscriptionStatus.EXPIRED.value:
            return 3 if trial else 2
        return 1 if trial else 0

    # Тот же предикат, что и у экрана партнёрки: подходящий статус, не суточный
    # тариф, EXPIRED-заготовка не считается подпиской. Один источник правды —
    # UI обещает ровно то, что реально умеет начисление.
    candidates = [sub for sub in locked if is_referral_reward_target(sub)]
    if not candidates:
        return None
    candidates.sort(
        key=lambda sub: (
            _priority(sub),
            -(sub.end_date.timestamp() if sub.end_date else 0),
            sub.id,
        )
    )
    return candidates[0]


async def register_first_purchase(
    db: AsyncSession,
    *,
    referee_id: int,
    subscription_id: int,
    duration_days: int,
    paid_amount_kopeks: int,
    source: str,
) -> FirstPurchaseResult:
    """Учесть первую оплаченную покупку приглашённого и выдать дни.

    Вызывается уже после успешной оплаты. Маркер, награда приглашённому и
    награда пригласившему пишутся в ОДНОЙ транзакции с единственным коммитом:
    половина записи опаснее отказа. Маркер без дней навсегда лишает человека
    награды, дни без маркера позволяют получить её повторно.

    Суточный тариф — не покупка для маркера: первый день, ежедневное списание
    и возобновление повторяются каждый день и не должны ни ставить маркер, ни
    сжигать бонус. Право на награду остаётся у следующей не-daily покупки.
    EXPIRED-заготовка и черновик покупкой не считаются и маркера не получают.
    """
    if paid_amount_kopeks <= 0:
        # Бесплатная выдача (промокод, подарок, админская подписка) покупкой не
        # считается и маркер не получает: настоящая первая оплата ещё впереди.
        return FirstPurchaseResult(status='skipped')
    if not settings.is_referral_program_enabled() or not settings.is_referral_days_only_scheme():
        return FirstPurchaseResult(status='skipped')

    # Daily-проверка ДО блокировок: ежедневное списание повторяется каждый день,
    # и держать FOR UPDATE на users/подписке до конца запроса ради немедленного
    # ``daily_not_counted`` незачем. Читаем без блокировки; если тариф успеют
    # сменить, ниже остаётся прежняя проверка под локом — решение не ухудшится.
    early_sub_result = await db.execute(
        select(Subscription)
        .where(Subscription.id == subscription_id, Subscription.user_id == referee_id)
        .options(selectinload(Subscription.tariff))
        .execution_options(populate_existing=True)
    )
    early_sub = early_sub_result.scalar_one_or_none()
    if early_sub is not None and is_daily_referral_tariff(early_sub):
        early_referrer_id = (
            await db.execute(select(User.referred_by_id).where(User.id == referee_id))
        ).scalar_one_or_none()
        # Нереферённого пользователя оставляем обычному пути: его статус —
        # ``not_referred``, а не daily-пропуск.
        if early_referrer_id is not None and early_referrer_id != referee_id:
            logger.debug(
                'Referral first purchase not counted: daily tariff',
                referee_id=referee_id,
                referrer_id=early_referrer_id,
                source=source,
            )
            return FirstPurchaseResult(
                status='daily_not_counted',
                referee_id=referee_id,
                referrer_id=early_referrer_id,
            )

    referee = await _lock_user(db, referee_id)
    if referee is None or referee.referred_by_id is None:
        return FirstPurchaseResult(status='not_referred')

    referrer_id = referee.referred_by_id
    if referrer_id == referee_id:
        return FirstPurchaseResult(status='not_referred')

    # Обе стороны пары блокируются до проверки маркера: иначе параллельные
    # покупки одного реферала обе увидели бы маркер отсутствующим и выдали бы
    # награду дважды.
    for user_id in sorted({referee_id, referrer_id}):
        if await _lock_user(db, user_id) is None:
            return FirstPurchaseResult(status='skipped')

    if await has_first_purchase_marker(db, referee_id):
        return FirstPurchaseResult(
            status='already_registered',
            referee_id=referee_id,
            referrer_id=referrer_id,
        )

    referee_sub = await _lock_subscription(db, subscription_id, referee_id)
    if referee_sub is None:
        return FirstPurchaseResult(status='skipped')

    referee_sub_status = str(referee_sub.status or '').strip().lower()
    if referee_sub_status in {SubscriptionStatus.PENDING.value, SubscriptionStatus.DISABLED.value}:
        # Черновик оформления или отключённая подписка: ни дней, ни маркера.
        return FirstPurchaseResult(status='skipped')

    if is_daily_referral_tariff(referee_sub):
        # Daily-покупка (первый день, ежедневное списание, возобновление) не
        # ставит маркер и не расходует бонус: она повторяется каждый день и
        # сожгла бы награду за настоящую первую покупку. Решение не зависит от
        # порядка событий: daily-платёж теперь не считается «уже платил» и в
        # поздней привязке. Debug, а не info: daily-списания частые, и уровень
        # info превратил бы штатный пропуск в шум. Событие стабильное —
        # `Referral first purchase not counted: daily tariff`.
        logger.debug(
            'Referral first purchase not counted: daily tariff',
            referee_id=referee_id,
            referrer_id=referrer_id,
            source=source,
        )
        return FirstPurchaseResult(
            status='daily_not_counted',
            referee_id=referee_id,
            referrer_id=referrer_id,
        )

    # Прочие неподходящие цели (EXPIRED-заготовка и т.п.) покупкой не считаются.
    if not is_referral_reward_target(referee_sub):
        return FirstPurchaseResult(status='skipped')

    # Бонусы сторон независимы: 0 у одной стороны не отменяет другую.
    referee_bonus = settings.get_referral_first_purchase_referee_days()
    referrer_bonus = settings.get_referral_first_purchase_referrer_days()
    extended_referee: Subscription | None = None
    extended_referrer: Subscription | None = None
    referee_days = 0
    referrer_days = 0
    referee_subscription_id: int | None = None
    referrer_subscription_id: int | None = None

    try:
        db.add(
            ReferralEarning(
                user_id=referee_id,
                referral_id=referrer_id,
                amount_kopeks=0,
                reason=REASON_FIRST_PURCHASE_REGISTERED,
                reward_type=ReferralRewardType.DAYS.value,
                level=1,
                days_granted=0,
            )
        )

        if duration_days < settings.REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS:
            # Короткая покупка получает маркер, но не оплачивается днями.
            # id снимается до коммита: после него объект может быть протухшим,
            # и обращение к полю в async-сессии — MissingGreenlet.
            referee_subscription_id = referee_sub.id
            await db.commit()
            logger.info(
                'Referral first purchase registered below bonus period',
                referee_id=referee_id,
                referrer_id=referrer_id,
                duration_days=duration_days,
                source=source,
            )
            return FirstPurchaseResult(
                status='registered',
                referee_id=referee_id,
                referrer_id=referrer_id,
                referee_subscription_id=referee_subscription_id,
            )

        if referee_bonus <= 0 and referrer_bonus <= 0:
            # Бонус выключен: дни никому не выдаются, но маркер всё равно
            # фиксирует первую покупку. Иначе каждая следующая покупка снова
            # считалась бы первой, и включение бонуса задним числом наградило
            # бы уже оплаченные покупки.
            referee_subscription_id = referee_sub.id
            await db.commit()
            logger.info(
                'Referral first purchase registered without bonus days',
                referee_id=referee_id,
                referrer_id=referrer_id,
                duration_days=duration_days,
                source=source,
            )
            return FirstPurchaseResult(
                status='registered',
                referee_id=referee_id,
                referrer_id=referrer_id,
                referee_subscription_id=referee_subscription_id,
            )

        if referee_bonus > 0:
            extended_referee = await add_reward_days(db, referee_sub, referee_bonus)
            if extended_referee is None:
                # Дни не легли никуда — откатываем и маркер: следующая покупка
                # должна иметь возможность принести награду.
                await db.rollback()
                return FirstPurchaseResult(status='skipped')

            referee_days = referee_bonus
            db.add(
                ReferralEarning(
                    user_id=referee_id,
                    referral_id=referrer_id,
                    amount_kopeks=0,
                    reason=REASON_FIRST_PURCHASE_REFEREE,
                    reward_type=ReferralRewardType.DAYS.value,
                    level=1,
                    days_granted=referee_bonus,
                    tariff_id=extended_referee.tariff_id,
                )
            )
            referee_subscription_id = extended_referee.id

        if referrer_bonus > 0:
            referrer_sub = await _lock_referrer_target(db, referrer_id)
            if referrer_sub is not None:
                extended_referrer = await add_reward_days(db, referrer_sub, referrer_bonus)
                if extended_referrer is not None:
                    referrer_days = referrer_bonus
                    db.add(
                        ReferralEarning(
                            user_id=referrer_id,
                            referral_id=referee_id,
                            amount_kopeks=0,
                            reason=REASON_FIRST_PURCHASE,
                            reward_type=ReferralRewardType.DAYS.value,
                            level=1,
                            days_granted=referrer_bonus,
                            tariff_id=extended_referrer.tariff_id,
                        )
                    )
                    referrer_subscription_id = extended_referrer.id

        # Все id сняты до коммита, пока объекты ещё не протухли.
        await db.commit()
    except SQLAlchemyError as error:
        # ``add_reward_days`` поднимает истёкшую подписку в ACTIVE. В тарифном
        # режиме рядом может оказаться живая подписка того же тарифа, и
        # частичный уникальный индекс отвергнет UPDATE. Откат обязателен: без
        # него в базе остался бы маркер без наград, и первая покупка потеряла бы
        # право на дни навсегда.
        await db.rollback()
        logger.error(
            'Referral first purchase failed, transaction rolled back',
            referee_id=referee_id,
            referrer_id=referrer_id,
            subscription_id=subscription_id,
            duration_days=duration_days,
            source=source,
            error=str(error),
            exc_info=True,
        )
        return FirstPurchaseResult(
            status='error',
            referee_id=referee_id,
            referrer_id=referrer_id,
        )

    status: FirstPurchaseStatus = 'rewarded' if referee_days > 0 or referrer_days > 0 else 'registered'
    logger.info(
        'Referral first purchase rewarded' if status == 'rewarded' else 'Referral first purchase registered',
        referee_id=referee_id,
        referrer_id=referrer_id,
        duration_days=duration_days,
        referee_days=referee_days,
        referrer_days=referrer_days,
        source=source,
    )
    return FirstPurchaseResult(
        status=status,
        referee_days=referee_days,
        referrer_days=referrer_days,
        referee_id=referee_id,
        referrer_id=referrer_id,
        referee_subscription_id=referee_subscription_id,
        referrer_subscription_id=referrer_subscription_id,
    )


async def register_first_purchase_after_purchase(
    *,
    referee_id: int,
    subscription_id: int,
    duration_days: int,
    paid_amount_kopeks: int,
    source: str,
    bot=None,
) -> FirstPurchaseResult:
    """Учесть покупку в отдельной сессии и выполнить пост-коммит эффекты.

    Отдельная сессия нужна потому, что вызывающий уже закоммитил покупку в
    своей, и награда не должна ни зависеть от её состояния, ни откатывать её.
    Исключения здесь только логируются: сбой реферальной награды не должен
    превращать успешную оплату в ошибку для пользователя.
    """
    from app.database.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            result = await register_first_purchase(
                db,
                referee_id=referee_id,
                subscription_id=subscription_id,
                duration_days=duration_days,
                paid_amount_kopeks=paid_amount_kopeks,
                source=source,
            )
    except Exception as error:
        logger.error(
            'Failed to process referral first purchase',
            referee_id=referee_id,
            subscription_id=subscription_id,
            source=source,
            error=str(error),
            exc_info=True,
        )
        return FirstPurchaseResult(status='error')

    if result.status == 'rewarded':
        # Побочные эффекты — уже после коммита награды. Их сбой (панель, Lava,
        # телеграм) не имеет права превратить успешную покупку в ошибку.
        try:
            await _post_commit_effects(result, referee_id, bot=bot)
        except Exception as error:
            logger.error(
                'Failed to run referral first purchase post-commit effects',
                referee_id=referee_id,
                subscription_id=subscription_id,
                source=source,
                error=str(error),
                exc_info=True,
            )
    return result


async def _post_commit_effects(
    result: FirstPurchaseResult,
    referee_id: int,
    *,
    bot=None,
) -> None:
    """Побочные эффекты после коммита награды: панель, Lava, уведомления.

    Вызывается только при ``status == 'rewarded'``. Маркер короткой покупки
    (``registered``) дни не выдавал, поэтому не должен ни синхронизировать
    подписки, ни сдвигать расписание списания Lava — иначе короткая покупка
    молча сдвинула бы пользователю платёж, который никто не продлевал.
    """
    from app.database.database import AsyncSessionLocal
    from app.database.models import Subscription
    from app.services.payment.lava import shift_lava_next_charge_after_manual_extension
    from app.services.remnawave_retry_queue import remnawave_retry_queue
    from app.services.subscription_service import SubscriptionService

    # Фактические дни каждой подписки: стороны настраиваются раздельно, и Lava
    # нужно сдвигать ровно на выданное этой подписке количество, а не на общий
    # бонус. Подписки без id в карту не попадают: начислять было некуда.
    rewarded_days: dict[int, int] = {}
    if result.referee_subscription_id is not None:
        rewarded_days[result.referee_subscription_id] = result.referee_days
    if result.referrer_subscription_id is not None:
        rewarded_days[result.referrer_subscription_id] = result.referrer_days

    for subscription_id in sorted(rewarded_days):
        async with AsyncSessionLocal() as db:
            subscription = await db.get(Subscription, subscription_id)
            if subscription is None:
                continue
            # Снимок до sync: при ошибке панели sync откатывает транзакцию и
            # экспайрит объект. Чтение subscription.user_id после этого в
            # async-сессии даёт MissingGreenlet, и retry не встанет.
            subscription_user_id = subscription.user_id
            try:
                synced = await SubscriptionService().sync_remnawave_user(db, subscription)
            except Exception as error:
                logger.error(
                    'Failed to sync referral reward subscription',
                    subscription_id=subscription_id,
                    error=str(error),
                )
                remnawave_retry_queue.enqueue(
                    subscription_id=subscription_id,
                    user_id=subscription_user_id,
                    action='update',
                )
            else:
                if synced is None:
                    logger.warning(
                        'Referral reward subscription sync returned no result',
                        subscription_id=subscription_id,
                    )
                    remnawave_retry_queue.enqueue(
                        subscription_id=subscription_id,
                        user_id=subscription_user_id,
                        action='update',
                    )
            try:
                await shift_lava_next_charge_after_manual_extension(
                    db,
                    subscription_id,
                    rewarded_days[subscription_id],
                )
            except Exception as error:
                logger.warning(
                    'Failed to shift Lava charge after referral reward',
                    subscription_id=subscription_id,
                    error=str(error),
                )

    if bot is None:
        return

    # Синхронизация и Lava выше уже выполнены: отключённые уведомления глушат
    # только сообщения, но не побочные эффекты награды. Порядок проверок тот же,
    # что в referral_service: сначала общий выключатель, затем реферальный.
    if not settings.is_notifications_enabled():
        logger.debug(
            'Referral first purchase notification suppressed: user notifications disabled',
            referee_id=referee_id,
        )
        return
    if not settings.is_referral_notifications_enabled():
        logger.debug(
            'Referral first purchase notification suppressed: referral notifications disabled',
            referee_id=referee_id,
        )
        return

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.database.crud.user import get_user_by_id
    from app.localization.texts import get_texts
    from app.utils.pricing_utils import _pluralize_days_ru

    async with AsyncSessionLocal() as db:
        referee = await get_user_by_id(db, referee_id)
        if referee is not None and referee.telegram_id:
            texts = get_texts(referee.language)
            if result.referee_days:
                referee_days_text = f'{result.referee_days} {_pluralize_days_ru(result.referee_days)}'
                try:
                    await bot.send_message(
                        chat_id=referee.telegram_id,
                        text=(
                            '<tg-emoji emoji-id="5235695112419303615">🎁</tg-emoji> '
                            f'<b>Вам +{referee_days_text}</b>\n\n'
                            'За первую покупку по приглашению.'
                        ),
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text=texts.t('MY_SUBSCRIPTION_BUTTON', 'Моя подписка'),
                                        callback_data='menu_subscription',
                                        icon_custom_emoji_id='5314310000531766389',
                                        style='primary',
                                    )
                                ]
                            ]
                        ),
                    )
                except Exception as error:
                    logger.warning('Failed to notify referee about referral days', error=str(error))

        if result.referrer_days and result.referrer_id:
            referrer = await get_user_by_id(db, result.referrer_id)
            if referrer and referrer.telegram_id:
                texts = get_texts(referrer.language)
                referrer_days_text = f'{result.referrer_days} {_pluralize_days_ru(result.referrer_days)}'
                try:
                    await bot.send_message(
                        chat_id=referrer.telegram_id,
                        text=(
                            '<tg-emoji emoji-id="5235695112419303615">🎉</tg-emoji> '
                            '<b>Друг купил подписку</b>\n\n'
                            f'Вам +{referrer_days_text} к текущей подписке.'
                        ),
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text=texts.t('MAIN_MENU_BUTTON', 'Главное меню'),
                                        callback_data='back_to_menu',
                                        icon_custom_emoji_id='5314310000531766389',
                                        style='primary',
                                    )
                                ]
                            ]
                        ),
                    )
                except Exception as error:
                    logger.warning('Failed to notify referrer about referral days', error=str(error))
