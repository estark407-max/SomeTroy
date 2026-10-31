"""Выполнение команд Троя на этом компьютере (Windows): питание, программы, музыка и громкость.

Всё штатными средствами Windows, без сторонних программ:
- выключение/перезагрузка — shutdown.exe с отсчётом 60 с и окном «Отмена»;
- открыть программу — по списку приложений меню «Пуск» (Get-StartApps: обычные программы и из Microsoft Store);
- закрыть программу — вежливо, как крестиком (taskkill без /F), чтобы она успела предложить сохранить;
- музыка и громкость — медиаклавиши, работают с любым плеером.
"""
import csv
import difflib
import io
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("troy.pc")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

MEDIA_KEYS = {"play_pause": 0xB3, "next_track": 0xB0, "prev_track": 0xB1,
              "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD}
MEDIA_TEXT = {"play_pause": "пауза / продолжить", "next_track": "следующий трек", "prev_track": "предыдущий трек",
              "volume_up": "громкость +10%", "volume_down": "громкость −10%", "mute": "звук выключен / включён"}

# Как люди называют программы → как они называются в Пуске / процессах
ALIASES = {
    "хром": "chrome", "гугл хром": "google chrome", "гугл": "google chrome", "телеграм": "telegram",
    "телеграмм": "telegram", "телега": "telegram", "блокнот": "notepad", "проводник": "explorer",
    "калькулятор": "calculator", "ворд": "word", "эксель": "excel", "иксель": "excel", "пауэрпоинт": "powerpoint",
    "стим": "steam", "дискорд": "discord", "спотифай": "spotify", "браузер": "edge", "эдж": "edge",
    "фотошоп": "photoshop", "зум": "zoom", "скайп": "skype", "ватсап": "whatsapp", "вотсап": "whatsapp",
    "обс": "obs", "аимп": "aimp", "винамп": "winamp", "рекордбокс": "rekordbox", "файрфокс": "firefox",
    "опера": "opera", "яндекс браузер": "yandex", "яндекс": "yandex", "вайбер": "viber", "outlook": "outlook",
    "аутлук": "outlook", "диспетчер задач": "task manager", "параметры": "settings", "настройки": "settings",
    "пейнт": "paint", "командная строка": "cmd", "терминал": "terminal", "vs code": "visual studio code",
    "вскод": "visual studio code", "битрикс": "bitrix24", "битрикс24": "bitrix24",
    "яндекс музыку": "яндекс музыка", "я музыка": "яндекс музыка", "яндекс музыке": "яндекс музыка",
    "музыку яндекс": "яндекс музыка", "yandex music": "яндекс музыка", "кинопоиск": "кинопоиск",
}
BUILTIN = {"notepad": "notepad.exe", "calculator": "calc.exe", "explorer": "explorer.exe",
           "task manager": "taskmgr.exe", "settings": "ms-settings:", "paint": "mspaint.exe", "cmd": "cmd.exe",
           "terminal": "wt.exe", "edge": "msedge.exe"}
# Имя процесса, если оно не похоже на название программы
PROCESS = {"word": "winword", "powerpoint": "powerpnt", "google chrome": "chrome", "edge": "msedge",
           "visual studio code": "code", "calculator": "calculatorapp", "task manager": "taskmgr",
           "yandex": "browser", "bitrix24": "bitrix24", "яндекс музыка": "яндекс музыка"}
# Дополнительные имена процессов (у приложений из Store они часто английские)
PROCESS_EXTRA = {"яндекс музыка": ["yandex.music", "yandexmusic", "yandex music"], "telegram": ["telegram desktop"],
                 "spotify": ["spotify"], "whatsapp": ["whatsapp", "whatsapp.root"]}
NEVER_CLOSE = {"troy", "explorer", "csrss", "winlogon", "system", "svchost", "lsass", "dwm", "services", "smss"}


def norm(s: str) -> str:
    return " ".join(s.lower().replace("ё", "е").replace(".exe", "").split())


def default_browser() -> str:
    """Какой браузер в Windows выбран по умолчанию: «открой браузер» откроет именно его."""
    try:
        import winreg
        key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            prog = str(winreg.QueryValueEx(k, "ProgId")[0]).lower()
    except Exception:
        return "edge"
    for mark, name in (("chrome", "google chrome"), ("firefox", "firefox"), ("yandex", "yandex"), ("opera", "opera"),
                       ("brave", "brave"), ("vivaldi", "vivaldi"), ("msedge", "edge")):
        if mark in prog:
            return name
    return "edge"


def canon(name: str) -> str:
    n = norm(name)
    if n in ("браузер", "браузера", "браузеру", "браузере", "browser", "интернет"):
        return default_browser()
    if n in ALIASES:
        return ALIASES[n]
    st = stem(n)                       # «телегу», «хрома», «ворде» → как в словаре
    for k, v in ALIASES.items():
        if stem(k) == st:
            return v
    return n


def stem(text: str) -> str:
    """Грубо убираем падежные окончания: «яндекс музыку» ~ «яндекс музыка», «телегу» ~ «телега»."""
    out = []
    for w in text.split():
        while len(w) > 4 and w[-1] in "аяуюеиыоьйём":
            w = w[:-1]
        out.append(w)
    return " ".join(out)


# ── ярлыки меню «Пуск» ──
def start_menu() -> dict:
    roots = [Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
             Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs"]
    links = {}
    for root in roots:
        if root.exists():
            for p in root.rglob("*.lnk"):
                stem = norm(p.stem)
                if "uninstall" in stem or "удален" in stem:
                    continue
                links.setdefault(stem, p)
    return links


def best_match(query: str, names) -> str:
    names = list(names)
    hit = _best(query, names)
    if hit:
        return hit
    stems = {stem(n): n for n in names}      # вторая попытка — без окончаний
    hit = _best(stem(query), list(stems))
    return stems[hit] if hit else ""


def _best(query: str, names: list) -> str:
    if not query:
        return ""
    if query in names:
        return query
    starts = [n for n in names if n.startswith(query)]
    if starts:
        return min(starts, key=len)
    contains = [n for n in names if query in n]
    if contains:
        return min(contains, key=len)
    close = difflib.get_close_matches(query, names, n=1, cutoff=0.72)
    return close[0] if close else ""


_apps_cache = {"t": 0.0, "apps": {}}


def start_apps() -> dict:
    """Все приложения меню «Пуск», включая Microsoft Store: имя → AppID (кэш 10 минут)."""
    if time.time() - _apps_cache["t"] < 600 and _apps_cache["apps"]:
        return _apps_cache["apps"]
    apps = {}
    try:
        cmd = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
               "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress")
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                             timeout=20, creationflags=NO_WINDOW).stdout.decode("utf-8", "replace").strip()
        import json
        data = json.loads(out) if out else []
        for a in data if isinstance(data, list) else [data]:
            n = norm(a.get("Name") or "")
            if n and "uninstall" not in n and "удален" not in n:
                apps.setdefault(n, (a.get("Name"), a.get("AppID")))
    except Exception as e:
        log.warning("Get-StartApps не сработал: %s", e)
    _apps_cache.update(t=time.time(), apps=apps)
    return apps


def open_app(name: str):
    q = canon(name)
    apps = start_apps()
    hit = best_match(q, apps) or best_match(norm(name), apps)   # и по словарю, и как сказали («Калькулятор»)
    try:
        if hit:
            title, app_id = apps[hit]
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"], creationflags=NO_WINDOW)
            return True, f"открыл «{title}»"
        links = start_menu()                       # запасной путь — ярлыки
        hit = best_match(q, links) or best_match(norm(name), links)
        if hit:
            os.startfile(str(links[hit]))
            return True, f"открыл «{links[hit].stem}»"
        if q in BUILTIN:
            os.startfile(BUILTIN[q])
            return True, f"открыл «{name}»"
    except OSError as e:
        return False, f"не смог открыть «{name}»: {e}"
    near = find_apps(name, 3)
    return False, f"не нашёл программу «{name}»" + (f". Похожие: {', '.join(near)}" if near else "")


def find_apps(word: str, limit: int = 15) -> list:
    """Для /apps: какие программы с таким словом видит Трой."""
    q, st = norm(word), stem(norm(word))
    names = set(start_apps()) | set(start_menu())
    found = sorted(n for n in names if q in n or st in stem(n))
    if not found:
        found = difflib.get_close_matches(q, list(names), n=limit, cutoff=0.5)
    titles = {n: t for n, (t, _) in start_apps().items()}
    return [titles.get(n, n) for n in found[:limit]]


# ── процессы ──
def running() -> dict:
    out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                         creationflags=NO_WINDOW, encoding="cp866", errors="replace").stdout
    procs = {}
    for row in csv.reader(io.StringIO(out)):
        if row and row[0].lower().endswith(".exe"):
            procs.setdefault(norm(row[0]), row[0])
    return procs


def close_app(name: str):
    c = canon(name)
    procs = running()
    hit = ""
    for q in [PROCESS.get(c, c)] + PROCESS_EXTRA.get(c, []):
        hit = best_match(q, procs)
        if hit:
            break
    if not hit and len(c.split()) > 1:
        hit = best_match(c.split()[-1], procs)
    if not hit:
        return False, f"«{name}» сейчас не запущена"
    if hit in NEVER_CLOSE:
        return False, f"«{procs[hit]}» закрывать нельзя — это часть Windows или сам Трой"
    r = subprocess.run(["taskkill", "/IM", procs[hit]], capture_output=True, text=True,
                       creationflags=NO_WINDOW, encoding="cp866", errors="replace")
    if r.returncode == 0:
        return True, f"закрыл «{procs[hit]}» (если были несохранённые файлы — программа спросит)"
    return False, f"«{procs[hit]}» не закрылась сама — возможно, ждёт ответа в своём окне"


# ── клавиши и питание ──
def press(vk: int, times: int = 1) -> None:
    import ctypes
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
        time.sleep(0.03)


def power_countdown(kind: str, report=None) -> None:
    """Одно окно Троя с отсчётом 60 с: «ОК» — сразу, «Отмена» или крестик — отменить.
    Выключение запускаем сами по окончании отсчёта — поэтому второго окна Windows «Ваш сеанс будет завершён» нет."""
    import ctypes
    what = "выключит компьютер" if kind == "shutdown" else "перезагрузит компьютер"
    box = ctypes.windll.user32.MessageBoxTimeoutW      # обычное окно Windows, которое само закрывается по таймеру
    res = box(0, f"Трой {what} через 60 секунд.\n\nОК — сделать это сейчас.\nОтмена — отменить.", "Трой",
              0x1 | 0x30 | 0x40000 | 0x10000 | 0x1000, 0, 60_000)  # OK/Отмена, предупреждение, поверх всех
    if res == 2:                                       # «Отмена» или крестик
        log.info("Выключение отменено на компьютере")
        if report:
            report(False, ("выключение" if kind == "shutdown" else "перезагрузку") + " отменили на компьютере")
        return
    log.info("Питание: %s (ответ окна %s)", kind, res)
    if report:                                         # потом компьютер уже ничего не скажет — сервер сам заметит
        report(True, "выключаю сейчас" if kind == "shutdown" else "перезагружаю сейчас")
    subprocess.run(["shutdown", "/s" if kind == "shutdown" else "/r", "/t", "0"], creationflags=NO_WINDOW)


SPLIT_RE = re.compile(r"\s*(?:,|;|\s+и\s+|\s+а также\s+|\s+плюс\s+|\s+потом\s+)\s*", re.I)


def several(fn, names: str):
    """«хром и яндекс музыку», «телеграм, стим» — по очереди каждую программу."""
    parts = [p.strip() for p in SPLIT_RE.split(names or "") if p and p.strip()]
    if len(parts) < 2:
        return fn(names)
    results = [fn(p) for p in parts[:5]]
    return all(ok for ok, _ in results), "; ".join(text for _, text in results)


def execute(action: str, arg: str = "", report=None):
    """Возвращает (успех, текст для владельца). report(ok, text) — сообщить владельцу позже (отмена выключения)."""
    if action in MEDIA_KEYS:
        press(MEDIA_KEYS[action], 5 if action in ("volume_up", "volume_down") else 1)
        return True, MEDIA_TEXT[action]
    if action == "open_app":
        return several(open_app, arg)
    if action == "find_app":
        found = find_apps(arg)
        return True, ("вижу программы: " + ", ".join(found)) if found else f"не вижу программ со словом «{arg}»"
    if action == "close_app":
        return several(close_app, arg)
    if action == "lock":
        import ctypes
        ctypes.windll.user32.LockWorkStation()
        return True, "экран заблокирован"
    if action == "sleep":
        def go():
            time.sleep(3)   # сначала успеваем отправить ответ
            subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=NO_WINDOW)
        threading.Thread(target=go, daemon=True).start()
        return True, "ухожу в сон"
    if action in ("shutdown", "restart"):
        threading.Thread(target=power_countdown, args=(action, report), daemon=True).start()
        return True, ("выключится" if action == "shutdown" else "перезагрузится") + \
            " через 60 секунд (на компьютере можно нажать «Отмена»)"
    return False, f"не знаю команду {action}"
