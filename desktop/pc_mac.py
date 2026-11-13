"""Команды Троя на компьютере Mac: программы, музыка и громкость, блокировка, сон, выключение.

Всё штатными средствами macOS:
- открыть программу — `open -a` по списку программ из папок «Программы»;
- закрыть программу — вежливо, как Cmd+Q (программа успеет спросить про несохранённое);
- громкость — через системную громкость; пауза и переключение треков — медиаклавиши;
- выключение и перезагрузка — окно с отсчётом 60 секунд и кнопкой «Отмена».
macOS может один раз спросить разрешение: «Трой хочет управлять…» — без него закрывать программы
и выключать компьютер не получится.
"""
import difflib
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import pc_control as pc

log = logging.getLogger("troy.pc")

# Как люди называют программы → как они называются на Mac
ALIASES = {
    "проводник": "finder", "файндер": "finder", "файлы": "finder", "блокнот": "textedit", "заметки": "notes",
    "калькулятор": "calculator", "параметры": "system settings", "настройки": "system settings",
    "системные настройки": "system settings", "терминал": "terminal", "командная строка": "terminal",
    "сафари": "safari", "почта": "mail", "календарь": "calendar", "музыка": "music", "фото": "photos",
    "сообщения": "messages", "карты": "maps", "диспетчер задач": "activity monitor",
    "мониторинг системы": "activity monitor", "просмотр": "preview", "эдж": "microsoft edge", "edge": "microsoft edge",
    "хром": "google chrome", "chrome": "google chrome", "ворд": "microsoft word", "word": "microsoft word",
    "эксель": "microsoft excel", "иксель": "microsoft excel", "excel": "microsoft excel",
    "пауэрпоинт": "microsoft powerpoint", "powerpoint": "microsoft powerpoint", "аутлук": "microsoft outlook",
    "outlook": "microsoft outlook", "вскод": "visual studio code", "vs code": "visual studio code",
    "зум": "zoom.us", "zoom": "zoom.us",
}
# названия программ macOS по-русски (в папке они лежат под английскими именами)
RU_NAMES = {"finder": "Finder", "textedit": "TextEdit", "notes": "Заметки", "calculator": "Калькулятор",
            "system settings": "Системные настройки", "terminal": "Терминал", "activity monitor": "Мониторинг системы"}
WINDOWS_ONLY = {"edge", "notepad", "explorer", "cmd", "settings", "terminal", "calculator", "task manager", "paint",
                "word", "excel", "powerpoint", "outlook", "zoom", "chrome"}
NEVER_CLOSE = {"troy", "sometroy", "master troy", "finder", "dock", "systemuiserver", "loginwindow", "windowserver",
               "controlcenter", "notificationcenter", "spotlight"}
APP_DIRS = ["/Applications", "/System/Applications", "/System/Applications/Utilities", "/Applications/Utilities",
            "/System/Library/CoreServices/Applications", str(Path.home() / "Applications")]
_cache = {"t": 0.0, "apps": {}}


def osa(script: str, timeout: int = 20) -> tuple:
    """Выполнить AppleScript. (получилось, вывод или ошибка)"""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout if r.returncode == 0 else r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def apps() -> dict:
    """Программы на этом Mac: имя (как ищем) → (название, путь). Кэш на 10 минут."""
    if time.time() - _cache["t"] < 600 and _cache["apps"]:
        return _cache["apps"]
    found = {}
    for root in APP_DIRS:
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            items = list(base.glob("*.app")) + list(base.glob("*/*.app"))
        except OSError:
            continue
        for p in items:
            found.setdefault(pc.norm(p.stem), (p.stem, str(p)))
    finder = Path("/System/Library/CoreServices/Finder.app")
    if finder.exists():
        found.setdefault("finder", ("Finder", str(finder)))
    _cache.update(t=time.time(), apps=found)
    return found


def default_browser() -> str:
    """Браузер по умолчанию — «открой браузер» откроет именно его."""
    try:
        r = subprocess.run(["defaults", "read", "com.apple.LaunchServices/com.apple.launchservices.secure", "LSHandlers"],
                           capture_output=True, text=True, timeout=5).stdout.lower()
        i = r.find("lshandlerurlscheme = https")
        block = r[max(0, i - 400):i + 100] if i >= 0 else ""
        for mark, name in (("chrome", "google chrome"), ("firefox", "firefox"), ("yandex", "yandex"), ("opera", "opera"),
                           ("brave", "brave browser"), ("edgemac", "microsoft edge"), ("safari", "safari")):
            if mark in block:
                return name
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "safari"


def canon(name: str) -> str:
    n = pc.norm(name)
    if n in ("браузер", "браузера", "браузеру", "браузере", "browser", "интернет"):
        return default_browser()
    if n in ALIASES:
        return ALIASES[n]
    st = pc.stem(n)
    for k, v in ALIASES.items():
        if pc.stem(k) == st:
            return v
    for k, v in pc.ALIASES.items():                      # общий словарь («телега» → telegram), кроме чисто виндовых имён
        if (k == n or pc.stem(k) == st) and v not in WINDOWS_ONLY:
            return v
    return n


def open_app(name: str):
    q = canon(name)
    have = apps()
    hit = pc.best_match(q, have) or pc.best_match(pc.norm(name), have)
    if not hit:
        near = find_apps(name, 3)
        return False, f"не нашёл программу «{name}»" + (f". Похожие: {', '.join(near)}" if near else "")
    title, path = have[hit]
    r = subprocess.run(["open", "-a", path], capture_output=True, text=True)
    if r.returncode != 0:
        return False, f"не смог открыть «{title}»: {r.stderr.strip()[:120]}"
    return True, f"открыл «{RU_NAMES.get(hit, title)}»"


def find_apps(word: str, limit: int = 15) -> list:
    q, st = pc.norm(word), pc.stem(pc.norm(word))
    have = apps()
    found = sorted(n for n in have if q in n or st in pc.stem(n))
    if not found:
        found = difflib.get_close_matches(q, list(have), n=limit, cutoff=0.5)
    return [have[n][0] for n in found[:limit]]


def running() -> dict:
    """Запущенные программы (те, что из папок .app): имя (как ищем) → название."""
    out = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True).stdout
    procs = {}
    for line in out.splitlines():
        i = line.find(".app/Contents/MacOS/")
        if i < 0 or ".app/Contents/" in line[:i]:        # только сама программа, не её помощники внутри пакета
            continue
        title = line[:i].rsplit("/", 1)[-1]
        procs.setdefault(pc.norm(title), title)
    return procs


def close_app(name: str):
    c = canon(name)
    procs = running()
    hit = pc.best_match(c, procs) or pc.best_match(pc.norm(name), procs)
    if not hit and len(c.split()) > 1:
        last = c.split()[-1]
        if len(last) >= 4 and not last.isdigit():
            hit = pc.best_match(last, procs)
    if not hit:
        return False, f"«{name}» сейчас не запущена"
    if hit in NEVER_CLOSE:
        return False, f"«{procs[hit]}» закрывать нельзя — это часть macOS или сам Трой"
    title = procs[hit].replace('"', "")
    ok, out = osa(f'tell application "{title}" to quit', 15)
    if ok:
        return True, f"закрыл «{title}» (если были несохранённые файлы — программа спросит)"
    if "-1743" in out or "not allowed" in out.lower():
        return False, ("macOS не разрешила Трою управлять программами. Разреши: Системные настройки → "
                       "Конфиденциальность и безопасность → Автоматизация → Troy")
    return False, f"«{title}» не закрылась сама — возможно, ждёт ответа в своём окне"


# ── музыка и громкость ──
def media_key(key: int) -> bool:
    """Медиаклавиша (16 — пауза/продолжить, 17 — следующий, 18 — предыдущий), как на клавиатуре."""
    try:
        import Quartz
        for down in (True, False):
            ev = Quartz.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                14, (0, 0), 0xA00 if down else 0xB00, 0, 0, 0, 8, (key << 16) | ((0xA if down else 0xB) << 8), -1)
            Quartz.CGEventPost(0, ev.CGEvent())
        return True
    except Exception as e:
        log.warning("Медиаклавиша: %s", e)
        return False


def volume(delta: int = 0, mute: bool = False) -> tuple:
    if mute:
        ok, out = osa("set m to output muted of (get volume settings)\nset volume output muted (not m)\n"
                      "return output muted of (get volume settings)")
        return ok, ("звук выключен" if out == "true" else "звук включён") if ok else f"не получилось: {out[:100]}"
    ok, out = osa(f"set v to (output volume of (get volume settings)) + ({delta})\nif v > 100 then set v to 100\n"
                  "if v < 0 then set v to 0\nset volume output volume v\nreturn v")
    return ok, (f"громкость {out}%" if ok else f"не получилось: {out[:100]}")


def power_countdown(kind: str, report=None) -> None:
    """Окно с отсчётом 60 с: «ОК» — сразу, «Отмена» — отменить; молчание — выполняем."""
    what = "выключит компьютер" if kind == "shutdown" else "перезагрузит компьютер"
    ok, out = osa(f'display dialog "Трой {what} через 60 секунд.\\n\\nОК — сделать это сейчас.\\nОтмена — отменить." '
                  'with title "Трой" buttons {"Отмена", "ОК"} default button "ОК" cancel button "Отмена" '
                  'with icon caution giving up after 60', 90)
    if not ok:                                         # «Отмена» (или окно показать не удалось) — не выключаем
        log.info("Выключение отменено на компьютере: %s", out[:80])
        if report:
            report(False, ("выключение" if kind == "shutdown" else "перезагрузку") + " отменили на компьютере")
        return
    if report:
        report(True, "выключаю сейчас" if kind == "shutdown" else "перезагружаю сейчас")
    ok, out = osa(f'tell application "System Events" to {"shut down" if kind == "shutdown" else "restart"}', 20)
    if not ok:
        log.warning("Питание: %s", out)


def execute(action: str, arg: str = "", report=None):
    """То же, что pc_control.execute, но для Mac. Возвращает (успех, текст для владельца)."""
    if action == "play_pause":
        return (True, pc.MEDIA_TEXT[action]) if media_key(16) else (False, "не получилось нажать «пауза»")
    if action in ("next_track", "prev_track"):
        ok = media_key(17 if action == "next_track" else 18)
        return (True, pc.MEDIA_TEXT[action]) if ok else (False, "не получилось переключить трек")
    if action in ("volume_up", "volume_down"):
        return volume(10 if action == "volume_up" else -10)
    if action == "mute":
        return volume(mute=True)
    if action == "open_app":
        return pc.several(open_app, arg)
    if action == "find_app":
        found = find_apps(arg)
        return True, ("вижу программы: " + ", ".join(found)) if found else f"не вижу программ со словом «{arg}»"
    if action == "close_app":
        return pc.several(close_app, arg)
    if action == "lock":
        subprocess.run(["pmset", "displaysleepnow"], capture_output=True)
        return True, "экран погашен (заблокируется, если в настройках включён пароль после сна)"
    if action == "sleep":
        def go():
            time.sleep(3)   # сначала успеваем отправить ответ
            subprocess.run(["pmset", "sleepnow"], capture_output=True)
        threading.Thread(target=go, daemon=True).start()
        return True, "ухожу в сон"
    if action in ("shutdown", "restart"):
        threading.Thread(target=power_countdown, args=(action, report), daemon=True).start()
        return True, ("выключится" if action == "shutdown" else "перезагрузится") + \
            " через 60 секунд (на компьютере можно нажать «Отмена»)"
    return False, f"не знаю команду {action}"
