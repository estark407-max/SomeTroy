@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title Трой: починить связь с сервером

echo ============================================================
echo   Трой: починить связь программы с сервером
echo ============================================================
echo.
echo Что сделает: откроет на сервере порт 443, переключит на него Троя
echo и пропишет новый адрес в программе на этом компьютере.
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
powershell -NoProfile -Command "$t = Get-Content -Raw -Encoding UTF8 '%~f0'; $i = $t.LastIndexOf('#SERVER-SCRIPT#'); [Console]::Out.Write($t.Substring($i))" | ssh -o StrictHostKeyChecking=accept-new root@%IP% "tr -d '\r' | bash -s" > "%LOG%" 2>&1
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
    echo Не получилось — выше написано почему. Если сервер не принял пароль — сбрось его в панели хостинга.
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

echo Порт открыт. Прописываю новый адрес в программе и перезапускаю её...
taskkill /im SomeTroy.exe /f >nul 2>&1
timeout /t 2 /nobreak >nul
powershell -NoProfile -Command "$c = Get-Content -Raw '%CFG%' | ConvertFrom-Json; $c.server = 'https://%IP%:443'; $c | ConvertTo-Json -Compress | Set-Content -Encoding ASCII '%CFG%'"
if exist "%EXE%" start "" "%EXE%"
echo.
echo Готово! Трой снова на связи. Проверь: напиши ему /diag.

:end
echo.
pause
exit /b

#SERVER-SCRIPT#
cd /opt/sometroy 2>/dev/null || { echo NOTROY; exit 2; }
CUR=$(grep -E '^DESKTOP_PORT=' settings.env | cut -d= -f2 | tr -d ' ')
if [ "$CUR" != "443" ] && ss -ltnH 'sport = :443' | grep -q LISTEN; then echo BUSY443; exit 3; fi
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
    if ss -ltnH 'sport = :443' | grep -q LISTEN; then echo "Трой слушает порт 443"; echo TROY_OK; exit 0; fi
    sleep 2
done
echo "Трой не запустился. Последние строки журнала:"
journalctl -u sometroy -n 20 --no-pager
exit 4
