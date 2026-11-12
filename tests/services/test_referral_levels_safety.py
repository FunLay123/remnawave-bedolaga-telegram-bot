"""Предохранители многоуровневой схемы.

Два класса ошибок, каждый из которых стоил бы денег или доверия:

1. **Правка уровня без сброса кэша.** Админ сохраняет новое правило, экран
   показывает новое значение, а начисления до перезапуска идут по старому.
   Расхождение видимого и работающего — худший вид бага в настройках.

2. **Легаси-доначисление на многоуровневой установке.** Диагностика ищет
   отсутствие строки с легаси-причиной и доплачивает по ключам ``REFERRAL_*``.
   В схеме 'levels' награда могла быть выдана по другому поводу или вовсе днями —
   такую пару детектор считает «пропущенной» и заплатит ВТОРОЙ раз, живыми
   деньгами.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.crud import referral_reward_level as level_crud
from app.services.referral_diagnostics_service import ReferralDiagnosticsService
from app.services.referral_reward_service import ReferralRewardLevelService


class TestCacheInvalidation:
    @pytest.mark.asyncio
    async def test_upsert_drops_cache(self, monkeypatch):
        ReferralRewardLevelService._cache = {}
        assert ReferralRewardLevelService._cache is not None

        saved = SimpleNamespace(level=1)

        async def fake_get(_db, _level):
            return saved

        async def noop_commit():
            return None

        async def noop_refresh(_obj):
            return None

        monkeypatch.setattr(level_crud, 'get_reward_level', fake_get)
        db = SimpleNamespace(commit=noop_commit, refresh=noop_refresh, add=lambda _o: None)

        await level_crud.upsert_reward_level(db, 1, referrer_percent=15)
        assert ReferralRewardLevelService._cache is None

    @pytest.mark.asyncio
    async def test_delete_drops_cache(self, monkeypatch):
        ReferralRewardLevelService._cache = {}
        deleted = SimpleNamespace(level=2)

        async def fake_get(_db, _level):
            return deleted

        async def noop_commit():
            return None

        async def noop_delete(_obj):
            return None

        monkeypatch.setattr(level_crud, 'get_reward_level', fake_get)
        db = SimpleNamespace(commit=noop_commit, delete=noop_delete)

        assert await level_crud.delete_reward_level(db, 2) is True
        assert ReferralRewardLevelService._cache is None


class TestCacheReload:
    """Сброса кэша мало — важно, что следующее чтение вернёт НОВОЕ значение.

    Проверка «_cache стал None» показывает лишь половину: если бы перезагрузка
    была сломана, правило продолжало бы отдаваться старым, а тест на сброс всё
    равно был бы зелёным.
    """

    @pytest.mark.asyncio
    async def test_next_read_returns_the_new_rule(self, monkeypatch):
        from app.database.models import ReferralRewardLevel

        stored = {'percent': 10}
        reads = []

        class _Result:
            def scalars(self):
                return self

            def all(self):
                reads.append(stored['percent'])
                row = ReferralRewardLevel(
                    level=1,
                    is_active=True,
                    reward_mode='money',
                    trigger='every_topup',
                    referrer_percent=stored['percent'],
                    referrer_days=0,
                    referee_days=0,
                    max_payments=0,
                )
                return [row]

        async def fake_execute(_query):
            return _Result()

        db = SimpleNamespace(execute=fake_execute)

        ReferralRewardLevelService.invalidate_cache()
        first = await ReferralRewardLevelService.get_level(db, 1)
        assert first.referrer_percent == 10

        # Повторное чтение обязано идти из кэша, а не в базу.
        await ReferralRewardLevelService.get_level(db, 1)
        assert len(reads) == 1, 'второе чтение должно обслуживаться кэшем'

        stored['percent'] = 25
        ReferralRewardLevelService.invalidate_cache()
        second = await ReferralRewardLevelService.get_level(db, 1)

        assert second.referrer_percent == 25, 'после сброса должно читаться новое правило'
        assert len(reads) == 2

        ReferralRewardLevelService.invalidate_cache()


class TestDiagnosticsGuard:
    @pytest.mark.asyncio
    async def test_detector_refuses_on_levels_scheme(self, monkeypatch):
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')

        async def explode(*_args, **_kwargs):
            raise AssertionError('детектор не должен ходить в базу на многоуровневой схеме')

        db = SimpleNamespace(execute=explode)
        report = await ReferralDiagnosticsService().check_missing_bonuses(db)

        assert report.unsupported_scheme is True
        assert report.missing_bonuses == []

    @pytest.mark.asyncio
    async def test_apply_refuses_stale_report(self, monkeypatch):
        """Отчёт мог быть построен ДО переключения схемы и пролежать в Redis."""
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')

        async def explode(*_args, **_kwargs):
            raise AssertionError('доначисление не должно трогать базу на многоуровневой схеме')

        stale = [SimpleNamespace(referral_id=2, referrer_id=1)]
        db = SimpleNamespace(execute=explode)
        report = await ReferralDiagnosticsService().fix_missing_bonuses(db, stale, apply=True)

        assert report.users_fixed == 0

    @pytest.mark.asyncio
    async def test_detector_refuses_on_days_only_scheme(self, monkeypatch):
        """days_only — не legacy: деньги в этой схеме не начисляются вовсе."""
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')

        async def explode(*_args, **_kwargs):
            raise AssertionError('детектор не должен ходить в базу на схеме days_only')

        db = SimpleNamespace(execute=explode)
        report = await ReferralDiagnosticsService().check_missing_bonuses(db)

        assert report.unsupported_scheme is True
        assert report.missing_bonuses == []

    @pytest.mark.asyncio
    async def test_apply_refuses_stale_report_on_days_only_scheme(self, monkeypatch):
        """Устаревший отчёт из Redis не должен доплачивать на схеме days_only."""
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')

        async def explode(*_args, **_kwargs):
            raise AssertionError('доначисление не должно трогать базу на схеме days_only')

        stale = [SimpleNamespace(referral_id=2, referrer_id=1)]
        db = SimpleNamespace(execute=explode)
        report = await ReferralDiagnosticsService().fix_missing_bonuses(db, stale, apply=True)

        assert report.users_fixed == 0

    @pytest.mark.asyncio
    async def test_lost_referrals_preview_skips_money_on_days_only_scheme(self, monkeypatch):
        """В days_only чинится только привязка: предпросмотр не должен обещать деньги."""
        from datetime import UTC, datetime

        from app.database.models import ReferralEarning, Transaction, User
        from app.services.referral_diagnostics_service import LostReferral

        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
        monkeypatch.setattr(settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 10_000)
        monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 10_000)
        monkeypatch.setattr(settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 10_000)

        user = SimpleNamespace(id=2, telegram_id=1002, username='referee', full_name='Referee', referred_by_id=None)
        referrer = SimpleNamespace(
            id=1, telegram_id=1001, username='referrer', full_name='Referrer', referral_commission_percent=10
        )
        first_topup = SimpleNamespace(amount_kopeks=50_000)
        user_queries = []

        class _Result:
            def __init__(self, rows=None):
                self._rows = rows or []

            def scalars(self):
                return self

            def all(self):
                return self._rows

            def scalar_one_or_none(self):
                return self._rows[0] if self._rows else None

        async def fake_execute(query):
            entity = query.column_descriptions[0].get('entity')
            if entity is User:
                # Первый запрос — рефералы, второй — рефереры.
                user_queries.append(1)
                return _Result([user]) if len(user_queries) == 1 else _Result([referrer])
            if entity is Transaction:
                return _Result([first_topup])
            if entity is ReferralEarning:
                return _Result([])
            raise AssertionError(f'неожиданный запрос: {query}')

        lost = LostReferral(
            telegram_id=1002,
            username='referee',
            full_name='Referee',
            referral_code='refABC',
            expected_referrer_code='refABC',
            expected_referrer_id=1,
            expected_referrer_name='Referrer',
            click_time=datetime.now(UTC),
            registered=True,
            has_referrer=False,
            current_referrer_id=None,
        )

        report = await ReferralDiagnosticsService().fix_lost_referrals(
            SimpleNamespace(execute=fake_execute), [lost], apply=False
        )

        assert report.bonuses_to_referrals == 0, 'в days_only деньги рефералу не начисляются'
        assert report.bonuses_to_referrers == 0, 'в days_only деньги рефереру не начисляются'
        assert report.users_fixed == 1, 'привязка referred_by_id восстанавливается и в days_only'
        assert report.details[0].referred_by_set is True

    @pytest.mark.asyncio
    async def test_lost_referrals_apply_restores_binding_without_paying_on_days_only_scheme(self, monkeypatch):
        """apply в days_only восстанавливает referred_by_id, но не платит."""
        from datetime import UTC, datetime

        from app.database.models import ReferralEarning, Transaction, User
        from app.services import referral_diagnostics_service as diag_module
        from app.services.referral_diagnostics_service import LostReferral

        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
        monkeypatch.setattr(settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 10_000)
        monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 10_000)
        monkeypatch.setattr(settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 10_000)

        paid = []

        async def fake_add_balance(*args, **_kwargs):
            paid.append(args)

        async def fake_create_earning(**kwargs):
            paid.append(kwargs)

        async def noop_commit():
            return None

        monkeypatch.setattr(diag_module, 'add_user_balance', fake_add_balance)
        monkeypatch.setattr(diag_module, 'create_referral_earning', fake_create_earning)

        user = SimpleNamespace(id=2, telegram_id=1002, username='referee', full_name='Referee', referred_by_id=None)
        referrer = SimpleNamespace(
            id=1, telegram_id=1001, username='referrer', full_name='Referrer', referral_commission_percent=10
        )
        first_topup = SimpleNamespace(amount_kopeks=50_000)
        user_queries = []

        class _Result:
            def __init__(self, rows=None):
                self._rows = rows or []

            def scalars(self):
                return self

            def all(self):
                return self._rows

            def scalar_one_or_none(self):
                return self._rows[0] if self._rows else None

        async def fake_execute(query):
            entity = query.column_descriptions[0].get('entity')
            if entity is User:
                user_queries.append(1)
                return _Result([user]) if len(user_queries) == 1 else _Result([referrer])
            if entity is Transaction:
                return _Result([first_topup])
            if entity is ReferralEarning:
                return _Result([])
            raise AssertionError(f'неожиданный запрос: {query}')

        lost = LostReferral(
            telegram_id=1002,
            username='referee',
            full_name='Referee',
            referral_code='refABC',
            expected_referrer_code='refABC',
            expected_referrer_id=1,
            expected_referrer_name='Referrer',
            click_time=datetime.now(UTC),
            registered=True,
            has_referrer=False,
            current_referrer_id=None,
        )

        # Отдельная сессия маркера не должна уходить в реальную БД: подменяем
        # сам helper и проверяем, что он вызван для привязанного пользователя.
        marker_mock = AsyncMock()
        monkeypatch.setattr(
            'app.services.referral_purchase_service.ensure_marker_for_already_paid_referral_in_new_session',
            marker_mock,
        )

        report = await ReferralDiagnosticsService().fix_lost_referrals(
            SimpleNamespace(execute=fake_execute, commit=noop_commit), [lost], apply=True
        )

        assert user.referred_by_id == 1, 'привязка обязана восстановиться'
        assert report.users_fixed == 1
        assert report.bonuses_to_referrals == 0
        assert report.bonuses_to_referrers == 0
        assert paid == [], 'деньги и ledger-строки в days_only не создаются'
        # Маркер первой покупки вызывается для привязанного пользователя.
        marker_mock.assert_awaited_once_with(2)

    @pytest.mark.asyncio
    async def test_apply_skips_pair_with_existing_earning(self, monkeypatch):
        """Повторный/устаревший apply не должен платить по паре второй раз."""
        from app.database.models import ReferralEarning, User
        from app.services import referral_diagnostics_service as diag_module

        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')

        paid = []

        async def fake_add_balance(*args, **_kwargs):
            paid.append(args)

        async def fake_create_earning(**kwargs):
            paid.append(kwargs)

        async def fake_campaign_id(_db, _user_id):
            return None

        async def noop_commit():
            return None

        monkeypatch.setattr(diag_module, 'add_user_balance', fake_add_balance)
        monkeypatch.setattr(diag_module, 'create_referral_earning', fake_create_earning)
        monkeypatch.setattr(diag_module, 'get_user_campaign_id', fake_campaign_id)

        referral = SimpleNamespace(id=2, telegram_id=1002, username='referee', full_name='Referee')
        referrer = SimpleNamespace(id=1, telegram_id=1001, username='referrer', full_name='Referrer')
        existing = SimpleNamespace(user_id=1, referral_id=2, reason='referral_first_topup')

        class _Result:
            def __init__(self, rows=None):
                self._rows = rows or []

            def scalars(self):
                return self

            def all(self):
                return self._rows

            def scalar_one_or_none(self):
                return self._rows[0] if self._rows else None

        async def fake_execute(query):
            entity = query.column_descriptions[0].get('entity')
            if entity is User:
                return _Result([referral, referrer])
            if entity is ReferralEarning:
                return _Result([existing])
            raise AssertionError(f'неожиданный запрос: {query}')

        stale = [
            SimpleNamespace(
                referral_id=2,
                referrer_id=1,
                referral_telegram_id=1002,
                referral_username='referee',
                referral_full_name='Referee',
                referrer_full_name='Referrer',
                first_topup_amount_kopeks=50_000,
                missing_referral_bonus=True,
                missing_referrer_bonus=True,
                referral_bonus_amount=10_000,
                referrer_bonus_amount=10_000,
            )
        ]

        db = SimpleNamespace(execute=fake_execute, commit=noop_commit)
        report = await ReferralDiagnosticsService().fix_missing_bonuses(db, stale, apply=True)

        assert report.users_fixed == 0
        assert report.bonuses_to_referrals == 0
        assert report.bonuses_to_referrers == 0
        assert paid == [], 'повторная выплата по уже оплаченной паре запрещена'

    @pytest.mark.asyncio
    async def test_legacy_apply_still_pays_missing_bonuses(self, monkeypatch):
        """Контроль: новый guard не выключает легаси-доначисление целиком."""
        from app.database.models import ReferralEarning, User
        from app.services import referral_diagnostics_service as diag_module

        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')

        balances = []
        earnings = []

        async def fake_add_balance(_db, user, amount, *_args, **_kwargs):
            balances.append((user.id, amount))

        async def fake_create_earning(**kwargs):
            earnings.append(kwargs)

        async def fake_campaign_id(_db, _user_id):
            return 7

        async def no_contests(_self, *_args, **_kwargs):
            return None

        async def noop_commit():
            return None

        monkeypatch.setattr(diag_module, 'add_user_balance', fake_add_balance)
        monkeypatch.setattr(diag_module, 'create_referral_earning', fake_create_earning)
        monkeypatch.setattr(diag_module, 'get_user_campaign_id', fake_campaign_id)
        monkeypatch.setattr(diag_module.ReferralDiagnosticsService, '_add_to_active_contests', no_contests)

        referral = SimpleNamespace(id=2, telegram_id=1002, username='referee', full_name='Referee')
        referrer = SimpleNamespace(id=1, telegram_id=1001, username='referrer', full_name='Referrer')

        class _Result:
            def __init__(self, rows=None):
                self._rows = rows or []

            def scalars(self):
                return self

            def all(self):
                return self._rows

            def scalar_one_or_none(self):
                return self._rows[0] if self._rows else None

        async def fake_execute(query):
            entity = query.column_descriptions[0].get('entity')
            if entity is User:
                return _Result([referral, referrer])
            if entity is ReferralEarning:
                return _Result([])
            raise AssertionError(f'неожиданный запрос: {query}')

        missing = [
            SimpleNamespace(
                referral_id=2,
                referrer_id=1,
                referral_telegram_id=1002,
                referral_username='referee',
                referral_full_name='Referee',
                referrer_full_name='Referrer',
                first_topup_amount_kopeks=50_000,
                missing_referral_bonus=True,
                missing_referrer_bonus=True,
                referral_bonus_amount=10_000,
                referrer_bonus_amount=10_000,
            )
        ]

        db = SimpleNamespace(execute=fake_execute, commit=noop_commit)
        report = await ReferralDiagnosticsService().fix_missing_bonuses(db, missing, apply=True)

        assert report.users_fixed == 1
        assert report.bonuses_to_referrals == 10_000
        assert report.bonuses_to_referrers == 10_000
        assert {user_id for user_id, _ in balances} == {1, 2}, 'выплата обоим участникам пары'
        assert len(earnings) == 1
        assert earnings[0]['reason'] == 'referral_first_topup'
        assert (earnings[0]['user_id'], earnings[0]['referral_id']) == (1, 2)

    @pytest.mark.asyncio
    async def test_legacy_scheme_still_runs_detector(self, monkeypatch):
        """Гейт не должен выключать диагностику на обычных установках."""
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')
        reached = []

        async def fake_execute(*_args, **_kwargs):
            reached.append(1)

            class _Empty:
                def scalars(self):
                    return self

                def all(self):
                    return []

            return _Empty()

        db = SimpleNamespace(execute=fake_execute)
        report = await ReferralDiagnosticsService().check_missing_bonuses(db)

        assert reached, 'на legacy-схеме детектор обязан работать как раньше'
        assert report.unsupported_scheme is False


class TestApplyMissingBonusesHandler:
    @pytest.mark.asyncio
    async def test_nothing_to_pay_shows_neutral_message(self, monkeypatch):
        """Пустой результат apply не должен выглядеть как успешная выплата."""
        import inspect

        from app.handlers.admin import referrals as admin_referrals
        from app.services import referral_diagnostics_service as diag_module

        async def fake_fix(_db, _bonuses, apply=False):
            assert apply is True
            return diag_module.FixReport()

        monkeypatch.setattr(diag_module.referral_diagnostics_service, 'fix_missing_bonuses', fake_fix)

        class _Message:
            def __init__(self):
                self.texts = []

            async def edit_text(self, text, reply_markup=None):
                self.texts.append(text)

        class _Callback:
            def __init__(self):
                self.message = _Message()
                self.answers = []

            async def answer(self, text=None, show_alert=False):
                self.answers.append((text, show_alert))

        class _State:
            def __init__(self, data):
                self.data = data

            async def get_data(self):
                return dict(self.data)

            async def update_data(self, **kwargs):
                self.data.update(kwargs)

        report_dict = {
            'total_referrals_checked': 1,
            'referrals_with_topup': 1,
            'missing_bonuses': [
                {
                    'referral_id': 2,
                    'referral_telegram_id': 1002,
                    'referrer_id': 1,
                    'referrer_telegram_id': 1001,
                }
            ],
            'total_missing_to_referrals': 10_000,
            'total_missing_to_referrers': 10_000,
            'unsupported_scheme': False,
        }
        callback = _Callback()
        state = _State({'missing_bonuses_report': report_dict})

        raw_handler = inspect.unwrap(admin_referrals.apply_missing_bonuses)
        await raw_handler(callback, SimpleNamespace(id=1), SimpleNamespace(), state)

        text = callback.message.texts[-1]
        assert 'Начислять нечего' in text
        assert 'Бонусы начислены' not in text


class TestBackupCoverage:
    """Правила уровней обязаны попадать в бэкап.

    Флаг схемы живёт в SystemSetting и восстановление переживает. Если правила
    не восстановятся, бот встанет с включённой многоуровневой схемой и пустой
    таблицей уровней: цепочка не найдёт ни одного правила и не заплатит НИЧЕГО —
    без ошибки, без записи в логе, при живой истории начислений в ledger'е.
    """

    def test_reward_levels_are_backed_up(self, tmp_path, monkeypatch):
        monkeypatch.setenv('BACKUP_LOCATION', str(tmp_path))
        from app.database.models import ReferralRewardLevel, Tariff
        from app.services.backup_service import backup_service

        models = backup_service._base_backup_models
        assert ReferralRewardLevel in models, 'referral_reward_levels отсутствует в бэкапе'
        # FK на tariffs: тариф обязан восстановиться раньше правила уровня.
        assert models.index(Tariff) < models.index(ReferralRewardLevel)


class TestMergeChainRepair:
    @pytest.mark.asyncio
    async def test_two_hop_cycle_is_broken(self, monkeypatch):
        """primary → X → primary: проверки self-referral такую петлю не видят."""
        from app.services import account_merge_service as merge

        primary = SimpleNamespace(id=1, referred_by_id=7)
        chain = {7: 1}  # X приглашён primary

        class _Result:
            def __init__(self, value):
                self._value = value

            def scalar_one_or_none(self):
                return self._value

        async def fake_execute(query):
            # Единственный SELECT в обходе — «кто пригласил current_id».
            compiled = str(query.compile(compile_kwargs={'literal_binds': True}))
            uid = int(compiled.rsplit('=', 1)[-1].strip())
            return _Result(chain.get(uid))

        db = SimpleNamespace(execute=fake_execute)
        broken = await merge._break_referral_cycle_through(db, primary)

        assert broken is True
        assert primary.referred_by_id is None

    @pytest.mark.asyncio
    async def test_cycle_above_primary_is_left_alone(self, monkeypatch):
        """Петля B→C→B к слиянию отношения не имеет.

        Снять там привязку primary значит уничтожить его связь с законным
        реферером и при этом оставить настоящую петлю нетронутой.
        """
        from app.services import account_merge_service as merge

        primary = SimpleNamespace(id=1, referred_by_id=2)
        chain = {2: 3, 3: 4, 4: 3}  # 1 → 2 → 3 → 4 → 3: петля целиком выше primary

        class _Result:
            def __init__(self, value):
                self._value = value

            def scalar_one_or_none(self):
                return self._value

        async def fake_execute(query):
            compiled = str(query.compile(compile_kwargs={'literal_binds': True}))
            uid = int(compiled.rsplit('=', 1)[-1].strip())
            return _Result(chain.get(uid))

        db = SimpleNamespace(execute=fake_execute)
        assert await merge._break_referral_cycle_through(db, primary) is False
        assert primary.referred_by_id == 2, 'законная связь primary с его реферером обязана уцелеть'

    @pytest.mark.asyncio
    async def test_healthy_chain_is_left_alone(self, monkeypatch):
        from app.services import account_merge_service as merge

        primary = SimpleNamespace(id=1, referred_by_id=7)
        chain = {7: 8, 8: None}

        class _Result:
            def __init__(self, value):
                self._value = value

            def scalar_one_or_none(self):
                return self._value

        async def fake_execute(query):
            compiled = str(query.compile(compile_kwargs={'literal_binds': True}))
            uid = int(compiled.rsplit('=', 1)[-1].strip())
            return _Result(chain.get(uid))

        db = SimpleNamespace(execute=fake_execute)
        assert await merge._break_referral_cycle_through(db, primary) is False
        assert primary.referred_by_id == 7


class TestAsyncSessionHazards:
    """Ловушки async-сессии, каждая из которых теряет уже выданную награду."""

    @pytest.mark.asyncio
    async def test_tariff_name_never_touches_the_relationship(self):
        """subscription.tariff у только что найденной подписки не загружен.

        Обращение к связи — неявный запрос: в async-сессии это MissingGreenlet,
        и дни оказываются выданы, а строка ledger'а уже не записана.
        """
        import ast
        import inspect

        from app.services.referral_reward_service import grant_reward_days

        tree = ast.parse(inspect.getsource(grant_reward_days).lstrip())
        touches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr == 'tariff'
            and isinstance(node.value, ast.Name)
            and node.value.id == 'subscription'
        ]
        assert not touches, 'название тарифа обязано резолвиться запросом по tariff_id'

        # То же самое, но через getattr — обход проверки выше.
        source = inspect.getsource(grant_reward_days)
        assert "getattr(subscription, 'tariff'" not in source

    @pytest.mark.asyncio
    async def test_recipient_is_reloaded_per_component(self):
        """Неудачный add_user_balance делает rollback, а он истекает ВСЕ объекты сессии.

        Держать полученного до отката пользователя и читать его поля дальше — тот
        же MissingGreenlet, только уже в уведомлении.
        """
        import inspect

        from app.services.referral_reward_service import _grant_one

        source = inspect.getsource(_grant_one)
        assert 'await get_user_by_id(db, component.recipient_id)' in source


class TestCacheGeneration:
    @pytest.mark.asyncio
    async def test_invalidation_during_load_is_not_overwritten(self, monkeypatch):
        """Сброс кэша, случившийся ПОКА идёт чтение, не должен затираться результатом.

        Иначе в кэше навсегда останется снимок, сделанный до правки: админ видит
        новое правило, а начисления идут по старому до перезапуска.
        """
        ReferralRewardLevelService.invalidate_cache()

        class _Result:
            def scalars(self):
                return self

            def all(self):
                # Правка приезжает ровно в момент чтения.
                ReferralRewardLevelService.invalidate_cache()
                return []

        async def fake_execute(_query):
            return _Result()

        db = SimpleNamespace(execute=fake_execute)
        await ReferralRewardLevelService._load(db)

        assert ReferralRewardLevelService._cache is None, 'устаревший снимок не должен осесть в кэше'


class TestFirstTopupClaim:
    @pytest.mark.asyncio
    async def test_concurrent_topups_fire_first_event_once(self, monkeypatch):
        """Два одновременных платежа не должны оба выдать награду за первое пополнение."""
        from app.services import referral_service

        rows_affected = [1, 0]  # первый UPDATE выигрывает, второй затрагивает 0 строк
        events: list[str] = []

        async def fake_execute(query):
            # Через ту же сессию проходит ещё и удаление pending-строки — считать
            # захватом первого пополнения нужно только UPDATE по users.
            statement = str(query).strip().upper()
            if statement.startswith('UPDATE USERS'):
                return SimpleNamespace(rowcount=rows_affected.pop(0))
            return SimpleNamespace(rowcount=0)

        async def fake_award(_db, _user, *, event, topup_amount_kopeks=0, bot=None):
            events.append(event)
            return []

        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        monkeypatch.setattr(settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 100_00)
        monkeypatch.setattr('app.services.referral_reward_service.award_referral_rewards', fake_award)

        async def noop_commit():
            return None

        db = SimpleNamespace(execute=fake_execute, commit=noop_commit)

        for _ in range(2):
            user = SimpleNamespace(
                id=2,
                telegram_id=1002,
                full_name='Реферал',
                language='ru',
                referred_by_id=1,
                has_made_first_topup=False,
            )
            await referral_service._process_topup_levels(db, user, 500_00, None)

        assert events.count('first_topup') == 1, 'первое пополнение засчитывается ровно один раз'
        assert events.count('repeat_topup') == 1
