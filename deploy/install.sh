#!/usr/bin/env bash
# Установка или обновление job-spy-bot на VPS (Debian 12+ / Ubuntu 24.04+).
#   curl -fsSL https://raw.githubusercontent.com/luxsordel/job-spy-bot/master/deploy/install.sh | sudo bash
# или из клона репозитория:  sudo ./deploy/install.sh
set -euo pipefail

REPO="${REPO:-https://github.com/luxsordel/job-spy-bot.git}"
BRANCH="${BRANCH:-master}"
APP_DIR=/opt/job-spy-bot
DATA_DIR=/var/lib/job-spy-bot
ENV_DIR=/etc/job-spy-bot
SERVICE=job-spy-bot

[[ $EUID -eq 0 ]] || { echo "Запустите от root (sudo)"; exit 1; }

if ! command -v git >/dev/null || ! python3 -c 'import venv' 2>/dev/null; then
    if command -v apt-get >/dev/null; then
        apt-get update -qq && apt-get install -y -qq git python3 python3-venv
    else
        echo "Установите git и python3-venv"; exit 1
    fi
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || { echo "Нужен Python 3.11+, установлен $(python3 -V)"; exit 1; }

id jobspy >/dev/null 2>&1 \
    || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin jobspy

if [[ -d "$APP_DIR/.git" ]]; then
    echo "→ Обновление кода"
    git -C "$APP_DIR" fetch -q origin "$BRANCH"
    git -C "$APP_DIR" checkout -q "$BRANCH"
    git -C "$APP_DIR" pull -q --ff-only origin "$BRANCH"
else
    echo "→ Клонирование $REPO"
    git clone -q --branch "$BRANCH" "$REPO" "$APP_DIR"
fi

echo "→ Зависимости"
[[ -x "$APP_DIR/.venv/bin/python" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --no-cache-dir --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q --no-cache-dir "$APP_DIR"

install -d -o jobspy -g jobspy -m 750 "$DATA_DIR"
install -d -g jobspy -m 750 "$ENV_DIR"
if [[ ! -f "$ENV_DIR/env" ]]; then
    install -g jobspy -m 640 "$APP_DIR/.env.example" "$ENV_DIR/env"
    sed -i "s|^DB_PATH=.*|DB_PATH=$DATA_DIR/jobspy.sqlite3|" "$ENV_DIR/env"
    echo "→ Создан $ENV_DIR/env — заполните токены"
fi

install -m 644 "$APP_DIR/deploy/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable -q "$SERVICE"

if grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$ENV_DIR/env"; then
    systemctl restart "$SERVICE"
    echo "✓ Сервис перезапущен. Логи: journalctl -u $SERVICE -f"
else
    echo "✓ Установлено. Заполните $ENV_DIR/env и выполните: systemctl start $SERVICE"
fi
