@echo off
chcp 65001 >nul
setlocal
title Обновление Троя на сервере

echo Обновление кода Троя: напоминания, история и настройки на сервере не трогаются.
echo.
set /p IP=IP-адрес вашего сервера: 
if "%IP%"=="" (
    echo Нужен IP-адрес сервера.
    pause
    exit /b 1
)
set "USR=root"

set "SRC=%~dp0"
set "SRC=%SRC:~0,-1%"
set "DST=/opt/sometroy"

echo.
echo Введите пароль сервера (раскладка английская):
rem Новый код сначала проверяется Python-ом сервера; не прошёл — прежняя версия остаётся работать
tar -cf - -C "%SRC%" bot.py bitrix.py desktop_api.py | ssh -o StrictHostKeyChecking=accept-new %USR%@%IP% "rm -rf /tmp/sometroy-up && mkdir -p /tmp/sometroy-up && tar -xf - -C /tmp/sometroy-up && if '%DST%/venv/bin/python' -m py_compile /tmp/sometroy-up/bot.py /tmp/sometroy-up/bitrix.py /tmp/sometroy-up/desktop_api.py; then cp /tmp/sometroy-up/*.py '%DST%/' && systemctl restart sometroy && sleep 5 && journalctl -u sometroy -n 10 --no-pager -o cat; else exit 7; fi"
if errorlevel 7 (
    echo.
    echo !! Новый код не прошёл проверку на сервере - оставил прежнюю рабочую версию.
    echo    Пришлите текст из этого окна.
    pause
    exit /b 1
)
echo.
echo Готово.
pause
