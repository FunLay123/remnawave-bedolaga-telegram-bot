# Remnawave Bot — резервные копии

Это папка с резервными копиями PostgreSQL и конфигурации бота.

## Структура

- `5min/` — последние 12 пар (1 час)
- `hourly/` — последние 24 пары (1 сутки)
- `daily/` — последние 30 пар
- `weekly/` — последние 12 пар
- `monthly/` — последние 12 пар

В каждой папке лежат пары:

- `DD_Month_YYYY_HH-MM.dump` — дамп PostgreSQL в custom-формате (`pg_dump -Fc`);
- `DD_Month_YYYY_HH-MM.env.bak` — копия `.env` на тот же момент.

Например:

```text
5min/26_January_2026_22-30.dump
5min/26_January_2026_22-30.env.bak
```

Если контейнер перезапустился в ту же минуту, имя получает суффикс `_2`.
В Drive имена без `.bin`: в конфиге crypt стоит `suffix = none`.

## Шифрование

Имена файлов и папок открытые, содержимое зашифровано rclone crypt.
Чтобы прочитать файлы, нужны:

- rclone;
- конфиг rclone с remote `gdrive` и `gcrypt`;
- пароль crypt (хранится отдельно от сервера, например в менеджере паролей).

Без пароля и конфига файлы не расшифровать. Этот `README.md` не зашифрован.

## Команда compose

```bash
DC="docker compose -f docker-compose.yml -f docker-compose.local.yml"
# если на хосте есть docker-compose.asteria.yml — добавь -f docker-compose.asteria.yml
```

## Если стек ещё работает

Используй `$DC` из начала документа. Скачай нужную пару через контейнер
`backup` — у него уже есть rclone-конфиг:

```bash
$DC exec backup rclone copyto gcrypt:5min/26_January_2026_22-30.dump /backups/restore.dump
$DC exec backup rclone copyto gcrypt:5min/26_January_2026_22-30.env.bak /backups/env.restore
$DC cp backup:/backups/restore.dump ./db.dump
$DC cp backup:/backups/env.restore ./.env.restore
```

## Восстановить базу

1. Останови бота и бэкапы, чтобы цикл не снял полувосстановленную базу:

```bash
$DC stop bot backup
```

2. Восстанови дамп через контейнер `postgres` (там есть `pg_restore`):

```bash
cat ./db.dump | $DC exec -T postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges'
```

`pg_restore` читает custom-формат из stdin. `--clean --if-exists` удаляет
существующие объекты перед восстановлением.

3. Запусти сервисы и убери дамп:

```bash
$DC start bot backup
rm -f ./db.dump
$DC exec backup rm -f /backups/restore.dump
```

## Восстановить `.env`

```bash
$DC stop bot backup
cp ./.env.restore ./.env
chmod 600 .env
$DC up -d
rm -f ./.env.restore
$DC exec backup rm -f /backups/env.restore
```

`up -d` нужен, потому что переменные подставляются в контейнеры при создании.
Если в восстановленном `.env` другой `POSTGRES_PASSWORD`, существующая база его
не подхватит: оставь старый пароль или смени пароль пользователя в PostgreSQL.

## Новый хост

Если сервера больше нет:

1. Поставь rclone и создай remote `gdrive`/`gcrypt` по сохранённому токену и
   паролю. Проще всего — сохранить готовый `rclone.conf` ещё при настройке.
   Если его нет, настрой так (salt/`password2` должен быть пустым):

```bash
rclone config create gdrive drive scope drive token '<JSON из rclone authorize>'
rclone config create gcrypt crypt \
  remote gdrive:remnawave-bot \
  filename_encryption off \
  directory_name_encryption false \
  suffix none \
  password "$(rclone obscure '<пароль>')"
```

2. Скачай самый свежий `.env.bak` и положи его как `.env` рядом с compose.
3. Подними только базу: `$DC up -d postgres`.
4. Скачай дамп и восстанови его:

```bash
rclone copyto gcrypt:5min/26_January_2026_22-30.dump ./db.dump
cat ./db.dump | $DC exec -T postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges'
```

5. Подними остальное: `$DC up -d`, затем `rm -f ./db.dump`.

## Проверка

```bash
$DC exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM users;"'
```

## Важно

- `.env` содержит токены, пароль БД и ключи платёжных провайдеров. Не пересылай его и не выкладывай публично.
- Пароль crypt не хранится в этой папке. Если он потерян, копии не расшифровать.
- Не меняй `RCLONE_CONFIG_GCRYPT_PASSWORD` после первых бэкапов: старые объекты
  станут нечитаемыми. Смена пароля — только с перезаливкой или сохранением
  старого ключа.
- Если у дампа нет парного `.env.bak` (старая копия или сбой), используй
  ближайший предыдущий `.env.bak` или текущий `.env`.
- После восстановления проверь, что бот видит базу и платежи, и только потом удаляй старые копии.
