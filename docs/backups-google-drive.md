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
добавляется `_2`.

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

### База

1. Останови бота:

```bash
$DC stop bot
```

2. Скачай нужный дамп на сервер:

```bash
rclone copyto gcrypt:5min/26_January_2026_22-30.dump ./db.dump
```

3. Положи дамп в контейнер `backup`:

```bash
$DC cp ./db.dump backup:/backups/restore.dump
```

4. Восстанови:

```bash
$DC exec -T backup sh -c \
  'pg_restore -U "$PGUSER" -d "$PGDATABASE" --clean --if-exists --no-owner --no-privileges /backups/restore.dump'
```

`--clean --if-exists` удаляет существующие объекты. Бот должен быть остановлен,
чтобы не писать в базу во время восстановления.

5. Запусти бота:

```bash
$DC start bot
```

Если на хосте есть `pg_restore`, можно восстановить без контейнера:

```bash
pg_restore --clean --if-exists --no-owner --no-privileges -d "$DATABASE_URL" ./db.dump
```

### `.env`

```bash
$DC stop bot
rclone copyto gcrypt:5min/26_January_2026_22-30.env.bak ./.env.restore
cp ./.env.restore ./.env
chmod 600 .env
$DC up -d bot
```

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
| `401`/`403` от Google Drive | Токен просрочен или отозван; получи новый через `rclone authorize "drive"` |
| `pg_dump: error: connection ...` | `POSTGRES_*` в `.env` не совпадают с базой |
| `directory not found` | Проверь `BACKUP_GDRIVE_FOLDER` и scope токена |
| Healthcheck `unhealthy` | `$DC logs backup`; посмотри `/backups/pending` в контейнере |
| В Drive видны имена, но файлы не читаются | Это нормально: содержимое зашифровано, читай через `rclone` |
| В Drive имена выглядят как gibberish | В конфиге crypt не выставлен `filename_encryption=off` |

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
