# Планировщик грузового пространства: запуск на сервере

Что внутри:
- `server.py` — сервер (регистрация, вход, история расчётов). Только Python, без установки библиотек.
- `static/index.html` — сама страница планировщика.
- `Dockerfile`, `docker-compose.yml`, `Caddyfile` — запуск в Docker с автоматическим https.

Адрес по умолчанию: https://cargotest.duckdns.org (меняется в `Caddyfile`).

## 1. Направьте адрес на сервер
На duckdns.org в строке вашего адреса (cargotest) впишите IP сервера в поле «current ip» и нажмите «update ip».

## 2. Зайдите на сервер
На Windows откройте PowerShell (на Mac и Linux — Терминал) и выполните:

    ssh root@IP_СЕРВЕРА

Подтвердите подключение словом `yes` и введите пароль из письма Aeza (при вводе пароль не отображается — это нормально).

## 3. Установите Docker (один раз)

    curl -fsSL https://get.docker.com | sh

Если команда не сработала, запасной вариант:

    apt update && apt install -y docker.io docker-compose-v2

Проверка: `docker compose version` должно показать версию.

## 4. Загрузите проект
На вашем компьютере (не на сервере), в папке, где лежит архив `cargo-site.zip`:

    scp cargo-site.zip root@IP_СЕРВЕРА:/root/

Затем на сервере:

    apt install -y unzip
    unzip cargo-site.zip -d /opt
    cd /opt/cargo-site

## 5. Запустите

    docker compose up -d --build

Первый запуск занимает 1–3 минуты. Через минуту откройте https://cargotest.duckdns.org — должна открыться страница, а справа вверху кнопки «Войти» и «Регистрация».

Если сайт не открывается:
- `docker compose logs caddy` — покажет, получил ли сервер сертификат. Ошибка обычно значит, что адрес DuckDNS указывает не на тот IP или закрыты порты 80 и 443.
- Если на сервере включён файервол: `ufw allow 22 && ufw allow 80 && ufw allow 443`.
- `docker compose logs app` — журнал самого приложения.

## Полезные команды (из папки /opt/cargo-site)

| Что нужно | Команда |
|---|---|
| Сколько зарегистрировалось | `docker compose exec app python server.py stats` |
| Перезапустить | `docker compose restart` |
| Остановить | `docker compose down` |
| Обновить страницу или сервер | заменить файл, затем `docker compose up -d --build` |
| Сделать копию базы | см. ниже |

Копия базы (пользователи и история):

    docker compose exec app python -c "import sqlite3;s=sqlite3.connect('/data/app.db');d=sqlite3.connect('/data/backup.db');s.backup(d)"
    docker compose cp app:/data/backup.db ./backup.db

## Что важно знать
- Пароли хранятся только в виде хэша, прочитать их нельзя. Восстановления пароля по почте пока нет: его нужно добавлять вместе с отправкой писем.
- Сессия входа живёт 30 дней. После 8 неверных паролей подряд вход блокируется на 10 минут.
- Пока на сервере только тестовые аккаунты. Реальных пользователей из России лучше подключать после переезда на российский сервер (закон о хранении персональных данных).
- Переезд: поставить Docker на новом сервере, скопировать папку проекта и `backup.db`, указать новый IP в DNS.
