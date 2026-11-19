# Бэкапы в Google Drive: Restic + rclone

> **Текущая схема (2026-11-19).** Она заменяет прежний sidecar, который делал
> собственные копии в `5min/`, `hourly/`, `daily/`, `weekly/` и `monthly/`.
> Исторические spec/plan остаются как история решения; актуальны этот документ и
> `docker/backup/RESTORE.md`. Старые папки и старый `rclone.conf` не удаляются и
> не читаются новым сервисом — их можно убрать только после проверенного
> параллельного переключения. Прежние переменные `RCLONE_CONFIG_GDRIVE_TOKEN` и
> `RCLONE_CONFIG_GCRYPT_PASSWORD` больше не используются: не переноси их в новую
> схему.

Опциональный сервис `backup` каждые 5 минут снимает `pg_dump` PostgreSQL и
текущий `.env`, упаковывает их в один tar и отправляет **одним Restic-снапшотом**
в отдельный репозиторий на Google Drive через rclone. Сервис живёт отдельно от
бота: рестарт, падение или деплой бота не останавливают бэкапы.

Штатный ежедневный бэкап самого бота (`BackupService`, `BACKUP_*`) не изменён и
продолжает работать независимо. Backrest не используется.

## Что получается

```text
Google Drive (аккаунт оператора)
└── remnawave-restic/              ← отдельный Restic-репозиторий
    ├── config                     ← создаётся ТОЛЬКО `restic init`
    ├── data/...
    ├── index/...
    ├── keys/...
    └── snapshots/...
```

Внутри каждого снапшота лежит один файл `remnawave-backup.tar` (Restic может
показывать путь как `/remnawave-backup.tar`). Внутри tar — ровно два файла:

```text
db.dump   — pg_dump -Fc --no-owner --no-privileges --no-password
.env      — копия текущего .env на тот же цикл, режим 0600
```

Пара «дамп + `.env`» публикуется только целиком: Restic получает tar-поток через
`--stdin-from-command`. Если `tar` не смог прочитать один из файлов, Restic
завершается ошибкой и снапшот не публикуется. Отдельной очереди нет: неудачный
цикл не копится, следующий цикл делает новый дамп. `uploads`, `locales`, код,
Redis и логи в снапшот не входят.

| Параметр | Значение по умолчанию |
|---|---|
| Интервал | 300 секунд |
| RPO | до 5 минут (без WAL; это не point-in-time recovery) |
| Ретенция | 12 последних, 24 часовых, 30 дневных, 12 недельных, 12 месячных |
| Staging | tmpfs 256 МиБ, `0700` |
| Проверка свежести | 900 секунд |
| Автоматический maintenance | раз в сутки после успешного бэкапа, в 03:00 |

Ретенция Restic — это **политика**, а не копирование каждого снапшота во все
уровни. Один снапшот может одновременно попасть в hourly/daily/weekly/monthly;
дедупликация не создаёт лишних копий.

## Что настраивается отдельно от `.env`

В `.env` остаются только несекретные параметры и путь к файлу пароля. Реальные
секреты лежат в файлах на хосте, вне репозитория:

| Что | Путь по умолчанию | Права |
|---|---|---|
| rclone config с OAuth-токеном | `/etc/remnawave-backup/rclone/rclone.conf` | `0600`, каталог `0700` |
| Пароль Restic | `/etc/remnawave-backup/restic-password` | `0400` |
| Опциональный ping URL | `/etc/remnawave-backup/ping-url` | `0400` |

`rclone` должен иметь возможность **писать** в свой каталог: при обновлении
access token он переписывает config. Поэтому каталог rclone монтируется `rw`, а
файл пароля — только `ro`. Не храни пароль Restic в `.env`: `.env` попадает
внутрь каждого зашифрованного снапшота.

## 1. Свой OAuth client и отдельная папка

Не используй встроенный OAuth client rclone в продакшене: общий клиент
ограничен по квоте и выводится из эксплуатации. Создай собственный **Desktop**
OAuth client:

1. В Google Cloud Console создай проект (или отдельный проект для бэкапов).
2. Включи **Google Drive API**.
3. Заполни OAuth consent screen. Для боевого аккаунта переведи приложение в
   production; в testing-режиме Google выдаёт refresh-token на ограниченный
   срок (обычно 7 дней), и бэкапы перестанут ходить.
4. Создай credentials → OAuth client ID → **Desktop app**. Сохрани `client_id`
   и `client_secret` в менеджере паролей.
5. На машине с браузером создай отдельный каталог конфигурации и запусти
   `rclone config`:

```bash
install -d -m 700 /etc/remnawave-backup/rclone
RCLONE_CONFIG=/etc/remnawave-backup/rclone/rclone.conf rclone config
```

```text
name:   gdrive
type:   drive
client_id:     <свой Desktop client_id>
client_secret: <свой client_secret>
scope:  drive.file
```

`drive.file` даёт доступ только к файлам и папкам, которые создало это
приложение. Поэтому папку репозитория нужно создать **через rclone**, а не
выбирать существующую папку из «Мой диск» или «Доступные мне».

```bash
# для тестового аккаунта можно сначала создать отдельную папку
rclone --config /etc/remnawave-backup/rclone/rclone.conf \
  mkdir gdrive:remnawave-restic-test

# проверить, что rclone видит свою папку
rclone --config /etc/remnawave-backup/rclone/rclone.conf \
  lsf gdrive:remnawave-restic-test
```

Если папка создана другим инструментом или другим OAuth client, `drive.file`
может её не увидеть. Это нормальное ограничение scope, а не повод переходить на
полный `drive`.

Готовый `rclone.conf` (с refresh-token) сохрани в менеджере паролей. На новом
хосте без него бэкапы не прочитать.

## 2. Пароль Restic

Сгенерируй длинный случайный пароль, например:

```bash
install -d -m 700 /etc/remnawave-backup
openssl rand -base64 48 > /etc/remnawave-backup/restic-password
chmod 400 /etc/remnawave-backup/restic-password
```

Сохрани **сам пароль** (не только файл) в офлайн-менеджере паролей. Потеря
пароля означает потерю всех снапшотов: Restic не сможет их расшифровать.
Пароль не печатается в логи и не передаётся через `.env`.

## 3. Переменные `.env`

Включи профиль и задай параметры:

```dotenv
COMPOSE_PROFILES=backup

BACKUP_RESTIC_INTERVAL_SECONDS=300
BACKUP_RESTIC_KEEP_LAST=12
BACKUP_RESTIC_KEEP_HOURLY=24
BACKUP_RESTIC_KEEP_DAILY=30
BACKUP_RESTIC_KEEP_WEEKLY=12
BACKUP_RESTIC_KEEP_MONTHLY=12
BACKUP_RESTIC_MAINTENANCE_ENABLED=true
BACKUP_RESTIC_MAINTENANCE_TIME=03:00
BACKUP_RESTIC_HEALTHCHECK_MAX_AGE_SECONDS=900
BACKUP_RESTIC_TMPFS_SIZE=256m

# Отдельный репозиторий, НЕ старые папки remnawave-bot/5min|hourly|...
BACKUP_RESTIC_REPOSITORY=rclone:gdrive:remnawave-restic

# Пути на хосте, не внутри репозитория
BACKUP_RCLONE_CONFIG_DIR=/etc/remnawave-backup/rclone
BACKUP_RESTIC_PASSWORD_FILE=/etc/remnawave-backup/restic-password
# BACKUP_PING_URL_FILE=/etc/remnawave-backup/ping-url
```

Проверки при старте:

- интервал и все `KEEP_*` — положительные целые; ноль, пустое значение или
  опечатка завершают контейнер, а не «отключают» защиту;
- `BACKUP_RESTIC_TMPFS_SIZE` — от 16m до 1024m или ровно `1g`; больше 1 ГиБ
  запрещено, чтобы staging не съел память хоста;
- `BACKUP_RESTIC_MAINTENANCE_TIME` — строго `HH:MM`;
- репозиторий не должен указывать на старую папку `remnawave-bot`;
- файл пароля должен существовать и быть читаемым;
- для `rclone:`-репозитория должен существовать `rclone.conf`;
- одновременно может работать только один экземпляр (lock в state-каталоге).

## 4. Явная инициализация репозитория

Автосоздания нет: если репозиторий отсутствует или пароль неверный, сервис
пишет ошибку и **не** вызывает `restic init`. Инициализация — отдельное
осознанное действие оператора:

```bash
DC="docker compose -f docker-compose.yml -f docker-compose.local.yml"
# если на хосте есть docker-compose.asteria.yml — добавь -f docker-compose.asteria.yml

$DC --profile backup run --rm --no-deps --entrypoint restic backup init
$DC --profile backup run --rm --no-deps --entrypoint restic backup snapshots
```

Вторая команда должна показать пустой список и завершиться успешно. Если Restic
сообщает, что репозиторий уже существует, не удаляй его и не запускай `init`
повторно: проверь `BACKUP_RESTIC_REPOSITORY`.

> Не инициализируй Restic в старой папке `remnawave-bot/`, где лежат прежние
> `5min/`, `hourly/` и т.д. Это разные схемы; новый репозиторий должен быть
> отдельным (`remnawave-restic`).

## 5. Запуск и проверка

```bash
$DC --profile backup up -d --build backup
$DC logs -f backup
$DC exec backup restic snapshots
$DC exec backup restic ls latest
```

Первый цикл выполняется сразу после старта, затем — на ближайшем будущем
5-минутном слоте. Если цикл длится дольше интервала, пропущенные слоты не
ставятся в очередь: следующий запуск идёт в будущий слот.

Проверка свежести:

```bash
docker inspect --format '{{.State.Health.Status}}' remnawave_bot_backup
```

Healthcheck — это только проверка свежести маркера. Он **не** доказывает, что
снапшот можно восстановить; для этого нужен restore drill (см. ниже).

### Локальная проверка без Google Drive

Скрипты уважают стандартные переменные Restic, поэтому интеграционный тест можно
прогнать на локальном backend, не обращаясь к Drive:

```bash
# внутри тестового контейнера; pg_dump здесь — синтетическая заглушка
export RESTIC_REPOSITORY=/tmp/remnawave-restic-test
export RESTIC_PASSWORD=test-only
export BACKUP_RESTIC_STAGE_DIR=/tmp/stage
export BACKUP_RESTIC_STATE_DIR=/tmp/state
export ENV_FILE=/tmp/app.env
restic init
backup.sh --once
restic snapshots
```

Для проверки именно rclone-бэкенда можно создать локальный remote:

```bash
export RCLONE_CONFIG=/tmp/rclone.conf
rclone config create localtest local
RESTIC_REPOSITORY=rclone:localtest:/tmp/remnawave-restic-test restic init
```

Это только синтетические данные. Не запускай такие тесты против рабочего
Google Drive и не используй реальный `.env`.

## 6. Ретенция и maintenance

По умолчанию Restic хранит:

| Политика | Значение |
|---|---|
| `--keep-last` | 12 |
| `--keep-hourly` | 24 |
| `--keep-daily` | 30 |
| `--keep-weekly` | 12 |
| `--keep-monthly` | 12 |

Maintenance запускается в том же последовательном процессе **после успешного
бэкапа**, не чаще одного раза в сутки и не раньше `BACKUP_RESTIC_MAINTENANCE_TIME`
(по умолчанию 03:00). Обычные пятиминутные запуски `restic forget` не вызывают.
Если maintenance не удался, повтор в тот же день не выполняется каждые 5 минут;
оператор может запустить его вручную.

Ручной запуск:

```bash
# посмотреть, что будет удалено, ничего не удаляя
$DC exec backup backup.sh --maintenance --dry-run

# выполнить политику и prune
$DC exec backup backup.sh --maintenance
```

Ручной maintenance можно запускать на работающем loop: он берёт отдельный
operation-lock и выполняется между пятиминутными слотами. Если именно в этот
момент идёт бэкап, команда завершится с сообщением о занятом operation-lock —
повтори через несколько секунд. Второй loop одновременно не запускается.

Maintenance ограничен нашим host и тегом:

```text
--host remnawave-backup --tag remnawave-backup --group-by host,tags
```

Он не трогает старые GFS-объекты: их нет в этом репозитории. Старые папки
Google Drive не удаляются автоматически.

## 7. Маркер свежести и внешние пинги

После **полного** успешного `restic backup` сервис атомарно пишет
`/backups/restic/state/last_success` (epoch seconds). Если Restic вернул `3` (часть
исходных данных не прочитана), `10` (нет репозитория), `11` (lock), `12`
(неверный пароль) или другую ошибку, маркер **не** обновляется и healthcheck
остаётся красным.

Маркер также не обновляется при `--dry-run`. Healthcheck отклоняет:

- отсутствующий маркер;
- нечисловое/пустое содержимое;
- время в будущем больше чем на 5 минут;
- возраст больше `BACKUP_RESTIC_HEALTHCHECK_MAX_AGE_SECONDS`.

Причина отказа пишется в stderr healthcheck и видна в `docker inspect`/логах.

Healthcheck также отправляет опциональный внешний пинг, совместимый с
healthchecks.io: положи URL в `/etc/remnawave-backup/ping-url` (одна строка),
смонтируй файл `ro` и укажи `BACKUP_PING_URL_FILE`. При каждом запуске healthcheck
успех — GET на URL, ошибка — GET на `<URL>/fail`. URL читается из файла, не из
`.env`, и не попадает в логи. Если файла нет или он `/dev/null`, пинг отключён.

## 8. Безопасное восстановление

**По умолчанию восстановление неразрушающее.** Не запускай `pg_restore --clean`
против рабочей базы. Сначала восстанови снапшот в свежую БД, проверь данные, и
только потом переключайся.

### 8.1. Достать пару из снапшота

```bash
DC="docker compose -f docker-compose.yml -f docker-compose.local.yml"
# + -f docker-compose.asteria.yml, если он есть

$DC exec backup restic snapshots
$DC exec backup restic check
$DC exec backup restic ls <snapshot-id>

install -d -m 700 ./restore
$DC exec -T backup restic dump <snapshot-id> remnawave-backup.tar > ./restore/remnawave-backup.tar
tar -C ./restore -xf ./restore/remnawave-backup.tar
chmod 600 ./restore/db.dump ./restore/.env
tar -tvf ./restore/remnawave-backup.tar
file ./restore/db.dump
```

В каталоге должны быть ровно `db.dump` и `.env`. Не используй `.env` из другого
снапшота: пара должна быть с одного цикла.

### 8.2. Восстановить в свежую БД одной транзакцией

Останови бота, чтобы он не писал в базу, но рабочую БД пока не меняй. Для полной
изоляции лучше подними отдельный временный `postgres:18` на другом порту и
восстанови туда; команды ниже используют свежую БД в текущем кластере.

```bash
$DC stop bot backup
$DC exec -T postgres sh -c \
  'createdb -U "$POSTGRES_USER" remnawave_restore_verify'
cat ./restore/db.dump | $DC exec -T postgres sh -c \
  'pg_restore --single-transaction --exit-on-error --no-owner --no-privileges \
     -U "$POSTGRES_USER" -d remnawave_restore_verify'
```

`--single-transaction` гарантирует, что при ошибке не останется наполовину
восстановленной схемы. `--exit-on-error` не даёт проглотить ошибку. Флаг
`--clean` здесь не нужен и опасен.

Проверь:

```bash
$DC exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d remnawave_restore_verify \
     -c "SELECT count(*) FROM users;" \
     -c "SELECT count(*) FROM subscriptions;"'
```

Сравни числа с ожидаемыми и проверь пару свежих записей. Если этого мало,
подними одноразовый бот на этой БД в отдельной сети/порту.

### 8.3. Переключение без `--clean`

Когда проверенная БД устраивает:

```bash
$DC stop bot backup
$DC exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d postgres -c "ALTER DATABASE remnawave_bot RENAME TO remnawave_bot_old_$(date +%Y%m%d);"'
$DC exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d postgres -c "ALTER DATABASE remnawave_restore_verify RENAME TO remnawave_bot;"'
```

`ALTER DATABASE` требует отсутствия подключений к переименовываемой БД: бот и
`backup` должны быть остановлены. Затем замени `.env` **только после** проверки
пары:

```bash
cp .env .env.before-restore-$(date +%Y%m%d)
install -m 600 ./restore/.env .env
$DC up -d
```

Если в восстановленном `.env` другой `POSTGRES_PASSWORD`, существующая роль
PostgreSQL его не подхватит: оставь старый пароль или смени пароль роли
вручную. Старую БД `remnawave_bot_old_*` и старый `.env` не удаляй, пока бот и
платежи не проверены. Откат: переименуй БД обратно и верни старый `.env`.

### 8.4. Новый хост

1. Поставь Docker/Compose, `restic` и `rclone` (или используй образ sidecar).
2. Восстанови из офлайн-копии `rclone.conf` и файл пароля Restic. Не делай
   `restic init` — репозиторий уже существует.
3. Проверь доступ: `restic snapshots`.
4. Подними только `postgres`, создай свежую БД и восстанови снапшот по шагам
   выше.
5. Запусти бота, проверь БД и платежи, и только потом удаляй старые копии и
   старые GFS-папки.

## 9. Безопасность и ограничения

- Restic шифрует содержимое и метаданные репозитория. Но `.env` внутри снапшота
  содержит все секреты приложения, поэтому доступ к репозиторию и паролю должен
  быть ограничен.
- `rclone.conf` содержит refresh-token: `0600`, только на хосте, не в Git.
- Пароль Restic хранится отдельно от сервера в менеджере паролей. Без него
  снапшоты не расшифровать.
- Staging — tmpfs `0700`; перед дампом проверяется свободное место. Дамп и
  `.env` удаляются после каждого обработанного успеха/ошибки. При SIGKILL
  staging очищается при следующем старте (tmpfs также очищается при перезапуске
  контейнера).
- SIGTERM не обрывает активный `pg_dump`/`restic`: сервис завершает текущую
  операцию и выходит. Docker `stop_grace_period` — 2 минуты; принудительный
  SIGKILL может оставить lock/временный каталог, но следующий старт их уберёт.
- Пятиминутный интервал — это не WAL и не zero-RPO. Потеря последних минут
  данных при аварии возможна.
- Healthcheck проверяет только свежесть маркера, а не целостность архива.
  Периодически делай restore drill в свежую БД.
- `weekly`/`monthly` в Restic считаются по календарным правилам Restic, а не
  копированием одной пары в каждую папку.
- Старые GFS-объекты, старый `rclone.conf` и старые `.env.bak` не удаляются
  автоматически. Их удаление — отдельное решение после параллельной проверки.

## Диагностика

| Симптом | Что проверить |
|---|---|
| `restic repository does not exist` | Репозиторий не создан: выполни явный `restic init` с правильным `BACKUP_RESTIC_REPOSITORY` |
| `rclone config is missing` | `BACKUP_RCLONE_CONFIG_DIR` и файл `rclone.conf` на хосте |
| `401`/`403` от Google | OAuth client, scope `drive.file`, тестовый пользователь, срок refresh-token |
| `restic password is incorrect` | Файл `/etc/remnawave-backup/restic-password` |
| `restic repository is locked` | Убедись, что второй процесс не работает; только затем `restic unlock` вручную |
| `restic backup could not read all source data` (exit 3) | Restic не прочитал tar; маркер не обновлён, смотри `docker logs backup` |
| `not enough free space` | Размер `BACKUP_RESTIC_TMPFS_SIZE` и рост дампа |
| Healthcheck `unhealthy` | `$DC logs backup`, `/backups/restic/state/last_success` |
| `WARN: ... is not a tmpfs mount` | Профиль/версия Compose не применили `tmpfs`; проверь `docker inspect` |
| Не видно папки в Drive | `drive.file` видит только созданное rclone; создай папку через rclone |
| В Drive старые имена `5min/*.dump` | Это старая схема; новый сервис пишет только в отдельный Restic-репозиторий |
