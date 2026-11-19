# Бэкапы в Google Drive

Опциональный сервис `backup` делает дамп PostgreSQL каждые 5 минут, кладёт рядом
копию `.env` и отправляет пары в Google Drive. Сервис живёт отдельно от бота:
падение, рестарт или деплой бота не останавливают бэкапы.

## Что получится

```text
remnawave-bot/
├── README.md
├── 5min/
│   ├── 26_January_2026_22-30.dump
│   ├── 26_January_2026_22-30.env.bak
│   └── ...
├── hourly/
│   ├── 26_January_2026_22-00.dump
│   ├── 26_January_2026_22-00.env.bak
│   └── ...
├── daily/
├── weekly/
└── monthly/
```

Имена файлов — локальное время контейнера (`TZ`, по умолчанию `Europe/Moscow`).
Секунды не используются; если контейнер перезапустился в ту же минуту, к имени
добавляется `_2`. В compose стоит `RCLONE_CONFIG_GCRYPT_SUFFIX=none`, поэтому в
Drive имена ровно `*.dump` / `*.env.bak`, без `.bin`.

| Папка | Частота | Храним | Примерный объём |
|---|---|---|---|
| `5min` | каждые 5 минут | 12 пар (1 час) | ~1.4 МБ |
| `hourly` | раз в час | 24 пары (1 сутки) | ~2.8 МБ |
| `daily` | раз в сутки | 30 пар | ~3.5 МБ |
| `weekly` | раз в неделю | 12 пар | ~1.4 МБ |
| `monthly` | раз в месяц | 12 пар | ~1.4 МБ |

Итого ~90 пар и ~10–11 МБ при текущем размере базы. Старые файлы удаляются
по времени изменения, поэтому человеческие имена не ломают ротацию.

`README.md` в корне папки — открытая инструкция по восстановлению. Остальные
файлы зашифрованы rclone crypt; имена файлов и папок при этом видны.

## Настройка

### 1. OAuth-токен Google Drive

На машине с браузером установи rclone и выполни:

```bash
rclone authorize "drive"
```

Скопируй JSON-токен из вывода. В `.env` добавь:

```dotenv
RCLONE_CONFIG_GDRIVE_TOKEN='{"access_token":"...","token_type":"Bearer","refresh_token":"...","expiry":"..."}'
```

Токен лучше держать в одинарных кавычках, чтобы `.env` не пытался разобрать
внутренние кавычки JSON.

Сохрани этот JSON (или готовый `rclone.conf`) в менеджере паролей: без него на
новом хосте не скачать даже `.env` из бэкапа.

Если используешь собственный OAuth client, переведи его в production mode:
в testing mode refresh-token живёт 7 дней и бэкапы перестанут ходить.

### 2. Пароль шифрования

Придумай пароль и получи его obscured-версию:

```bash
rclone obscure 'твой-пароль'
```

В `.env`:

```dotenv
RCLONE_CONFIG_GCRYPT_PASSWORD=<вывод rclone obscure>
```

Сам пароль (не obscured) сохрани в менеджере паролей. Он не хранится на сервере
в открытом виде и не попадает в бэкапы. Без него дампы не расшифровать.

### 3. Переменные

Скопируй блок из `.env.example`:

```dotenv
COMPOSE_PROFILES=backup

BACKUP_GDRIVE_FOLDER=remnawave-bot
BACKUP_GDRIVE_DUMP_INTERVAL_SECONDS=300
BACKUP_GDRIVE_UPLOAD_INTERVAL_SECONDS=300
BACKUP_GDRIVE_KEEP_5MIN=12
BACKUP_GDRIVE_KEEP_HOURLY=24
BACKUP_GDRIVE_KEEP_DAILY=30
BACKUP_GDRIVE_KEEP_WEEKLY=12
BACKUP_GDRIVE_KEEP_MONTHLY=12
BACKUP_GDRIVE_HEALTHCHECK_MAX_AGE_SECONDS=900

RCLONE_CONFIG_GDRIVE_TOKEN='...'
RCLONE_CONFIG_GCRYPT_PASSWORD=...
```

`COMPOSE_PROFILES=backup` включает сервис. Без этой строки `docker compose up`
не запускает контейнер и не требует `RCLONE_*`.

Пароль БД берётся из существующих `POSTGRES_*`. Если в
`docker-compose.asteria.yml` пароль задан литералом, продублируй его в `.env`
как `POSTGRES_PASSWORD` — иначе `pg_dump` не подключится.

Все `BACKUP_GDRIVE_KEEP_*` — положительные целые. Ноль, пустое значение или
опечатка не «отключают» уровень: контейнер завершится с ошибкой при старте,
чтобы случайно не удалить все копии.

### 4. Запуск

Используй свою обычную команду; отдельный `-f` для бэкапов не нужен:

```bash
DC="docker compose -f docker-compose.yml -f docker-compose.local.yml -f docker-compose.asteria.yml"
$DC up -d --build
```

Проверь:

```bash
$DC ps
$DC logs -f backup
$DC exec backup rclone lsf gcrypt:5min --files-only
```

Первый цикл создаст дамп, загрузит пару в `5min/`, а также по одной паре в
`hourly/`, `daily/`, `weekly/`, `monthly/`.

### 5. Проверка

```bash
$DC exec backup rclone lsf gcrypt:5min --files-only
$DC exec backup sh -c 'rclone lsf gcrypt:5min --files-only | grep -c "\.dump$"'
docker inspect --format '{{.State.Health.Status}}' remnawave_bot_backup
```

После 13 загрузок в `5min/` должно остаться 12 дампов. Healthcheck — `healthy`,
если последняя успешная загрузка была меньше 15 минут назад.

## Восстановление

Для команд ниже снова определи `DC` — в новом шелле он не сохраняется:

```bash
DC="docker compose -f docker-compose.yml -f docker-compose.local.yml -f docker-compose.asteria.yml"
```

### База

1. Скачай нужный дамп через контейнер `backup` (у него уже есть rclone-конфиг):

```bash
$DC exec backup rclone copyto gcrypt:5min/26_January_2026_22-30.dump /backups/restore.dump
$DC cp backup:/backups/restore.dump ./db.dump
```

2. Останови бота **и** бэкапы, чтобы цикл не снял полувосстановленную базу:

```bash
$DC stop bot backup
```

3. Восстанови дамп через контейнер `postgres` (там есть `pg_restore`):

```bash
cat ./db.dump | $DC exec -T postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges'
```

`pg_restore` читает custom-формат из stdin, поэтому файл не нужно копировать в
контейнер. `--clean --if-exists` удаляет существующие объекты.

4. Запусти сервисы обратно и убери дамп с хоста:

```bash
$DC start bot backup
rm -f ./db.dump
```

Бот и бэкапы должны быть остановлены, чтобы не писать в базу и не снимать её в
процессе восстановления.

### `.env`

1. Скачай парный `.env.bak` через контейнер `backup`:

```bash
$DC exec backup rclone copyto gcrypt:5min/26_January_2026_22-30.env.bak /backups/env.restore
$DC cp backup:/backups/env.restore ./.env.restore
```

2. Останови сервисы, замени `.env` и пересоздай контейнеры:

```bash
$DC stop bot backup
cp ./.env.restore ./.env
chmod 600 .env
$DC up -d
rm -f ./.env.restore
```

`up -d` нужен, потому что `RCLONE_*` и `POSTGRES_*` подставляются в контейнеры
при создании. Если в восстановленном `.env` другой `POSTGRES_PASSWORD`,
существующая база его не подхватит: либо оставь старый пароль, либо смени пароль
пользователя в PostgreSQL вручную.

### Восстановление на новом хосте

Если сервера больше нет:

1. Поставь rclone и создай remote `gdrive`/`gcrypt` по сохранённому токену и
   паролю (или скопируй сохранённый `rclone.conf`).
2. Скачай самый свежий `.env.bak` из любого уровня и положи его как `.env` рядом
   с compose-файлами.
3. Подними только базу:

```bash
$DC up -d postgres
```

4. Скачай дамп и восстанови его в пустую базу:

```bash
rclone copyto gcrypt:5min/26_January_2026_22-30.dump ./db.dump
cat ./db.dump | $DC exec -T postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges'
```

5. Подними остальное:

```bash
$DC up -d
rm -f ./db.dump
```

Если `docker-compose.asteria.yml` на новом хосте нет — убери его из `DC`.

### Проверка после восстановления

```bash
$DC exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM users;"'
```

Проверь, что бот видит пользователей и подписки, и только потом удаляй старые
копии.

## Диагностика

| Симптом | Что проверить |
|---|---|
| `RCLONE_CONFIG_GDRIVE_TOKEN / ... are empty` в логах | Заполни оба секрета в `.env` |
| `ENV_FILE ... is missing or unreadable` | `.env` не смонтировался; проверь файл рядом с compose |
| `invalid KEEP_...` в логах | `BACKUP_GDRIVE_KEEP_*` должен быть положительным целым |
| `401`/`403` от Google Drive | Токен просрочен или отозван; получи новый через `rclone authorize "drive"` |
| `pg_dump: error: connection ...` | `POSTGRES_*` в `.env` не совпадают с базой |
| `directory not found` | Проверь `BACKUP_GDRIVE_FOLDER` и scope токена |
| Healthcheck `unhealthy` | `$DC logs backup`; посмотри `/backups/pending` в контейнере |
| В Drive видны имена, но файлы не читаются | Это нормально: содержимое зашифровано, читай через `rclone` |
| В Drive имена выглядят как gibberish | В конфиге crypt не выставлен `filename_encryption=off` |
| В Drive имена с `.bin` | Не выставлен `RCLONE_CONFIG_GCRYPT_SUFFIX=none` |

Проверить очередь внутри контейнера:

```bash
$DC exec backup sh -c 'ls -la /backups/pending /backups/local /backups/state'
```

## Важно

- `.env` содержит пароль БД, токен бота и ключи платёжных провайдеров. Его
  зашифрованная копия лежит рядом с дампами; не выкладывай папку публично.
- Пароль crypt хранится отдельно. Потеряешь его — потеряешь все копии.
- `uploads`, `locales`, код и Redis не бэкапятся. Штатный ежедневный архив бота
  остаётся второй копией и продолжает работать.
- `weekly` — скользящее окно 7 дней: дата в имени — первый дамп нового окна,
  а не обязательно понедельник.
