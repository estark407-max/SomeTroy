@echo off
chcp 65001 >nul
setlocal
title Установка Троя на сервер

echo ============================================================
echo   Установка Троя на сервер
echo ============================================================
echo.
echo Другие программы на сервере не затрагиваются.
echo.
set /p IP=IP-адрес вашего сервера: 
if "%IP%"=="" (
    echo Нужен IP-адрес сервера.
    pause
    exit /b 1
)
set /p USR=Пользователь [root]:
if "%USR%"=="" set "USR=root"

set "SRC=%~dp0"
set "SRC=%SRC:~0,-1%"
set "DST=/opt/sometroy"

if not exist "%SRC%\settings.env" (
    echo Нет файла settings.env: скопируйте settings.env.example в settings.env и впишите ключи.
    pause
    exit /b 1
)
set "DESK=%SRC%\..\desktop"
if not exist "%DESK%" mkdir "%DESK%"

echo.
echo Копирую файлы, устанавливаю и запускаю.
echo Раскладка английская; пароль сервера спросят один раз (при вводе не виден).
echo.
tar -cf - -C "%SRC%" bot.py bitrix.py desktop_api.py requirements.txt install.sh settings.env | ssh -o StrictHostKeyChecking=accept-new %USR%@%IP% "mkdir -p '%DST%' && tar -xf - -C '%DST%' && bash '%DST%/install.sh' %IP% 1>&2 && cat '%DST%/tls/cert.pem'" > "%DESK%\troy-cert.pem"
if errorlevel 1 (
    echo.
    echo Установка завершилась с ошибкой - пришлите текст из этого окна.
    pause
    exit /b 1
)

rem адрес сервера для программы на компьютере
> "%DESK%\config.json" echo {"server": "https://%IP%:8443"}

findstr /c:"BEGIN CERTIFICATE" "%DESK%\troy-cert.pem" >nul
if errorlevel 1 (
    echo.
    echo Бот установлен, но сертификат для программы на компьютере не получен.
) else (
    echo.
    echo Сертификат и адрес сервера для программы сохранены в папке desktop.
)
echo.
echo Всё. Трой работает.
pause
