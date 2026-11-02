#!/usr/bin/env bash
# Установка ассистента на сервер (Ubuntu/Debian) и запуск как службы systemd.
# Запускается автоматически из deploy.bat; вручную: bash install.sh
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
SERVICE=sometroy
echo "==> Папка бота: $DIR"

echo "==> Устанавливаю Python (если ещё нет)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip >/dev/null

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))'; then
    echo "!! На сервере Python $(python3 -V 2>&1 | cut -d' ' -f2) — Трою нужен 3.8 или новее (Ubuntu 20.04+, Debian 11+)."
    exit 1
fi

echo "==> Устанавливаю библиотеки бота..."
python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

if [ ! -f settings.env ]; then
    echo "!! Нет файла settings.env — заполните его и запустите снова."
    exit 1
fi
sed -i 's/\r$//' settings.env  # убираем виндовые переводы строк

# Сертификат для программы на компьютере: создаётся один раз, на IP сервера
PUBLIC_IP="${1:-$(hostname -I | awk '{print $1}')}"
PORT="$(grep -E '^DESKTOP_PORT=' settings.env | cut -d= -f2)"; PORT="${PORT:-8443}"
mkdir -p tls
CERT_DUPS="$( [ -f tls/cert.pem ] && openssl x509 -in tls/cert.pem -noout -text | grep -c 'X509v3 Basic Constraints' || echo 0)"
if [ ! -f tls/cert.pem ] || ! openssl x509 -in tls/cert.pem -noout -text | grep -q "IP Address:$PUBLIC_IP" || [ "$CERT_DUPS" -gt 1 ]; then
    echo "==> Создаю сертификат для программы на компьютере (IP $PUBLIC_IP)..."
    CNF="$(mktemp)"
    printf '[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=Troy\n[ext]\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\nextendedKeyUsage=serverAuth\nsubjectAltName=IP:%s\n' "$PUBLIC_IP" > "$CNF"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -sha256 -keyout tls/key.pem -out tls/cert.pem -config "$CNF" 2>/dev/null
    rm -f "$CNF"
    chmod 600 tls/key.pem
fi
# Открываем порт для программы во всех файрволах сервера, какие есть (файрвол в панели хостинга отсюда не виден)
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    ufw allow "$PORT/tcp" >/dev/null && echo "==> Открыл порт $PORT в ufw"
fi
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd -q --permanent --add-port="$PORT/tcp" && firewall-cmd -q --reload && echo "==> Открыл порт $PORT в firewalld"
fi
if command -v iptables >/dev/null 2>&1; then
    if iptables -S INPUT 2>/dev/null | grep -qE '^-P INPUT (DROP|REJECT)|-j (DROP|REJECT)'; then
        if ! iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null; then
            iptables -I INPUT -p tcp --dport "$PORT" -j ACCEPT && echo "==> Открыл порт $PORT в iptables"
            if command -v netfilter-persistent >/dev/null 2>&1; then netfilter-persistent save >/dev/null 2>&1 || true
            elif [ -d /etc/iptables ]; then iptables-save > /etc/iptables/rules.v4 || true; fi
        fi
    fi
fi

echo "==> Проверяю ключи и доступ к сервисам..."
venv/bin/python bot.py --check || echo "!! Проверка нашла проблемы (см. выше). Бот всё равно будет запущен."

echo "==> Настраиваю автозапуск..."
cat > /etc/systemd/system/$SERVICE.service <<EOF
[Unit]
Description=SomeTroy personal assistant
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart="$DIR/venv/bin/python" "$DIR/bot.py"
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
MemoryMax=250M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable $SERVICE >/dev/null 2>&1
systemctl restart $SERVICE
sleep 6

echo
if systemctl is-active --quiet $SERVICE; then
    echo "==> ГОТОВО: ассистент работает. Напишите ему в Telegram."
else
    echo "!! Бот не запустился. Последние строки лога:"
fi
echo "---------------- лог ----------------"
journalctl -u $SERVICE -n 15 --no-pager -o cat || true
echo "-------------------------------------"
echo "Лог:          journalctl -u $SERVICE -f"
echo "Перезапуск:   systemctl restart $SERVICE"
echo "Память:       systemctl status $SERVICE"
