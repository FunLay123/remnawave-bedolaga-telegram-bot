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
2. При смене тарифа в `extend_subscription` (`app/database/crud/subscription.py`)
   рядом с `_drop_orphan_premium_states` вызываем новый
   `sync_premium_state_limits(db, subscription_id, new_tariff)`
   (`app/database/crud/premium_traffic.py`): для сквадов, оставшихся премиумными в
   новом тарифе, `limit_bytes` обновляется сразу. Сквады с `closed_at` не трогаем;
   `extra_bytes` и `used_bytes` сохраняются. Для автоснятого сквада (`is_limited`,
   `closed_at IS NULL`) поднятие лимита разрешает воркеру вернуть его.
3. После успешной синхронизации с панелью с `reset_traffic=True` в
   `SubscriptionService.create_remnawave_user` и `update_remnawave_user` вызываем
   `premium_traffic_service.request_pass(...)`, если у текущего тарифа есть
   премиум-сквады. Точка — после `db.commit()` и после сброса трафика в панели:
   воркер увидит новый `lastTrafficResetAt` и начнёт новый премиум-период.
4. Обновляем `docs/premium-traffic-limits.md` и запись в `../claude-changes.md`.

## Границы

- Не меняем интервал, настройки и рубильник `PREMIUM_TRAFFIC_ENABLED`.
- Не запускаем проход параллельно с циклом и не создаём отдельную задачу на покупку.
- Не трогаем админское закрытие (`closed_at`) и не снимаем его.
- Не меняем экраны, клавиатуры и правила `_resolve_period` для правок тарифа
  администратором — это отдельная тема.
- Триггер срабатывает только при `reset_traffic=True` и только если в тарифе есть
  премиум-сквады.

## Почему так

Полный проход — единственное место, которое знает состояние панели. Будить
существующий цикл дешевле и безопаснее, чем запускать второй `process_once` из
хендлера: несколько покупок подряд схлопываются в один проход, а конкурентных
записей в панель не появляется. Немедленный снимок убирает окно, в котором экран
показывает старый лимит. Триггер после синхронизации с панелью, а не после коммита,
чтобы воркер не проспал сброс `lastTrafficResetAt` и не начал период со старыми
данными.

## Проверка

- `tests/crud/test_premium_traffic_state.py` (или новый): `sync_premium_state_limits`
  поднимает и опускает лимит, сохраняет `extra_bytes`, не трогает `closed_at`,
  поднимает лимит автоснятому скваду.
- `tests/services/test_premium_traffic_service.py`: `request_pass` будит
  `start_monitoring` до истечения интервала; `stop` завершает цикл; повторные
  вызовы схлопываются.
- Тест на триггер: при `reset_traffic=True` и премиум-тарифе вызывается
  `request_pass` с panel id; без премиум-сквадов — нет.
- `pytest tests/crud/test_premium_traffic_state.py
  tests/services/test_premium_traffic_service.py` плюс смежные
  `tests/crud/test_drop_orphan_premium_states.py`,
  `tests/services/test_premium_squads_are_filtered_everywhere.py`.
- Запись в `../claude-changes.md`.
