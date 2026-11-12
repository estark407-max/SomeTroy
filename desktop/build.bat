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

rem config.json и troy-cert.pem не обязательны: без них при первом запуске Трой откроет мастер настройки

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
".venv\Scripts\python.exe" -m pip install -q requests[socks] pystray pillow pyinstaller paramiko python-dotenv tzdata pypdf
if errorlevel 1 (
    echo Не удалось установить библиотеки. Проверьте интернет.
    pause
    exit /b 1
)
rem окно Троя на WebView2: pythonnet + файлы WebView2 из пакета pywebview
".venv\Scripts\python.exe" -m pip install -q pywebview pythonnet
".venv\Scripts\python.exe" -c "import webview,os;print(os.path.join(os.path.dirname(webview.__file__),'lib'))" > "%TEMP%\troy-wvlib.txt"
set /p WVLIB=<"%TEMP%\troy-wvlib.txt"
set "WVOPT="
if exist "%WVLIB%" set WVOPT=--add-data "%WVLIB%;webview\lib" --hidden-import clr --hidden-import shell_window --collect-all pythonnet --collect-all clr_loader
if not defined WVOPT echo        Не нашёл файлы WebView2 - окно будет по-старому, через Edge.

rem версия — из APP_VERSION в troy_desktop.py: попадает в свойства exe и в имя установщика
".venv\Scripts\python.exe" set_version.py > "%TEMP%\sometroy-ver.txt"
set /p VER=<"%TEMP%\sometroy-ver.txt"
if not defined VER set "VER=0.0.0"
echo        Версия: %VER%

echo [2/4] Собираю SomeTroy.exe...
rem --onedir (папка, а не один самораспаковывающийся exe): антивирус почти не путает с вредоносной программой
if exist "dist\SomeTroy" rmdir /s /q "dist\SomeTroy" >nul 2>&1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --windowed --name SomeTroy ^
    --version-file version.txt --icon troy.ico --hidden-import pystray._win32 --add-data "ui.html;." --add-data "favicon.png;." %WVOPT% ^
    --paths "..\server" --hidden-import bot --hidden-import bitrix --hidden-import connectors --hidden-import desktop_api --hidden-import backup ^
    --hidden-import setup_wizard --hidden-import local_server --hidden-import socks --collect-data tzdata ^
    --add-data "..\server\bot.py;server_src" --add-data "..\server\bitrix.py;server_src" --add-data "..\server\connectors.py;server_src" ^
    --add-data "..\server\desktop_api.py;server_src" --add-data "..\server\backup.py;server_src" --add-data "..\server\requirements.txt;server_src" ^
    --add-data "..\server\install.sh;server_src" --add-data "..\server\uninstall.sh;server_src" ^
    --add-data "..\server\settings.env.example;server_src" troy_desktop.py >build.log 2>&1
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
set "SETUP=SomeTroySetup-%VER%.exe"
if exist "%SETUP%" del /f /q "%SETUP%" >nul 2>&1
set "OK="
for %%N in (1 2 3 4) do (
    if not defined OK (
        if exist "%OUTTMP%" rmdir /s /q "%OUTTMP%" >nul 2>&1
        "%ISCC%" /Q /DAppVersion=%VER% /O"%OUTTMP%" troy.iss >>build.log 2>&1
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
copy /y "%OUTTMP%\%SETUP%" "%SETUP%" >nul
if errorlevel 1 (
    echo Не удалось скопировать установщик из %OUTTMP%
    pause
    exit /b 1
)

echo.
echo Готово: %~dp0%SETUP%
echo Его можно ставить и на другие компьютеры. Паролей и ключей внутри нет: при первом запуске
echo Трой сам проведёт настройку в своём окне.
echo.
set /p RUN=Установить Троя на этот компьютер сейчас? [Y/n]:
if /i not "%RUN%"=="n" start "" "%~dp0%SETUP%"
exit /b 0

:find_iscc
set "ISCC="
for %%P in ("%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do (
    if exist "%%~P" set "ISCC=%%~P"
)
if not defined ISCC for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do set "ISCC=%%I"
exit /b 0
