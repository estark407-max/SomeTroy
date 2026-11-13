#!/bin/bash
# Сборка Troy для Mac: программа Troy.app и образ Troy-mac-<процессор>-<версия>.dmg.
# Запускать на Mac (собрать программу для Mac в Windows нельзя):  bash build_mac.sh
# Нужен Python 3.10+ (python.org или `brew install python`). Сборка идёт 2–4 минуты.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
ARCH="$(uname -m)"; [ "$ARCH" = "x86_64" ] && ARCH="intel"

echo "[1/4] Готовлю окружение…"
[ -x .venv-mac/bin/python ] || "$PY" -m venv .venv-mac
.venv-mac/bin/python -m pip install -q --upgrade pip
.venv-mac/bin/python -m pip install -q "requests[socks]" pystray pillow pyinstaller paramiko python-dotenv tzdata pypdf

VER="$(.venv-mac/bin/python set_version.py)"
echo "      Версия: $VER ($ARCH)"

echo "[2/4] Значок…"
rm -rf build/troy.iconset && mkdir -p build/troy.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s ../art/icon-512.png --out build/troy.iconset/icon_${s}x${s}.png >/dev/null
  d=$((s * 2)); [ $d -le 512 ] && sips -z $d $d ../art/icon-512.png --out build/troy.iconset/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns build/troy.iconset -o build/troy.icns

echo "[3/4] Собираю Troy.app…"
rm -rf dist/Troy.app dist/Troy
.venv-mac/bin/python -m PyInstaller --noconfirm --clean --windowed --name Troy \
  --icon build/troy.icns --osx-bundle-identifier io.github.sometroy.troy \
  --hidden-import pystray._darwin --add-data "ui.html:." --add-data "favicon.png:." \
  --hidden-import mac_support --hidden-import pc_mac --hidden-import server_tools \
  --paths "../server" --hidden-import bot --hidden-import bitrix --hidden-import connectors \
  --hidden-import desktop_api --hidden-import backup \
  --hidden-import setup_wizard --hidden-import local_server --hidden-import socks --collect-data tzdata \
  --add-data "../server/bot.py:server_src" --add-data "../server/bitrix.py:server_src" \
  --add-data "../server/connectors.py:server_src" --add-data "../server/desktop_api.py:server_src" \
  --add-data "../server/backup.py:server_src" --add-data "../server/requirements.txt:server_src" \
  --add-data "../server/install.sh:server_src" --add-data "../server/uninstall.sh:server_src" \
  --add-data "../server/settings.env.example:server_src" troy_desktop.py > build-mac.log 2>&1 \
  || { echo "Сборка не удалась — смотри build-mac.log"; tail -20 build-mac.log; exit 1; }

# Трой живёт значком в строке меню (без значка в Dock); версия и пояснение для запросов разрешений
PL="dist/Troy.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PL" 2>/dev/null || /usr/libexec/PlistBuddy -c "Set :LSUIElement true" "$PL"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VER" "$PL"
/usr/libexec/PlistBuddy -c "Add :NSAppleEventsUsageDescription string 'Трой по вашей команде закрывает программы, выключает и перезагружает компьютер.'" "$PL" 2>/dev/null || true
codesign --force --deep -s - dist/Troy.app            # подпись «для себя»: без неё программа не запустится на Apple Silicon

echo "[4/4] Образ .dmg…"
rm -rf build/dmg && mkdir -p build/dmg
cp -R dist/Troy.app build/dmg/
ln -s /Applications build/dmg/Applications
cat > "build/dmg/Как открыть в первый раз.txt" <<TXT
1. Перетащи Troy в папку Applications («Программы»).
2. Открой Troy из «Программ».
3. macOS скажет, что не может проверить программу: у Troy нет платной подписи Apple.
   Открой: Системные настройки → Конфиденциальность и безопасность → внизу «Всё равно открыть».
   (В старых версиях macOS: правый щелчок по Troy → «Открыть» → «Открыть».)
4. Значок Троя появится в строке меню вверху экрана, а окно — в браузере Chrome, Edge или Яндекс
   (если их нет — во вкладке Safari).
TXT
DMG="Troy-mac-$ARCH-$VER.dmg"
rm -f "$DMG"
ok=""
for n in 1 2 3 4 5; do            # «Resource busy» бывает, пока система индексирует свежую папку, — повторяем
  if hdiutil create -volname "Troy $VER" -srcfolder build/dmg -ov -format UDZO "$DMG" >/dev/null 2>>build-mac.log; then ok=1; break; fi
  echo "      образ не собрался (попытка $n), повторяю…"; sleep 8
done
[ -n "$ok" ] || { echo "Образ .dmg не собрался — смотри build-mac.log"; exit 1; }
echo
echo "Готово: $(pwd)/$DMG"
