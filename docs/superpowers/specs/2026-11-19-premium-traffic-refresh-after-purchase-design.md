# Мгновенное обновление белых списков после покупки тарифа

## Зачем

У триала в тарифе лимит белых списков 3 ГБ. После покупки тарифа на 20 ГБ подписка
сразу получает новый тариф, но состояние `subscription_premium_traffic.limit_bytes`
остаётся снимком триального периода. Экран «Информация о подписке»
(`premium_squad_lines`) читает именно этот снимок, а воркер обновляет его только на
ближайшем проходе — до 5 минут. Если триальный лимит был исчерпан, снятый за расход
сквад тоже остаётся недоступным до этого прохода.

## Что делаем

1. `PremiumTrafficService` (`app/services/premium_traffic_service.py`): добавляем
   `_wake_event = asyncio.Event()`. `start_monitoring` после прохода ждёт
   `wait_for(event.wait(), timeout=interval)` вместо `sleep`. Новый
   `request_pass(panel_user_id=None)` сбрасывает кеш карточки панели и будит цикл;
   `stop()` тоже будит, чтобы задача завершилась сразу. Проходы остаются
   последовательными, повторные вызовы схлопываются в один.
2. При смене тарифа вызываем общий `refresh_premium_states_for_tariff` в
   `app/database/crud/subscription.py`: он убирает осиротевшие состояния и
   подтягивает `sync_premium_state_limits(db, subscription_id, new_tariff)`
   (`app/database/crud/premium_traffic.py`). Для сквадов, оставшихся премиумными
   в новом тарифе, `limit_bytes` обновляется сразу. Сквады с `closed_at` в этой
   синхронизации не трогаем; `extra_bytes` и `used_bytes` сохраняются. Для
   автоснятого сквада (`is_limited`, `closed_at IS NULL`) поднятие лимита
   разрешает воркеру вернуть его, опускание оставляет прежний потолок. Общий
   helper вызывают все пути смены тарифа: `extend_subscription`,
   `replace_subscription` (guest-покупка) и кабинетный `tariff_switch`.
3. После успешной синхронизации с панелью с `reset_traffic=True` в
   `SubscriptionService.create_remnawave_user` и `update_remnawave_user` вызываем
   `request_premium_traffic_refresh(...)`, если у текущего тарифа есть
   премиум-сквады. Точка — после `db.commit()` и после сброса трафика в панели:
   воркер увидит новый `lastTrafficResetAt` и начнёт новый премиум-период.
   Guest-доставка зовёт тот же helper после своего коммита, хотя панель там
   трафик не сбрасывает: подписка только что куплена, и снятый за расход сквад
   должен вернуться, а не ждать периода.
4. Обновляем `docs/premium-traffic-limits.md` и запись в `../claude-changes.md`.

## Границы

- Не меняем интервал, настройки и рубильник `PREMIUM_TRAFFIC_ENABLED`.
- Не запускаем проход параллельно с циклом и не создаём отдельную задачу на покупку.
- Не снимаем админское закрытие (`closed_at`) и не возвращаем доступ. В новом
  периоде снимок лимита следует за тарифом, как у всех, но `closed_at` и
  `is_limited` сохраняются.
- Не меняем экраны, клавиатуры и правила `_resolve_period` для правок тарифа
  администратором — это отдельная тема.
- Триггер срабатывает после покупки или синхронизации. У
  `update_remnawave_user` — только при `reset_traffic=True`; guest-доставка
  будит после коммита покупки, даже если панель трафик не сбрасывала. Без
  премиум-сквадов в тарифе проход не нужен.

## Почему так

Полный проход — единственное место, которое знает состояние панели. Будить
существующий цикл дешевле и безопаснее, чем запускать второй `process_once` из
хендлера: несколько покупок подряд схлопываются в один проход, а конкурентных
записей в панель не появляется. Немедленный снимок убирает окно, в котором экран
показывает старый лимит. Триггер после синхронизации с панелью, а не после коммита,
чтобы воркер не проспал сброс `lastTrafficResetAt` и не начал период со старыми
данными.

## Проверка

- `tests/crud/test_premium_traffic_state.py`: `sync_premium_state_limits`
  поднимает и опускает лимит, сохраняет `extra_bytes`, не трогает `closed_at`,
  поднимает лимит автоснятому скваду; новый период у закрытого сквада обновляет
  снимок, но оставляет `closed_at` и блокировку.
- `tests/crud/test_premium_traffic_on_tariff_change.py`: реальные
  `extend_subscription` и `replace_subscription` доводят новый лимит до
  `subscription_premium_traffic`.
- `tests/services/test_premium_traffic_service.py`: `request_pass` будит
  `start_monitoring` до истечения интервала; `stop` завершает цикл; повторные
  вызовы схлопываются.
- `tests/services/test_recreate_deleted_panel_user.py`: `create_remnawave_user`
  и `update_remnawave_user` зовут пробуждение только при `reset_traffic=True`.
- `tests/services/test_subscription_service_premium_refresh.py`: гейты helper'а
  (премиум-тариф, рубильник, отсутствие сброса).
- `pytest` по перечисленным файлам плюс смежные
  `tests/crud/test_drop_orphan_premium_states.py`,
  `tests/services/test_premium_squads_are_filtered_everywhere.py`.
- Запись в `../claude-changes.md`.
