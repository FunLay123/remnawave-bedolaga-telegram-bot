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
показывать путь как `/remnawave-backup.tar`). По умолчанию внутри tar — ровно
два файла:

```text
db.dump   — pg_dump -Fc --no-owner --no-privileges --no-password
.env      — копия текущего .env на тот же цикл, режим 0600
```

Если явно включён `BACKUP_COMPOSE_FILES`, в тот же tar добавляются
`compose/project-name`, `compose/files.list` и выбранные Compose-файлы в порядке
списка. Это единственное изменение формата; путь `remnawave-backup.tar` прежний.
Старые двухфайловые снапшоты остаются читаемыми через data-only restore.

Пара «дамп + `.env`» публикуется только целиком: Restic получает tar-поток через
`--stdin-from-command`. Если `tar` не смог прочитать один из файлов, Restic
завершается ошибкой и снапшот не публикуется. Отдельной очереди нет: неудачный
цикл не копится, следующий цикл делает новый дамп. `uploads`, `locales`, код,
Redis и логи в снапшот не входят. Монтированные Docker credentials, secrets,
локальные volumes и live Docker state не копируются.

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

### Compose-конфигурация в снапшоте (опционально)

Production Asteria использует project `bot-asteria` и три Compose-входа. Задай
на production-хосте:

```dotenv
COMPOSE_PROJECT_NAME=bot-asteria
BACKUP_COMPOSE_FILES=docker-compose.yml:docker-compose.local.yml:docker-compose.asteria.yml
```

При восстановлении оператор задаёт то же значение явно:
`EXPECTED_COMPOSE_PROJECT=bot-asteria`. Оно не выводится из имени каталога и не
подменяется «похожим» project.

`COMPOSE_PROJECT_NAME` попадает в backup как `BACKUP_COMPOSE_PROJECT` через
`${COMPOSE_PROJECT_NAME:-}`. Project ID должен быть задан явно: через
`COMPOSE_PROJECT_NAME=bot-asteria` или `docker compose -p bot-asteria`. В Compose
v5.8.0 `-p` также попадает в интерполяцию; на старой версии, если
`BACKUP_COMPOSE_PROJECT` оказался пустым, экспортируй `COMPOSE_PROJECT_NAME` для
процесса, который запускает backup. Иначе backup откажется публиковать снапшот с
выбранными файлами.

Правила выбора:

- только basenames, ровно `.yml`/`.yaml`, уникальные, в порядке списка;
- файл должен быть читаемым непустым обычным файлом (не symlink) в том же
  каталоге, что и app `.env` (в контейнере `/src`); glob, абсолютные пути, `..`
  и подкаталоги запрещены;
- список — allowlist: невыбранные override-файлы в архив не попадают;
- при отсутствии, нечитаемости, symlink или изменении выбранного файла снапшот
  не публикуется, а маркер свежести остаётся прежним.

Тестовый стенд не должен делить с production ни Restic repository, ни project,
ни подсеть, ни host port, ни fixed container names:

```dotenv
COMPOSE_PROJECT_NAME=remnawave-bot-test
BACKUP_COMPOSE_FILES=docker-compose.yml:docker-compose.local.yml:docker-compose.test.yml
BACKUP_RESTIC_REPOSITORY=rclone:gdrive-test:remnawave-restic-test
BOT_NETWORK_SUBNET=172.31.0.0/24
BOT_NETWORK_GATEWAY=172.31.0.1
WEB_API_PORT=18080
```

На одном host этого мало: `docker-compose.local.yml` фиксирует `container_name`
(`remnawave_bot`, `remnawave_bot_db`, ...), а `docker-compose.yml` — host port.
Не переименовывай live project; создай отдельный test project и переопредели все
`container_name`, порт и сеть, либо используй отдельный host. Пример
operator-maintained `docker-compose.test.yml`, не checked in:

```yaml
services:
  postgres:
    container_name: remnawave_bot_db_test
  redis:
    container_name: remnawave_bot_redis_test
  bot:
    container_name: remnawave_bot_test
  backup:
    container_name: remnawave_bot_backup_test
networks:
  remnawave-network:
    name: remnawave-test-network
    driver: bridge
```

`docker-compose.local.yml` объявляет `remnawave-network` как Compose-managed сеть
с фиксированным именем и **без** `external: true`. Это не panel network: local
override создаёт/использует локальную сеть и не должен считаться attach к панели.
Для panel deployment нужен отдельный operator-maintained override, который не
коммитится, например:

```yaml
# docker-compose.panel-network.yml (operator-maintained, not checked in)
networks:
  remnawave-network:
    external: true
```

Включай его в `BACKUP_COMPOSE_FILES` только осознанно. Test не должен
присоединяться к shared prod aliases через local override, если это не сделано
намеренно; test override выше переименовывает сеть.

Общая сеть `remnawave-network` — внешняя зависимость, которой управляет оператор
панели. Снапшот не содержит Docker network state и не меняет сети. Preflight в
runbook требует effective `external: true` и проверяет существующую сеть и её
provenance (Compose project label или явное подтверждение для unlabeled). Если
сети нет, остановись и согласуй её с оператором панели; не создавай shared panel
network из этого runbook. Частое предупреждение на текущем deployment не даёт
разрешения менять сеть. Project-local сети (например `bot_network`) Compose
создаёт сам; не меняй network wiring в Compose ради восстановления, не добавляй
`network inspect` в backup и не пытайся архивировать Docker socket.

Production repository остаётся `rclone:gdrive:remnawave-restic`. Файл
`docker-compose.asteria.yml` — private operator override: не коммить его в Git и
не подменяй тестовым или чужим файлом.

## 4. Явная инициализация репозитория

Автосоздания нет: если репозиторий отсутствует или пароль неверный, сервис
пишет ошибку и **не** вызывает `restic init`. Инициализация — отдельное
осознанное действие оператора:

```bash
DC="docker compose -f docker-compose.yml -f docker-compose.local.yml"
# если на хосте есть docker-compose.asteria.yml — добавь -f docker-compose.asteria.yml

$DC --profile backup run --rm --no-deps --entrypoint restic backup --no-cache init
$DC --profile backup run --rm --no-deps --entrypoint restic backup --no-cache snapshots
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
$DC exec backup restic --no-cache snapshots
$DC exec backup restic --no-cache ls latest
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
restic --no-cache snapshots
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

Выполняй 8.1–8.3 в одном shell с `set -euo pipefail`: `DC`, `RESTORE_DIR`,
`RESTORE_DB`, `ORIGINAL_DB`, `ENV_BACKUP` должны сохраниться между блоками, а
после ошибки нельзя продолжать. `RESTORE_DIR` — стабильный приватный каталог вне
Git и не `/tmp`; не удаляй его, пока проверка не закончена.

`docker/backup/restore-compose.sh` запускается из checkout репозитория и
не копируется в backup-образ. Его `--output` — только этот приватный каталог:
сначала проверка, потом явное решение оператора, и только затем установка
выбранных файлов в project root.

### 8.0. Общие функции восстановления

Функции ниже читают проверенный `compose/files.list` построчно в Bash-массив.
Никакого `eval`, `source` manifest'а, `$(cat ...)` и неявного word splitting:
только `read -r` и явные элементы `DC`. Это работает для одного файла и для
любого корректного project name.

```bash
validate_compose_project() {
    [[ ${1:-} =~ ^[a-z0-9][a-z0-9_-]*$ ]]
}

valid_compose_name() {
    [[ ${1:-} =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml$ ]]
}

compose_from_repo() {
    local project_dir=$1 project_name=$2
    local name seen=' ' count=0
    shift 2
    validate_compose_project "$project_name" || {
        echo "invalid project: $project_name" >&2
        return 1
    }
    DC=(docker compose --project-directory "$project_dir" -p "$project_name")
    for name in "$@"; do
        valid_compose_name "$name" || {
            echo "invalid Compose input: $name" >&2
            return 1
        }
        case "$seen" in
            *" $name "*)
                echo "duplicate Compose input: $name" >&2
                return 1
                ;;
        esac
        seen="$seen$name "
        if [ ! -f "$project_dir/$name" ] || [ -L "$project_dir/$name" ] || [ ! -s "$project_dir/$name" ]; then
            echo "missing, empty or symlinked Compose input: $name" >&2
            return 1
        fi
        DC+=(-f "$project_dir/$name")
        count=$((count + 1))
    done
    [ "$count" -gt 0 ] || {
        echo 'no Compose inputs selected' >&2
        return 1
    }
}

compose_from_restore() {
    local restore_dir=$1 project_name=$2 project_dir=$3
    local recorded name seen=' ' count=0
    validate_compose_project "$project_name" || {
        echo "invalid project: $project_name" >&2
        return 1
    }
    IFS= read -r recorded < "$restore_dir/compose/project-name" || {
        echo 'missing compose/project-name' >&2
        return 1
    }
    if [ "$recorded" != "$project_name" ]; then
        echo "project mismatch: archive '$recorded', expected '$project_name'" >&2
        return 1
    fi
    DC=(docker compose --project-directory "$project_dir" -p "$project_name")
    while IFS= read -r name || [ -n "$name" ]; do
        valid_compose_name "$name" || {
            echo "invalid Compose input: $name" >&2
            return 1
        }
        case "$seen" in
            *" $name "*)
                echo "duplicate Compose input: $name" >&2
                return 1
                ;;
        esac
        seen="$seen$name "
        if [ ! -f "$restore_dir/compose/$name" ] || [ -L "$restore_dir/compose/$name" ] || [ ! -s "$restore_dir/compose/$name" ]; then
            echo "missing, empty or symlinked restored Compose input: $name" >&2
            return 1
        fi
        DC+=(-f "$restore_dir/compose/$name")
        count=$((count + 1))
    done < "$restore_dir/compose/files.list"
    [ "$count" -gt 0 ] || {
        echo 'restored Compose file list is empty' >&2
        return 1
    }
}

preflight_panel_network() {
    local config network_labels project_label PANEL_NETWORK_CONFIRM
    config=$("${DC[@]}" config) || {
        echo 'cannot render effective Compose config' >&2
        return 1
    }
    if ! printf '%s\n' "$config" | awk '
        BEGIN { in_networks = 0; in_target = 0; external = 0; found = 0; bad_name = 0 }
        /^networks:[[:space:]]*$/ { in_networks = 1; next }
        in_networks && /^[^[:space:]]/ { in_networks = 0; in_target = 0 }
        in_networks && /^  remnawave-network:[[:space:]]*$/ { in_target = 1; found = 1; next }
        in_target && /^  [^[:space:]]/ { in_target = 0 }
        in_target && /^    external:[[:space:]]*true[[:space:]]*$/ { external = 1 }
        in_target && /^    name:/ {
            name = $0
            sub(/^    name:[[:space:]]*/, "", name)
            gsub(/["[:space:]]/, "", name)
            if (name != "remnawave-network") bad_name = 1
        }
        END { exit !(found && external && !bad_name) }
    '; then
        echo 'remnawave-network must be external: true via an operator-maintained override' >&2
        return 1
    fi
    network_labels=$(docker network inspect remnawave-network --format '{{json .Labels}}' 2>/dev/null) || {
        echo 'shared remnawave-network is missing; verify it with the panel operator; do not create it here' >&2
        return 1
    }
    project_label=$(docker network inspect remnawave-network \
        --format '{{index .Labels "com.docker.compose.project"}}' 2>/dev/null || true)
    if [ -n "${EXPECTED_PANEL_PROJECT:-}" ]; then
        if [ "$project_label" != "$EXPECTED_PANEL_PROJECT" ]; then
            echo "network project label '$project_label' != '$EXPECTED_PANEL_PROJECT'; stop" >&2
            return 1
        fi
    else
        printf 'network labels: %s\n' "$network_labels"
        printf 'Type YES to confirm this is the panel-managed remnawave-network: '
        read -r PANEL_NETWORK_CONFIRM
        if [ "$PANEL_NETWORK_CONFIRM" != YES ]; then
            return 1
        fi
    fi
}
```

Проверенный `compose/files.list` — единственный источник порядка `-f` после
restore. `--project-directory "$PROJECT_DIR"` задаёт нужный base path для
`.env`, bind mounts и `build: .`, а `-p "$EXPECTED_COMPOSE_PROJECT"` не даёт
Compose создать второй project или переконфигурировать live site.

### 8.1. Остановить writers и прочитать снапшот

Сначала останавливаем bot и backup loop: так `restic check` не конкурирует с
активным backup, а cutover не пишет в БД. Чтение делается одноразовым
контейнером с entrypoint `restic`, поэтому backup loop/prune не запускаются.
Это работает и когда контейнера `backup` ещё нет (новый хост): Compose `run`
создаёт его только на время команды.

```bash
set -euo pipefail
umask 077

PROJECT_DIR=$(pwd -P)
if [ -z "${EXPECTED_COMPOSE_PROJECT:-}" ]; then
    printf 'Expected Compose project: '
    read -r EXPECTED_COMPOSE_PROJECT
fi
export EXPECTED_COMPOSE_PROJECT

# Set exactly the ordered inputs of the existing live project. Do not add a
# file merely because it exists; do not rename the live project.
REPO_COMPOSE_FILES=(docker-compose.yml docker-compose.local.yml docker-compose.asteria.yml)

RECOVERY_ROOT=${RECOVERY_ROOT:-/var/lib/remnawave-recovery}
install -d -m 700 "$RECOVERY_ROOT"
RESTORE_DIR="$RECOVERY_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 700 "$RESTORE_DIR"

# Never create or target a second project by accident.
if [ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$EXPECTED_COMPOSE_PROJECT")" ]; then
    echo "no containers for project '$EXPECTED_COMPOSE_PROJECT'; verify the live project name" >&2
    exit 1
fi

compose_from_repo "$PROJECT_DIR" "$EXPECTED_COMPOSE_PROJECT" "${REPO_COMPOSE_FILES[@]}"
"${DC[@]}" stop bot backup

"${DC[@]}" --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache snapshots
# check запускай, когда нет активного backup-writer. Если repository locked,
# сначала убедись, что writer действительно не работает; unlock вручную.
"${DC[@]}" --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache check
if [ -z "${SNAPSHOT_ID:-}" ]; then
    printf 'Snapshot ID: '
    read -r SNAPSHOT_ID
fi
[ -n "$SNAPSHOT_ID" ] || { echo 'SNAPSHOT_ID is required' >&2; exit 1; }

"${DC[@]}" --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache ls "$SNAPSHOT_ID"
"${DC[@]}" --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache dump "$SNAPSHOT_ID" remnawave-backup.tar \
  > "$RESTORE_DIR/remnawave-backup.tar"

# Read-only: покажет project/order или явный legacy notice.
sh docker/backup/restore-compose.sh \
  --archive "$RESTORE_DIR/remnawave-backup.tar" \
  --inspect

# Выбери по выводу inspect. Legacy 16e67daf не содержит Compose metadata и
# допускается только через явный data-only путь.
printf 'Snapshot kind [compose/legacy]: '
read -r SNAPSHOT_KIND
if [ "$SNAPSHOT_KIND" = compose ]; then
    sh docker/backup/restore-compose.sh \
      --archive "$RESTORE_DIR/remnawave-backup.tar" \
      --project "$EXPECTED_COMPOSE_PROJECT" \
      --output "$RESTORE_DIR"
elif [ "$SNAPSHOT_KIND" = legacy ]; then
    sh docker/backup/restore-compose.sh \
      --archive "$RESTORE_DIR/remnawave-backup.tar" \
      --legacy-data-only \
      --output "$RESTORE_DIR"
else
    echo 'SNAPSHOT_KIND must be compose or legacy' >&2
    exit 1
fi
# Для legacy оставь DC из compose_from_repo: проверенные live-файлы. Не
# подставляй override "на глаз"; automatic fallback отсутствует.
if [ -f "$RESTORE_DIR/compose/files.list" ]; then
    compose_from_restore "$RESTORE_DIR" "$EXPECTED_COMPOSE_PROJECT" "$PROJECT_DIR"
    # Existing host: recovered inputs must match the reviewed live files. Stop
    # instead of silently reconfiguring the running site.
    while IFS= read -r name; do
        cmp -s "$PROJECT_DIR/$name" "$RESTORE_DIR/compose/$name" || {
            echo "recovered Compose input differs from live: $name; stop" >&2
            exit 1
        }
    done < "$RESTORE_DIR/compose/files.list"
fi

cat "$RESTORE_DIR/compose/project-name" 2>/dev/null || true
cat "$RESTORE_DIR/compose/files.list" 2>/dev/null || true
file "$RESTORE_DIR/db.dump"
"${DC[@]}" exec -T postgres pg_restore --list < "$RESTORE_DIR/db.dump" >/dev/null
```

Helper не распаковывает архив через `tar -xf` и не исполняет manifest: он
проверяет точный набор членов и типы файлов, стримит только `db.dump`, `.env` и
`compose/*` в приватный scratch, а в `$RESTORE_DIR` кладёт результат лишь после
совпадения `compose/project-name` с ожидаемым project. При mismatch каталог
`$RESTORE_DIR` не получает ни Compose-файлов, ни данных. Для снапшота с Compose
metadata в `$RESTORE_DIR/compose/files.list` лежит авторитетный порядок; в
`compose/project-name` — записанный project ID. В стабильном `$RESTORE_DIR`
лежит и сам `remnawave-backup.tar`; каталог не удаляется автоматически и не
должен попадать в Git. Плейнтекст tar/dump не кладётся в репозиторий.

Проверь входы до применения: сравни `compose/files.list` с ожидаемым порядком,
просмотри сами файлы и только потом используй явные `-p`/`-f`. Не применяй
восстановленный Compose поверх уже запущенного stack.

### 8.2. Зафиксировать фактическую БД и восстановить в свежую

Целевую БД берём из **preflight effective app Compose config** (bot и backup), а
не из env-файла контейнера `postgres`: после earlier `--no-deps` cutover env
postgres-контейнера может остаться старым. Для rollback expected DB — это
`$ORIGINAL_DB`, полученный этим preflight. Если preflight не совпадает с тем,
с чем фактически работал бот, сначала приведи `.env` в соответствие и не начинай
cutover.

```bash
# Убираем host-override, чтобы Compose прочитал .env.
unset POSTGRES_DB

ORIGINAL_BOT_DB=$("${DC[@]}" --profile backup run --rm --no-deps -T \
  --entrypoint sh bot -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
ORIGINAL_BACKUP_DB=$("${DC[@]}" --profile backup run --rm --no-deps -T \
  --entrypoint sh backup -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'preflight bot=%s backup=%s\n' "$ORIGINAL_BOT_DB" "$ORIGINAL_BACKUP_DB"
[ -n "$ORIGINAL_BOT_DB" ] && [ "$ORIGINAL_BOT_DB" = "$ORIGINAL_BACKUP_DB" ] || {
  echo 'preflight DB mismatch; stop and fix Compose config' >&2
  exit 1
}
ORIGINAL_DB="$ORIGINAL_BOT_DB"

# Fixed short ASCII prefix + timestamp/PID, independent of ORIGINAL_DB:
# PostgreSQL truncates identifiers over 63 bytes, including multibyte names.
RESTORE_DB="remnawave_restore_$(date +%Y%m%d%H%M%S)_$$"
echo "original=$ORIGINAL_DB restore=$RESTORE_DB"

# psql :"restore_db" безопасно квотирует идентификатор. POSTGRES_USER должен
# раскрыться внутри контейнера, а не в host shell под set -u.
printf 'CREATE DATABASE :"restore_db";\n' |
  "${DC[@]}" exec -T postgres sh -c \
    'psql -v ON_ERROR_STOP=1 -v restore_db="$1" -U "$POSTGRES_USER" -d postgres' \
    sh "$RESTORE_DB"

"${DC[@]}" exec -T postgres sh -c \
  'pg_restore --single-transaction --exit-on-error --no-owner --no-privileges \
     -U "$POSTGRES_USER" -d "$1"' sh "$RESTORE_DB" \
  < "$RESTORE_DIR/db.dump"
```

`--single-transaction` гарантирует, что при ошибке не останется наполовину
восстановленной схемы. `--exit-on-error` не даёт проглотить ошибку. Флаг
`--clean` здесь не нужен и опасен.

Проверь:

```bash
"${DC[@]}" exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$1" -v ON_ERROR_STOP=1 \
     -c "SELECT count(*) FROM users;" \
     -c "SELECT count(*) FROM subscriptions;"' sh "$RESTORE_DB"
```

Сравни числа с ожидаемыми и проверь пару свежих записей. Не поднимай второй
экземпляр production-бота с тем же `BOT_TOKEN`: Telegram допускает один poller, а
тестовая БД не должна получать production webhook. Для проверки достаточно SQL
и `psql`; при необходимости используй отдельный тестовый токен и сеть. Рабочую
базу и текущий `.env` не меняй, пока проверка не закончена.

### 8.3. Cutover A/B/C с rollback

#### A. Остановить и сохранить исходный live `.env` — до любых правок

```bash
"${DC[@]}" stop bot backup

# Идентифицируемая приватная копия .env вне репозитория, только для rollback.
ROLLBACK_DIR=$(mktemp -d /tmp/remnawave-env-rollback.XXXXXX)
chmod 700 "$ROLLBACK_DIR"
ENV_BACKUP="$ROLLBACK_DIR/.env.before-restore"
install -m 600 .env "$ENV_BACKUP"
[ -s "$ENV_BACKUP" ] || { echo 'rollback .env copy is empty' >&2; exit 1; }
echo "rollback env: $ENV_BACKUP"
```

Блок A не меняет `.env`. Не редактируй `.env` до его выполнения.

#### B. Явно изменить `.env`, дождаться Enter, затем preflight и запуск

Блок напечатает **готовое literal-значение** `RESTORE_DB`. Впиши в `.env` ровно
строку `POSTGRES_DB=<это значение>` (не `$RESTORE_DB` буквально), сохрани файл и
нажми Enter. `read` останавливает выполнение, если команды вставлены одним
куском; без Enter дальше не пойдёт.

```bash
printf 'Set exactly in .env: POSTGRES_DB=%s\n' "$RESTORE_DB"
printf 'Press Enter after saving .env... '
read -r _
[ -f "$ENV_BACKUP" ] || { echo 'missing rollback .env; run block A first' >&2; exit 1; }

# Host export has precedence over .env in Compose interpolation.
unset POSTGRES_DB
PRE_BOT_DB=$("${DC[@]}" --profile backup run --rm --no-deps -T \
  --entrypoint sh bot -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
PRE_BACKUP_DB=$("${DC[@]}" --profile backup run --rm --no-deps -T \
  --entrypoint sh backup -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'preflight cutover bot=%s backup=%s\n' "$PRE_BOT_DB" "$PRE_BACKUP_DB"
[ "$PRE_BOT_DB" = "$RESTORE_DB" ] && [ "$PRE_BACKUP_DB" = "$RESTORE_DB" ] || {
  echo 'preflight DB mismatch; fix .env, do not start' >&2
  exit 1
}

"${DC[@]}" up -d --no-deps --force-recreate bot backup

BOT_DB=$("${DC[@]}" exec -T bot sh -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
BACKUP_DB=$("${DC[@]}" exec -T backup sh -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'effective bot=%s backup=%s\n' "$BOT_DB" "$BACKUP_DB"
[ "$BOT_DB" = "$RESTORE_DB" ] && [ "$BACKUP_DB" = "$RESTORE_DB" ] || {
  echo 'effective DB mismatch; stop and fix config before continuing' >&2
  exit 1
}
```

#### C. Rollback: вернуть исходный `.env` из A и проверить

```bash
"${DC[@]}" stop bot backup
[ -f "$ENV_BACKUP" ] || { echo 'missing rollback .env; run block A first' >&2; exit 1; }
unset POSTGRES_DB
install -m 600 "$ENV_BACKUP" .env

PRE_BOT_DB=$("${DC[@]}" --profile backup run --rm --no-deps -T \
  --entrypoint sh bot -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
PRE_BACKUP_DB=$("${DC[@]}" --profile backup run --rm --no-deps -T \
  --entrypoint sh backup -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'preflight rollback bot=%s backup=%s\n' "$PRE_BOT_DB" "$PRE_BACKUP_DB"
[ "$PRE_BOT_DB" = "$ORIGINAL_DB" ] && [ "$PRE_BACKUP_DB" = "$ORIGINAL_DB" ] || {
  echo 'rollback preflight mismatch; fix .env, do not start' >&2
  exit 1
}

"${DC[@]}" up -d --no-deps --force-recreate bot backup

BOT_DB=$("${DC[@]}" exec -T bot sh -c 'printf "%s" "$POSTGRES_DB"' | tr -d '\r')
BACKUP_DB=$("${DC[@]}" exec -T backup sh -c 'printf "%s" "$PGDATABASE"' | tr -d '\r')
printf 'rollback effective bot=%s backup=%s\n' "$BOT_DB" "$BACKUP_DB"
[ "$BOT_DB" = "$ORIGINAL_DB" ] && [ "$BACKUP_DB" = "$ORIGINAL_DB" ] || {
  echo 'rollback effective DB mismatch; stop and fix config' >&2
  exit 1
}
```

Если восстановленный `.env` содержит другой `POSTGRES_PASSWORD`, существующая
роль PostgreSQL его не подхватит: оставь старый пароль или смени пароль роли
вручную. Старую БД `$ORIGINAL_DB` не переименовывай и не удаляй до проверки.

Не удаляй `$ORIGINAL_DB`, `$RESTORE_DB`, старый `.env`, `$ENV_BACKUP` и
`$ROLLBACK_DIR`, пока бот и платежи не проверены. После стабилизации:

```bash
rm -rf "$RESTORE_DIR"
rm -rf "$ROLLBACK_DIR"
```

### 8.4. Новый хост без running backup

Порядок важен: сначала читаем удалённый снапшот одноразовым Restic, **не**
устанавливая app `.env` и не запуская backup loop; только потом ставим `.env` и
поднимаем сервисы. `compose run` с entrypoint `restic` не запускает автоматический
backup/prune.

1. Поставь Docker/Compose, восстанови из офлайн-копии `rclone.conf` и файл пароля
   Restic. Не выполняй `restic init`.
2. Собери/скачай образы `bot` и `backup`, чтобы `compose run` мог их использовать.
   `EXPECTED_COMPOSE_PROJECT` — операторское значение, не догадка; `REPO_COMPOSE_FILES`
   — ровно те файлы, которые уже лежат в project root и будут использованы только
   для чтения Restic. На новом хосте live-контейнеров ещё нет.

```bash
set -euo pipefail
umask 077

PROJECT_DIR=$(pwd -P)
if [ -z "${EXPECTED_COMPOSE_PROJECT:-}" ]; then
    printf 'Expected Compose project: '
    read -r EXPECTED_COMPOSE_PROJECT
fi
export EXPECTED_COMPOSE_PROJECT
REPO_COMPOSE_FILES=(docker-compose.yml docker-compose.local.yml)
# Если operator-maintained panel override уже установлен и проверен:
# REPO_COMPOSE_FILES+=(docker-compose.panel-network.yml)

compose_from_repo "$PROJECT_DIR" "$EXPECTED_COMPOSE_PROJECT" "${REPO_COMPOSE_FILES[@]}"
"${DC[@]}" --profile backup build bot backup
```

3. Пока app `.env` ещё не установлен, прочитай снапшот. Выполняй шаги 3–6 в
   одном shell: `RESTORE_DIR`, `RESTORE_DB` и `ORIGINAL_DB` должны сохраниться.
   Если твои repo/пути были кастомными, экспортируй их для этого shell (это не
   секреты); иначе действуют defaults из compose:

```bash
RECOVERY_ROOT=${RECOVERY_ROOT:-/var/lib/remnawave-recovery}
install -d -m 700 "$RECOVERY_ROOT"
RESTORE_DIR="$RECOVERY_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 700 "$RESTORE_DIR"
# export BACKUP_RESTIC_REPOSITORY='rclone:gdrive:remnawave-restic'
# export BACKUP_RCLONE_CONFIG_DIR='/etc/remnawave-backup/rclone'
# export BACKUP_RESTIC_PASSWORD_FILE='/etc/remnawave-backup/restic-password'

"${DC[@]}" --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache snapshots
if [ -z "${SNAPSHOT_ID:-}" ]; then
    printf 'Snapshot ID: '
    read -r SNAPSHOT_ID
fi
[ -n "$SNAPSHOT_ID" ] || { echo 'SNAPSHOT_ID is required' >&2; exit 1; }

"${DC[@]}" --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache check
"${DC[@]}" --profile backup run --rm --no-deps -T --entrypoint restic backup \
  --no-cache dump "$SNAPSHOT_ID" remnawave-backup.tar \
  > "$RESTORE_DIR/remnawave-backup.tar"

# Read-only: покажет project/order или явный legacy notice.
sh docker/backup/restore-compose.sh \
  --archive "$RESTORE_DIR/remnawave-backup.tar" \
  --inspect

# Выбери по выводу inspect. Legacy 16e67daf не содержит Compose metadata и
# допускается только через явный data-only путь.
printf 'Snapshot kind [compose/legacy]: '
read -r SNAPSHOT_KIND
if [ "$SNAPSHOT_KIND" = compose ]; then
    sh docker/backup/restore-compose.sh \
      --archive "$RESTORE_DIR/remnawave-backup.tar" \
      --project "$EXPECTED_COMPOSE_PROJECT" \
      --output "$RESTORE_DIR"
elif [ "$SNAPSHOT_KIND" = legacy ]; then
    sh docker/backup/restore-compose.sh \
      --archive "$RESTORE_DIR/remnawave-backup.tar" \
      --legacy-data-only \
      --output "$RESTORE_DIR"
else
    echo 'SNAPSHOT_KIND must be compose or legacy' >&2
    exit 1
fi

file "$RESTORE_DIR/db.dump"

if [ -f "$RESTORE_DIR/compose/files.list" ]; then
    # Проверенный files.list — единственный источник project и порядка -f.
    compose_from_restore "$RESTORE_DIR" "$EXPECTED_COMPOSE_PROJECT" "$PROJECT_DIR"
else
    # Legacy 16e67daf: оператор явно выбирает доверенные файлы. Не подставляй
    # override "на глаз" и не считай наличие файла разрешением его использовать.
    TRUSTED_COMPOSE_FILES=(docker-compose.yml docker-compose.local.yml)
    # TRUSTED_COMPOSE_FILES+=(docker-compose.panel-network.yml)
    compose_from_repo "$PROJECT_DIR" "$EXPECTED_COMPOSE_PROJECT" "${TRUSTED_COMPOSE_FILES[@]}"
fi
```

4. До установки `.env` проверь recovered Compose inputs. Не перезаписывай
   существующие файлы project root слепо: сначала diff, затем явное решение.
   Если не устанавливаешь exact-файлы, оставь capture выключенным
   (`BACKUP_COMPOSE_FILES=`) до ручного восстановления root inputs. Иначе следующий
   backup молча снимет stale root Compose files.

```bash
if [ -d "$RESTORE_DIR/compose" ]; then
    while IFS= read -r name; do
        diff -u "$PROJECT_DIR/$name" "$RESTORE_DIR/compose/$name" || true
    done < "$RESTORE_DIR/compose/files.list"
fi
printf 'Install exact recovered Compose files into %s? Type YES: ' "$PROJECT_DIR"
read -r INSTALL_COMPOSE
if [ "$INSTALL_COMPOSE" = YES ]; then
    while IFS= read -r name; do
        install -m 600 "$RESTORE_DIR/compose/$name" "$PROJECT_DIR/$name"
    done < "$RESTORE_DIR/compose/files.list"
    while IFS= read -r name; do
        cmp -s "$PROJECT_DIR/$name" "$RESTORE_DIR/compose/$name" || {
            echo "root Compose input diverged: $name" >&2
            exit 1
        }
    done < "$RESTORE_DIR/compose/files.list"
else
    echo 'Compose files were not installed; keep BACKUP_COMPOSE_FILES= empty in the recovered .env.' >&2
fi
```

Проверь и установи восстановленный `.env` как live-конфигурацию только после
этого решения:

```bash
if [ "$INSTALL_COMPOSE" != YES ]; then
    grep -q '^BACKUP_COMPOSE_FILES=$' "$RESTORE_DIR/.env" || {
        echo 'Set BACKUP_COMPOSE_FILES= in the recovered .env before installing it' >&2
        exit 1
    }
fi
install -m 600 "$RESTORE_DIR/.env" .env
```

5. Preflight effective Compose config и existing shared network **до** любого
   `up`. Функция не создаёт и не меняет сети; при ошибке остановись и согласуй
   panel network с его оператором. Проверенный `external: true` должен приходить
   из operator-maintained override, а не из checked-in
   `docker-compose.local.yml`: локальный override создаёт Compose-managed сеть с
   тем же именем, а не присоединяется к panel network. Частое предупреждение на
   текущем deployment не даёт разрешения менять сеть.

```bash
# Если у panel network есть Compose project label, задай его явно:
# EXPECTED_PANEL_PROJECT='<panel-compose-project>'
preflight_panel_network

# Только после успешного preflight:
"${DC[@]}" up -d --no-deps postgres redis
"${DC[@]}" exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d postgres'
"${DC[@]}" exec -T redis redis-cli ping
```

6. Дальше выполни 8.2 (preflight фактической БД, создание свежей БД и
   single-transaction restore) и 8.3 A/B/C (сохранение исходного `.env`,
   cutover и rollback). На новом хосте `ORIGINAL_DB` — это БД из восстановленного
   `.env`; старой населённой БД может не быть, но конфигурацию и файл всё равно
   сохраняем для rollback.

## 9. Безопасность и ограничения

- `BACKUP_COMPOSE_FILES` — явный allowlist; невыбранные файлы не попадают даже
  в staging. Восстановленные Compose-файлы содержат пути/значения конкретного
  хоста: держи их в приватном restore-каталоге, не коммить private override и не
  применяй поверх запущенного stack. Helper проверяет project до записи.
- Panel deployment: `remnawave-network` должен быть `external: true` из
  operator-maintained override. Checked-in `docker-compose.local.yml` объявляет
  Compose-managed сеть с тем же именем и не является panel network; common
  warning на текущем deployment не разрешает менять её. Preflight проверяет
  effective config, существующую сеть и Compose project label/ручное
  подтверждение. Снапшот не содержит Docker network state; project-local сети
  Compose создаёт сам, не меняй network wiring ради восстановления.
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
- Restic запускается с `--no-cache` в automated sidecar и в ручных restore/check
  командах runbook, поэтому кэш не растёт неограниченно в writable layer
  контейнера. Цена — повторное чтение индексов из rclone.
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
