@echo off
rem окно не закроется само, даже если что-то пойдёт не так
if not "%~1"=="--run" (
    cmd /k ""%~f0" --run"
    exit /b
)
chcp 65001 >nul
setlocal EnableDelayedExpansion
title Трой: обновить и починить сервер

echo ============================================================
echo   Трой: обновить Троя на сервере и починить связь
echo ============================================================
echo.
echo Что сделает: обновит Троя на сервере до версии этой программы, откроет порт 443,
echo переключит на него Троя и пропишет новый адрес и сертификат в программе.
echo Переписка, напоминания и настройки не трогаются.
echo.

set "CFG=%LOCALAPPDATA%\SomeTroy\config.json"
set "EXE=%LOCALAPPDATA%\Programs\SomeTroy\SomeTroy.exe"
set "IP="
if exist "%CFG%" (
    for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "try { ([uri](Get-Content -Raw '%CFG%' | ConvertFrom-Json).server).Host } catch {}"`) do set "IP=%%i"
)
if defined IP (
    echo Сервер из настроек программы: !IP!
    set /p "NEWIP=Enter — оставить, или впиши другой IP: "
    if defined NEWIP set "IP=!NEWIP!"
) else (
    set /p "IP=IP-адрес сервера: "
)
if not defined IP (
    echo Не указан IP-адрес.
    goto :end
)

echo.
echo Сейчас попросит пароль root от сервера. При вводе символы не видны — так и должно быть.
echo Вставить пароль: правой кнопкой мыши, затем Enter.
echo.
set "LOG=%TEMP%\troy-fix.log"
rem свежий код Троя: из установленной программы или из папки проекта
set "SRC="
if exist "%~dp0..\_internal\server_src\bot.py" set "SRC=%~dp0..\_internal\server_src"
if not defined SRC if exist "%~dp0..\server\bot.py" set "SRC=%~dp0..\server"
set "PACK=%TEMP%\troyfix"
if exist "%PACK%" rmdir /s /q "%PACK%"
mkdir "%PACK%"
if defined SRC (
    copy /y "%SRC%\*.py" "%PACK%\" >nul
    copy /y "%SRC%\requirements.txt" "%PACK%\" >nul
    echo Код Троя для сервера: %SRC%
) else (
    echo Код Троя рядом не найден — сервер не обновляю, только чиню связь.
)
powershell -NoProfile -Command "$t = Get-Content -Raw -Encoding UTF8 '%~f0'; $i = $t.LastIndexOf('#SERVER-SCRIPT#'); [IO.File]::WriteAllText('%PACK%\fix.sh', $t.Substring($i).Replace([string][char]13, ''))"
tar -cf - -C "%PACK%" . | ssh -o StrictHostKeyChecking=accept-new root@%IP% "rm -rf /tmp/troyfix && mkdir -p /tmp/troyfix && tar -xf - -C /tmp/troyfix && bash /tmp/troyfix/fix.sh %IP%" > "%LOG%" 2>&1
type "%LOG%"
echo.

findstr /c:"NOTROY" "%LOG%" >nul && (
    echo Трой на этом сервере не найден ^(/opt/sometroy^). Проверь IP-адрес.
    goto :end
)
findstr /c:"BUSY443" "%LOG%" >nul && (
    echo Порт 443 на сервере занят другой программой ^(например, сайтом^).
    echo Открой в панели хостинга входящий порт 8443 ^(TCP^) — тогда всё заработает без этого скрипта.
    goto :end
)
findstr /c:"TROY_OK" "%LOG%" >nul || (
    echo Не получилось — выше написано почему ^(пришли это разработчику^). Если сервер не принял пароль — сбрось его в панели хостинга.
    goto :end
)

echo Сервер готов. Проверяю, открыт ли порт 443 снаружи...
for /f "usebackq delims=" %%r in (`powershell -NoProfile -Command "(Test-NetConnection '%IP%' -Port 443 -WarningAction SilentlyContinue).TcpTestSucceeded"`) do set "OPEN=%%r"
if /i not "%OPEN%"=="True" (
    echo.
    echo Порт 443 закрыт в панели хостинга. Открой там входящий порт 443 ^(TCP^)
    echo ^(раздел «Файрвол», «Сеть» или «Группы безопасности»^) и запусти этот файл ещё раз.
    goto :end
)

echo Порт открыт. Прописываю новый адрес и сертификат сервера в программе и перезапускаю её...
taskkill /im SomeTroy.exe /f >nul 2>&1
timeout /t 2 /nobreak >nul
powershell -NoProfile -Command "$c = Get-Content -Raw '%CFG%' | ConvertFrom-Json; $c.server = 'https://%IP%:443'; $c | ConvertTo-Json -Compress | Set-Content -Encoding ASCII '%CFG%'"
powershell -NoProfile -Command "$t = Get-Content -Raw '%LOG%'; $m = [regex]::Match($t, '(?s)CERT-BEGIN\s*(-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----)'); if ($m.Success) { Set-Content -Encoding ASCII -NoNewline -Path '%LOCALAPPDATA%\SomeTroy\troy-cert.pem' -Value ($m.Groups[1].Value.Replace([string][char]13,'') + [string][char]10) }"
if exist "%EXE%" start "" "%EXE%"
echo.
echo Готово. Трой снова на связи. Проверь: напиши ему /diag.

:end
echo.
echo Журнал этого запуска: %TEMP%\troy-fix.log
echo Окно можно закрыть.
exit /b

#SERVER-SCRIPT#
cd /opt/sometroy 2>/dev/null || { echo NOTROY; exit 2; }
# 1) свежий код Троя из программы — проверяем, что он собирается, и кладём на место
if [ -f /tmp/troyfix/bot.py ]; then
    if venv/bin/python -m py_compile /tmp/troyfix/*.py; then
        cp /tmp/troyfix/*.py /opt/sometroy/ && cp /tmp/troyfix/requirements.txt /opt/sometroy/
        venv/bin/pip install -q -r requirements.txt || echo "pip: не все библиотеки поставились (см. выше)"
        echo "Обновил Троя на сервере до версии $(grep -o 'SERVER_VERSION = \"[0-9.]*' bot.py | grep -o '[0-9.]*$')"
    else
        echo "Свежий код не собирается на этом сервере — оставляю прежний"
    fi
fi
CUR=$(grep -E '^DESKTOP_PORT=' settings.env | cut -d= -f2 | tr -d ' ')
if [ "$CUR" != "443" ] && ss -ltnH 'sport = :443' | grep -q LISTEN; then echo BUSY443; exit 3; fi
# Python 3.8 (Ubuntu 20.04): в нём нет zoneinfo — ставим отдельным пакетом и чиним импорт
if ! venv/bin/python -c 'import zoneinfo' 2>/dev/null; then
    echo "Python на сервере: $(venv/bin/python -V 2>&1). Добавляю недостающую библиотеку часовых поясов..."
    venv/bin/pip install -q backports.zoneinfo || { echo "Не смог поставить backports.zoneinfo"; exit 5; }
    for f in bot.py connectors.py; do
        grep -q 'backports.zoneinfo' $f || sed -i 's/^from zoneinfo import ZoneInfo$/try:\n    from zoneinfo import ZoneInfo\nexcept ImportError:\n    from backports.zoneinfo import ZoneInfo/' $f
    done
    grep -q backports requirements.txt || echo 'backports.zoneinfo; python_version < "3.9"' >> requirements.txt
fi
IP_ARG="$1"
# сертификат: один набор расширений и правильный IP (на старом OpenSSL расширения дублировались — программа ему не верит)
DUPS=$(openssl x509 -in tls/cert.pem -noout -text | grep -c 'X509v3 Basic Constraints')
echo "Сертификат сервера: $(openssl x509 -in tls/cert.pem -noout -subject 2>&1); Basic Constraints: $DUPS; $(openssl x509 -in tls/cert.pem -noout -text | grep -o 'IP Address:[0-9.]*' | tr '\n' ' ')"
if [ "$DUPS" -gt 1 ] || { [ -n "$IP_ARG" ] && ! openssl x509 -in tls/cert.pem -noout -text | grep -q "IP Address:$IP_ARG"; }; then
    echo "Пересоздаю сертификат сервера (IP $IP_ARG)..."
    CNF=$(mktemp)
    printf '[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=Troy\n[ext]\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\nextendedKeyUsage=serverAuth\nsubjectAltName=IP:%s\n' "$IP_ARG" > "$CNF"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -sha256 -keyout tls/key.pem -out tls/cert.pem -config "$CNF" 2>/dev/null && chmod 600 tls/key.pem
    rm -f "$CNF"
fi
P=443
(command -v ufw >/dev/null && ufw status | grep -q 'Status: active' && ufw allow $P/tcp >/dev/null && echo "ufw: открыл $P")
(command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1 && firewall-cmd -q --permanent --add-port=$P/tcp && firewall-cmd -q --reload && echo "firewalld: открыл $P")
if command -v iptables >/dev/null && iptables -S INPUT | grep -qE '^-P INPUT (DROP|REJECT)|-j (DROP|REJECT)' && ! iptables -C INPUT -p tcp --dport $P -j ACCEPT 2>/dev/null; then
    iptables -I INPUT -p tcp --dport $P -j ACCEPT && echo "iptables: открыл $P"
    netfilter-persistent save >/dev/null 2>&1 || { [ -d /etc/iptables ] && iptables-save > /etc/iptables/rules.v4; }
fi
if grep -q '^DESKTOP_PORT=' settings.env; then sed -i 's/^DESKTOP_PORT=.*/DESKTOP_PORT=443/' settings.env; else echo 'DESKTOP_PORT=443' >> settings.env; fi
systemctl restart sometroy
for i in 1 2 3 4 5 6 7 8 9 10; do
    if ss -ltnH 'sport = :443' | grep -q LISTEN; then
        echo "Трой слушает порт 443"
        echo CERT-BEGIN; cat tls/cert.pem; echo CERT-END
        echo TROY_OK; exit 0
    fi
    sleep 2
done
echo "Трой не запустился. Последние строки журнала:"
journalctl -u sometroy -n 20 --no-pager
exit 4
