# Бэкапы в Google Drive: Restic + rclone

> **Текущая схема (2026-11-19).** Она заменяет прежний sidecar, который делал
> собственные копии в `5min/`, `hourly/`, `daily/`, `weekly/` и `monthly/`.
> Исторические spec/plan остаются как история решения; актуальны этот документ и
> `docker/backup/RESTORE.md`. Старые папки и старый `rclone.conf` не удаляются и
> не читаются новым сервисом — их можно убрать только после проверенного
> параллельного переключения. Прежние переменные `RCLONE_CONFIG_GDRIVE_TOKEN` и
> `RCLONE_CONFIG_GCRYPT_PASSWORD` больше не используются: не переноси их в новую
> схему.

Опциональный сервис `backup` по целевому расписанию раз в 5 минут снимает
`pg_dump` PostgreSQL и
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
| Целевой интервал | 300 секунд; это цель, а не гарантированный RPO |
| Ретенция | 12 последних, 24 часовых, 30 дневных, 12 недельных, 12 месячных |
| Staging | tmpfs 256 МиБ, `0700` |
| Проверка свежести | 900 секунд |
| Автоматический maintenance | раз в сутки после успешного бэкапа, в 03:00 |

Пятиминутный интервал — это **целевая частота**, а не гарантированный RPO:
длительность `pg_dump`, загрузки, ошибки и пропущенные слоты увеличивают
фактический разрыв. Это не WAL и не point-in-time recovery.

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
BACKUP_RESTIC_REQUIRE_TMPFS=true

# Отдельный репозиторий, НЕ старые папки remnawave-bot/5min|hourly|...
BACKUP_RESTIC_REPOSITORY=rclone:gdrive:remnawave-restic

# Пути на хосте, не внутри репозитория
BACKUP_RCLONE_CONFIG_DIR=/etc/remnawave-backup/rclone
BACKUP_RESTIC_PASSWORD_FILE=/etc/remnawave-backup/restic-password
# BACKUP_PING_URL_FILE=/etc/remnawave-backup/ping-url
```

Проверки при старте:

- интервал и все `KEEP_*` — положительные целые; ноль, ведущий ноль или
  опечатка завершают контейнер, а не «отключают» защиту;
- `BACKUP_RESTIC_HEALTHCHECK_MAX_AGE_SECONDS` — целое от 1 до 604800;
- `BACKUP_RESTIC_TMPFS_SIZE` — от 16m до 1024m или ровно `1g`; больше 1 ГиБ
  запрещено, чтобы staging не съел память хоста;
- `BACKUP_RESTIC_REQUIRE_TMPFS` — `true` в production (по умолчанию) или
  `false` только для синтетического теста, где stage заменён bind mount;
- `BACKUP_RESTIC_MAINTENANCE_TIME` — строго `HH:MM`;
- репозиторий не должен указывать на старую папку `remnawave-bot`;
- файл пароля должен существовать, быть читаемым и непустым;
- для `rclone:`-репозитория `rclone.conf` должен быть читаемым непустым
  обычным файлом;
- одновременно может работать только один backup-loop (lock в state-каталоге);
  ручной `--maintenance` сериализуется с ним через operation-lock.

Compose и скрипт используют запись `${VAR:-default}`: **пустое** значение
переменной означает default, а не «выключено». Например, пустой
`BACKUP_RESTIC_TMPFS_SIZE` станет `256m`, пустой
`BACKUP_RESTIC_HEALTHCHECK_MAX_AGE_SECONDS` — `900`, пустой
`BACKUP_RESTIC_REPOSITORY` — `rclone:gdrive:remnawave-restic`. Чтобы отключить
необязательный ping, используй отсутствующий файл или `/dev/null`, а не пустой
путь; production staging не отключается пустой строкой.

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
# /tmp/stage здесь не tmpfs: это явный синтетический override.
export BACKUP_RESTIC_REQUIRE_TMPFS=false
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
День фиксируется в state **до** `forget/prune`: если маркер дня нельзя записать,
maintenance отказывается удалять и возвращает ошибку. В работающем процессе есть
дополнительный at-most-once-per-day fallback, чтобы неудачная запись маркера не
приводила к prune каждые 5 минут. Если maintenance не удался, оператор может
запустить его вручную.

Restic запускается с `--no-cache`: sidecar не накапливает неограниченный кэш в
writable layer контейнера. Плата — повторное чтение индексов с rclone; для
небольшого репозитория это приемлемо. Maintenance и backup используют один и тот
же режим.

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
- нечисловое, пустое или слишком длинное содержимое (защита от `test`-ошибок
  BusyBox, которые иначе означали бы fail-open);
- ведущий ноль и другие значения вне разумного диапазона epoch;
- время в будущем больше чем на 5 минут;
- возраст больше `BACKUP_RESTIC_HEALTHCHECK_MAX_AGE_SECONDS`;
- `BACKUP_RESTIC_HEALTHCHECK_MAX_AGE_SECONDS` вне диапазона 1..604800.

Причина отказа пишется в stderr healthcheck и видна в `docker inspect`/логах.
Healthcheck не является доказательством восстановимости: проверяй снапшоты
restore drill'ом.

Healthcheck также отправляет опциональный внешний пинг, совместимый с
healthchecks.io: положи URL в `/etc/remnawave-backup/ping-url` (одна строка),
смонтируй файл `ro` и укажи `BACKUP_PING_URL_FILE`. При каждом запуске healthcheck
успех — GET на URL, ошибка — GET на `<URL>/fail`. URL читается из файла, не из
`.env`, и не попадает в логи. Пустая переменная означает «пинг не настроен».
Если путь задан, но файла нет, healthcheck пишет предупреждение в stderr.
`/dev/null` или char-device, созданный bind-mount'ом Compose, считается
намеренным отключением без предупреждения.

## 8. Безопасное восстановление

**По умолчанию восстановление неразрушающее.** Не запускай `pg_restore --clean`
против рабочей базы. Сначала восстанови снапшот в свежую БД, проверь данные, и
только потом переключайся.

### 8.1. Достать пару в приватный временный каталог

Выполняй 8.1–8.3 в одном shell с `set -eu`: после ошибки нельзя продолжать, а
`trap` удалит приватные артефакты при выходе.

```bash
set -eu
umask 077

DC="docker compose -f docker-compose.yml -f docker-compose.local.yml"
# + -f docker-compose.asteria.yml, если он есть

RESTORE_DIR=$(mktemp -d /tmp/remnawave-restore.XXXXXX)
chmod 700 "$RESTORE_DIR"
trap 'rm -rf "$RESTORE_DIR"' EXIT

$DC exec backup restic snapshots
# restic check запускай, когда backup не держит operation lock (loop спит
# между слотами). Не делай unlock, пока идёт активный backup.
$DC exec backup restic check
if [ -z "${SNAPSHOT_ID:-}" ]; then
    printf 'Snapshot ID: '
    read -r SNAPSHOT_ID
fi
[ -n "$SNAPSHOT_ID" ] || { echo 'SNAPSHOT_ID is required' >&2; exit 1; }

$DC exec backup restic ls "$SNAPSHOT_ID"
$DC exec -T backup restic dump "$SNAPSHOT_ID" remnawave-backup.tar \
  > "$RESTORE_DIR/remnawave-backup.tar"
tar -C "$RESTORE_DIR" -xf "$RESTORE_DIR/remnawave-backup.tar"
chmod 600 "$RESTORE_DIR/db.dump" "$RESTORE_DIR/.env"
tar -tvf "$RESTORE_DIR/remnawave-backup.tar"
file "$RESTORE_DIR/db.dump"
$DC exec -T postgres pg_restore --list < "$RESTORE_DIR/db.dump" >/dev/null
```

В tar должны быть ровно два члена: `db.dump` и `.env`. После распаковки в
`$RESTORE_DIR` временно лежит и сам `remnawave-backup.tar`; это нормально, его
удалит `trap` (или явный `rm` ниже). Не используй `.env` из другого снапшота:
пара должна быть с одного цикла. Плейнтекст tar/dump не кладётся в репозиторий;
каталог создаётся через `mktemp` вне его. Если нужно сохранить артефакты для
ручного разбора, убери `trap`, но ограничь доступ и удали каталог после проверки.

### 8.2. Восстановить в свежую БД одной транзакцией

Останови бота, чтобы он не писал в базу, но рабочую БД не меняй. Для полной
изоляции лучше подними отдельный временный `postgres:18` на другом порту и
восстанови туда; команды ниже используют свежую БД в текущем кластере.

```bash
$DC stop bot backup

ORIGINAL_DB=$($DC exec -T postgres sh -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
[ -n "$ORIGINAL_DB" ] || { echo 'POSTGRES_DB is empty' >&2; exit 1; }
RESTORE_DB="${ORIGINAL_DB}_restore_$(date +%Y%m%d%H%M%S)"
echo "original=$ORIGINAL_DB restore=$RESTORE_DB"

# SQL identifier quoting: psql :"restore_db" -> CREATE DATABASE "name".
# POSTGRES_USER must be expanded inside the container, not by the host shell.
printf 'CREATE DATABASE :"restore_db";\n' |
  $DC exec -T postgres sh -c \
    'psql -v ON_ERROR_STOP=1 -v restore_db="$1" -U "$POSTGRES_USER" -d postgres' \
    sh "$RESTORE_DB"

$DC exec -T postgres sh -c \
  'pg_restore --single-transaction --exit-on-error --no-owner --no-privileges \
     -U "$POSTGRES_USER" -d "$1"' sh "$RESTORE_DB" \
  < "$RESTORE_DIR/db.dump"
```

`--single-transaction` гарантирует, что при ошибке не останется наполовину
восстановленной схемы. `--exit-on-error` не даёт проглотить ошибку. Флаг
`--clean` здесь не нужен и опасен. `ORIGINAL_DB` берётся из фактического
`POSTGRES_DB` контейнера, а не из захардкоженного имени.

Проверь:

```bash
$DC exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$1" -v ON_ERROR_STOP=1 \
     -c "SELECT count(*) FROM users;" \
     -c "SELECT count(*) FROM subscriptions;"' sh "$RESTORE_DB"
```

Сравни числа с ожидаемыми и проверь пару свежих записей. Не поднимай второй
экземпляр production-бота с тем же `BOT_TOKEN`: Telegram допускает один poller, а
тестовая БД не должна получать production webhook. Для проверки достаточно SQL
и `psql`; при необходимости используй отдельный тестовый токен и сеть. Не трогай
рабочую базу и текущий `.env`, пока проверка не закончена.

### 8.3. Переключение на проверенную БД с rollback

**Default — без переименования БД и без удаления старой базы.** Старая база
остаётся под исходным именем, новая — под `$RESTORE_DB`; переключение делается
только конфигурацией `POSTGRES_DB`.

В `.env` установи **ровно** `POSTGRES_DB=$RESTORE_DB` (если строки нет — добавь).
Не полагайся на export в текущем shell: Compose отдаёт приоритет переменной
окружения shell над `.env`, поэтому перед `up` её нужно снять.

```bash
$DC stop bot backup

# Идентифицируемая приватная копия .env вне репозитория, только для rollback.
ROLLBACK_DIR=$(mktemp -d /tmp/remnawave-env-rollback.XXXXXX)
chmod 700 "$ROLLBACK_DIR"
ENV_BACKUP="$ROLLBACK_DIR/.env.before-restore"
install -m 600 .env "$ENV_BACKUP"
echo "rollback env: $ENV_BACKUP"

# Если восстанавливаешь и конфигурацию: сначала проверь diff с
# "$RESTORE_DIR/.env" (не выводя секреты), затем:
#   install -m 600 "$RESTORE_DIR/.env" .env
# и обязательно установи в нём POSTGRES_DB=$RESTORE_DB.
#
# Снимаем унаследованный POSTGRES_DB, иначе Compose проигнорирует .env.
unset POSTGRES_DB
$DC up -d --no-deps --force-recreate bot backup

BOT_DB=$($DC exec -T bot sh -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
BACKUP_DB=$($DC exec -T backup sh -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'effective bot POSTGRES_DB=%s\n' "$BOT_DB"
printf 'effective backup PGDATABASE=%s\n' "$BACKUP_DB"
[ "$BOT_DB" = "$RESTORE_DB" ] && [ "$BACKUP_DB" = "$RESTORE_DB" ] || {
  echo 'effective DB mismatch; stop and fix config before continuing' >&2
  exit 1
}
```

Проверь вход в бота, свежие записи и платежи. Если бот не стартует или данные
неверны, rollback одной конфигурацией. Rollback тоже должен снять возможный
host-override:

```bash
$DC stop bot backup
unset POSTGRES_DB
install -m 600 "$ENV_BACKUP" .env
$DC up -d --no-deps --force-recreate bot backup

BOT_DB=$($DC exec -T bot sh -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
BACKUP_DB=$($DC exec -T backup sh -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'rollback bot POSTGRES_DB=%s\n' "$BOT_DB"
printf 'rollback backup PGDATABASE=%s\n' "$BACKUP_DB"
[ "$BOT_DB" = "$ORIGINAL_DB" ] && [ "$BACKUP_DB" = "$ORIGINAL_DB" ] || {
  echo 'rollback DB mismatch; stop and fix config before continuing' >&2
  exit 1
}
```

Если восстановленный `.env` содержит другой `POSTGRES_PASSWORD`, существующая
роль PostgreSQL его не подхватит: оставь старый пароль или смени пароль роли
вручную. Старую БД `$ORIGINAL_DB` не переименовывай и не удаляй до проверки.

Старая БД `$ORIGINAL_DB` не переименовывалась и не удалялась; rollback возвращает
к ней. Не удаляй `$RESTORE_DB`, `$ORIGINAL_DB`, старый `.env`, `$ENV_BACKUP` и
`$ROLLBACK_DIR`, пока работа не подтверждена. После стабилизации явно удали
временные артефакты:

```bash
rm -rf "$RESTORE_DIR"
rm -rf "$ROLLBACK_DIR"
```

### 8.4. Новый хост

1. Поставь Docker/Compose, `restic` и `rclone` (или используй образ sidecar).
2. Восстанови из офлайн-копии `rclone.conf` и файл пароля Restic. Не делай
   `restic init` — репозиторий уже существует.
3. Проверь доступ: `restic snapshots`.
4. Подними только базовые сервисы и подготовь пустую БД:
   `$DC up -d --no-deps postgres redis`; создай свежую БД и восстанови снапшот
   по шагам выше.
5. В `.env` укажи `POSTGRES_DB` восстановленной БД (без переименования старой),
   затем подними приложение без рекурсии зависимостей:
   `$DC up -d --no-deps --force-recreate bot backup`. Проверь БД и платежи, и
   только потом удаляй старые копии и старые GFS-папки. Если что-то не так,
   верни прежний `POSTGRES_DB` и старый `.env`.

## 9. Безопасность и ограничения

- Restic шифрует содержимое и метаданные репозитория. Но `.env` внутри снапшота
  содержит все секреты приложения, поэтому доступ к репозиторию и паролю должен
  быть ограничен.
- `rclone.conf` содержит refresh-token: `0600`, только на хосте, не в Git.
- Пароль Restic хранится отдельно от сервера в менеджере паролей. Без него
  снапшоты не расшифровать.
- Staging — tmpfs `0700`; `BACKUP_RESTIC_REQUIRE_TMPFS=true` в production
  проверяет, что это действительно tmpfs. Перед дампом проверяется свободное
  место. Дамп и `.env` удаляются после каждого обработанного успеха/ошибки.
  Для синтетического smoke с bind-mount stage нужен явный
  `BACKUP_RESTIC_REQUIRE_TMPFS=false`; это не production-настройка.
- SIGTERM не обрывает активный `pg_dump`/`restic`: сервис завершает текущую
  операцию и выходит. Docker `stop_grace_period` — 2 минуты; принудительный
  SIGKILL может оставить локальный stage/локальный lock. Локальный stage
  очищается при следующем старте (tmpfs также очищается при перезапуске
  контейнера), но **удалённый Restic lock автоматически не снимается**. Если
  после SIGKILL Restic сообщает `repository is locked`, сначала убедись, что
  других активных writers нет, и только затем выполни `restic unlock` вручную.
- Пятиминутный интервал — это **целевая частота, а не гарантированный RPO**.
  Без WAL фактический разрыв зависит от длительности дампа/загрузки и ошибок;
  потеря последних минут данных при аварии возможна.
- Restic запускается с `--no-cache`, поэтому кэш не растёт неограниченно в
  writable layer контейнера. Цена — повторное чтение индексов из rclone.
- `RCLONE_TIMEOUT=2m` — это idle timeout одной I/O-операции, а
  `RCLONE_CONTIMEOUT=10s` — только установка соединения. `RCLONE_RETRIES=3`
  ограничивает число повторов; это **не** общий дедлайн всего upload/restore.
- `pg_dump` запускается с `--lock-wait-timeout=30s`: ожидание блокировки не
  бесконечно, но общий дедлайн дампа этим не задаётся.
- Healthcheck проверяет только свежесть маркера, а не целостность архива.
  Периодически делай restore drill в свежую БД. Маркер с огромным числом,
  ведущим нулём или некорректным `MAX_AGE` отклоняется fail-closed, а не
  пропускается из-за ошибки BusyBox `test`.
- `weekly`/`monthly` в Restic считаются по календарным правилам Restic, а не
  копированием одной пары в каждую папку.
- Старые GFS-объекты, старый `rclone.conf` и старые `.env.bak` не удаляются
  автоматически. Их удаление — отдельное решение после параллельной проверки.

## Диагностика

| Симптом | Что проверить |
|---|---|
| `restic repository does not exist` | Репозиторий не создан: выполни явный `restic init` с правильным `BACKUP_RESTIC_REPOSITORY` |
| `rclone config is missing` | `BACKUP_RCLONE_CONFIG_DIR`; файл `rclone.conf` должен быть читаемым непустым обычным файлом |
| `401`/`403` от Google | OAuth client, scope `drive.file`, тестовый пользователь, срок refresh-token |
| `restic password is incorrect` | Файл `/etc/remnawave-backup/restic-password` |
| `restic repository is locked` | Убедись, что второй процесс не работает; только затем `restic unlock` вручную |
| `restic backup could not read all source data` (exit 3) | Restic не прочитал tar; маркер не обновлён, смотри `docker logs backup` |
| `cannot persist maintenance day marker` | State-каталог недоступен для записи; maintenance отказался удалять, исправь права/volume |
| `not enough free space` | Размер `BACKUP_RESTIC_TMPFS_SIZE` и рост дампа |
| `Healthcheck unhealthy` / `corrupt freshness marker` / `invalid ... MAX_AGE` | `$DC logs backup`, `/backups/restic/state/last_success`; marker/MAX_AGE проверяются fail-closed |
| `... is not a tmpfs mount` | Production требует tmpfs; проверь Compose `tmpfs` и `docker inspect`. Для синтетического bind-mount smoke: `BACKUP_RESTIC_REQUIRE_TMPFS=false` |
| `pg_dump ... lock timeout` | `--lock-wait-timeout=30s`: кто-то держит блокировку; разберись с writer'ом, не увеличивай таймаут вслепую |
| `ping URL file is missing` | Задан `BACKUP_PING_URL_FILE`, но файла нет; для отключения используй `/dev/null` или пустую переменную |
| Не видно папки в Drive | `drive.file` видит только созданное rclone; создай папку через rclone |
| В Drive старые имена `5min/*.dump` | Это старая схема; новый сервис пишет только в отдельный Restic-репозиторий |
