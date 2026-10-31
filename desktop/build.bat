@echo off
chcp 65001 >nul
setlocal
title Сборка установщика Трой
cd /d "%~dp0"

echo ============================================================
echo   Сборка установщика Трой (3-5 минут)
echo ============================================================
echo.

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo Не найден Python. Установите его с python.org ^(галочка "Add python.exe to PATH"^)
    echo и запустите build.bat снова.
    pause
    exit /b 1
)

if not exist "config.json" (
    echo Нет config.json в папке desktop.
    pause
    exit /b 1
)
if not exist "troy-cert.pem" (
    echo Нет troy-cert.pem - сначала запустите server\deploy.bat, он его скачает.
    pause
    exit /b 1
)

rem Защитник Windows иногда удаляет свежесобранный SomeTroy.exe — предлагаем исключение для папки сборки
set "DEF="
powershell -NoProfile -Command "if ((Get-MpPreference).ExclusionPath -contains '%~dp0dist') { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 (
    set "DEF=Y"
    set /p DEF=Добавить папку сборки в исключения Защитника Windows? Попросит права администратора [Y/n]: 
)
if /i "%DEF%"=="Y" (
    if not exist "%~dp0dist" mkdir "%~dp0dist"
    powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '-NoProfile','-Command','Add-MpPreference -ExclusionPath ''%~dp0dist'''" >nul 2>&1
    if errorlevel 1 (echo        Не получилось - продолжаю без исключения.) else (echo        Папка сборки добавлена в исключения.)
)

echo [1/4] Готовлю окружение...
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q requests pystray pillow pyinstaller
if errorlevel 1 (
    echo Не удалось установить библиотеки. Проверьте интернет.
    pause
    exit /b 1
)

echo [2/4] Собираю SomeTroy.exe...
rem --onedir (папка, а не один самораспаковывающийся exe): антивирус почти не путает с вредоносной программой
if exist "dist\SomeTroy" rmdir /s /q "dist\SomeTroy" >nul 2>&1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --windowed --name SomeTroy ^
    --version-file version.txt --icon troy.ico --hidden-import pystray._win32 --add-data "ui.html;." --add-data "favicon.png;." troy_desktop.py >build.log 2>&1
if errorlevel 1 (
    echo Сборка не удалась. Пришлите файл build.log из папки desktop.
    pause
    exit /b 1
)

echo [3/4] Ищу Inno Setup (сборщик установщиков)...
call :find_iscc
if not defined ISCC (
    echo        Не найден - ставлю бесплатно через winget, 1-2 минуты...
    winget install --id JRSoftware.InnoSetup -e --scope user --silent --accept-source-agreements --accept-package-agreements >nul 2>&1
    call :find_iscc
)
if not defined ISCC (
    winget install --id JRSoftware.InnoSetup -e --silent --accept-source-agreements --accept-package-agreements >nul 2>&1
    call :find_iscc
)
if not defined ISCC (
    echo Не удалось поставить Inno Setup. Скачайте его с jrsoftware.org/isdl.php,
    echo установите и запустите build.bat снова.
    pause
    exit /b 1
)

echo [4/4] Собираю установщик...
rem Собираем во временной папке и при неудаче повторяем: антивирус иногда держит свежий файл
set "OUTTMP=%TEMP%\SomeTroySetupBuild"
if exist "SomeTroySetup.exe" del /f /q "SomeTroySetup.exe" >nul 2>&1
set "OK="
for %%N in (1 2 3 4) do (
    if not defined OK (
        if exist "%OUTTMP%" rmdir /s /q "%OUTTMP%" >nul 2>&1
        "%ISCC%" /Q /O"%OUTTMP%" troy.iss >>build.log 2>&1
        if not errorlevel 1 (
            set "OK=1"
        ) else (
            echo        попытка %%N не удалась, повторяю...
            timeout /t 5 /nobreak >nul
        )
    )
)
if not defined OK (
    echo Установщик не собрался. Пришлите файл build.log из папки desktop.
    pause
    exit /b 1
)
copy /y "%OUTTMP%\SomeTroySetup.exe" "SomeTroySetup.exe" >nul
if errorlevel 1 (
    echo Не удалось скопировать установщик из %OUTTMP%
    pause
    exit /b 1
)

echo.
echo Готово: %~dp0SomeTroySetup.exe
echo Его можно ставить и на другие компьютеры. Паролей внутри нет: при первом запуске
echo Трой спросит пароль, который ты задаёшь в Telegram командой /password.
echo.
set /p RUN=Установить Троя на этот компьютер сейчас? [Y/n]:
if /i not "%RUN%"=="n" start "" "%~dp0SomeTroySetup.exe"
exit /b 0

:find_iscc
set "ISCC="
for %%P in ("%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do (
    if exist "%%~P" set "ISCC=%%~P"
)
if not defined ISCC for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do set "ISCC=%%I"
exit /b 0
