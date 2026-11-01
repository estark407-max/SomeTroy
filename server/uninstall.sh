#!/usr/bin/env bash
# Полное удаление Троя (SomeTroy) с сервера: служба, папка с кодом, настройками и базой, правило порта.
# Запуск на сервере:  bash /opt/sometroy/uninstall.sh
set -e
SERVICE=sometroy
DIR=/opt/sometroy
PORT="$(grep -E '^DESKTOP_PORT=' "$DIR/settings.env" 2>/dev/null | cut -d= -f2 | tr -d '\r ')"
PORT="${PORT:-8443}"

echo "==> Останавливаю и удаляю службу $SERVICE..."
systemctl stop "$SERVICE" 2>/dev/null || true
systemctl disable "$SERVICE" >/dev/null 2>&1 || true
rm -f "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "^$PORT/tcp"; then
    echo "==> Закрываю порт $PORT в ufw..."
    ufw delete allow "$PORT/tcp" >/dev/null || true
fi

echo "==> Удаляю $DIR (код, настройки, базу, сертификат)..."
cd /
rm -rf "$DIR"

echo "==> Готово: Трой удалён с сервера. Другие программы не затронуты."
