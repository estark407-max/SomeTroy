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
NEVER_CLOSE = {"troy", "sometroy", "explorer", "csrss", "winlogon", "system", "svchost", "lsass", "dwm", "services",
               "smss", "msedgewebview2", "runtimebroker", "searchhost", "startmenuexperiencehost", "shellexperiencehost",
               "textinputhost", "ctfmon", "sihost", "fontdrvhost", "audiodg", "conhost"}


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
    # «содержит» — только для осмысленного слова: «2» или «pc» не должны совпасть с чем попало
    contains = [n for n in names if query in n] if len(re.sub(r"[\d\s]", "", query)) >= 3 else []
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


_DRIVE_RU = {"д": "D", "ц": "C", "с": "C", "е": "E", "ф": "F", "г": "G", "а": "A", "б": "B", "и": "I"}


def open_explorer(name: str):
    """«Проводник», «проводник на диске D», «открой диск Д» — всегда Проводник Windows (не Internet Explorer)."""
    m = re.search(r"\bдиск\w*\s+([a-zа-я])\b|\b([a-z]):\\?", norm(name))
    letter = (m.group(1) or m.group(2)) if m else ""
    letter = _DRIVE_RU.get(letter, letter).upper()
    if letter and os.path.exists(f"{letter}:\\"):
        subprocess.Popen(["explorer.exe", f"{letter}:\\"], creationflags=NO_WINDOW)
        return True, f"открыл Проводник на диске {letter}"
    subprocess.Popen(["explorer.exe"], creationflags=NO_WINDOW)
    return True, "открыл Проводник" + (f" (диска {letter} нет)" if letter else "")


def open_app(name: str):
    q = canon(name)
    if q == "explorer" or re.search(r"\bпроводник|\bexplorer\b(?<!internet explorer)|^диск\w*\s+[a-zа-я]\b", norm(name)) \
            and "internet" not in norm(name):
        try:
            return open_explorer(name)
        except OSError as e:
            return False, f"не смог открыть Проводник: {e}"
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
        last = c.split()[-1]
        if len(last) >= 4 and not last.isdigit():     # «ферма 2» не должно закрыть «…2.exe»
            hit = best_match(last, procs)
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


# ── файлы между компьютерами (до 50 МБ) ──
FILE_MAX = 50 * 1024 * 1024
_SKIP_DIRS = {"node_modules", ".git", "appdata", "$recycle.bin", "venv", ".venv", "__pycache__"}


def _user_dirs() -> list:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    one = [Path(os.environ[v]) for v in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial") if os.environ.get(v)]
    roots = []
    for base in [home] + one:
        for sub in ("Desktop", "Рабочий стол", "Documents", "Документы", "Downloads", "Загрузки"):
            p = base / sub
            if p.is_dir() and p not in roots:
                roots.append(p)
    return roots


def find_files(query: str, limit: int = 400) -> list:
    """Файлы, в имени которых есть запрос (без учёта регистра и «ё»); свежие — первыми."""
    q = query.strip().strip("«»\"'")
    p = Path(os.path.expandvars(q))
    if p.is_absolute():
        return [p] if p.is_file() else []
    want = norm(q).replace(" ", "")
    words = [w for w in norm(q).split() if len(w) > 1]
    found = []
    for root in _user_dirs():
        for dirpath, dirnames, filenames in os.walk(root):
            if len(Path(dirpath).relative_to(root).parts) >= 3:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS and not d.startswith(".")]
            for fname in filenames:
                n = norm(fname)
                if want in n.replace(" ", "") or (words and all(w in n for w in words)):
                    found.append(Path(dirpath) / fname)
                    if len(found) >= limit:
                        break
    exact = [f for f in found if norm(f.name) == norm(q) or norm(f.stem) == norm(q)]
    return sorted(exact or found, key=lambda f: f.stat().st_mtime if f.exists() else 0, reverse=True)


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} МБ" if n >= 100 * 1024 else f"{max(1, n // 1024)} КБ"


def file_send(remote, cmd_id: int, arg: str):
    """Найти файл и отдать серверу — он передаст его на другой компьютер."""
    import json
    from urllib.parse import quote
    info = json.loads(arg or "{}")
    q, to = info.get("q", ""), info.get("to", "другой компьютер")
    files = find_files(q)
    if not files:
        return False, (f"не нашёл файл «{q}» на рабочем столе, в Документах и Загрузках. "
                       "Скажи имя точнее или полный путь (например D:\\Работа\\отчёт.xlsx)")
    names = list(dict.fromkeys(f.name for f in files))
    if len(names) > 1 and norm(names[0]) not in (norm(q), norm(Path(q).stem)):
        return False, f"нашёл несколько файлов «{q}»: " + ", ".join(names[:6]) + ". Какой переслать?"
    f = files[0]
    size = f.stat().st_size
    if size > FILE_MAX:
        return False, f"«{f.name}» весит {_mb(size)} — пересылаю файлы только до 50 МБ"
    code, body = remote.forward("POST", f"/api/files/upload?cmd={cmd_id}&name={quote(f.name)}", f.read_bytes())
    if code != 200:
        try:
            why = json.loads(body).get("error", "")
        except ValueError:
            why = ""
        return False, f"не смог передать «{f.name}»: {why or 'код ' + str(code)}"
    return True, f"отдал «{f.name}» ({_mb(size)}) для {to}"


def _downloads() -> Path:
    try:                                    # настоящая папка «Загрузки», даже если её перенесли на другой диск
        import ctypes
        from ctypes import wintypes
        import uuid
        g = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}")
        buf = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(ctypes.create_string_buffer(g.bytes_le, 16)), 0, None,
                                                      ctypes.byref(buf)) == 0:
            path = Path(buf.value)
            ctypes.windll.ole32.CoTaskMemFree(buf)
            return path
    except Exception:
        pass
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Downloads"


def file_get(remote, arg: str):
    """Забрать файл с сервера и положить в «Загрузки\\Трой» (одноимённые не затираем)."""
    import json
    info = json.loads(arg or "{}")
    name, src = info.get("name") or "файл", info.get("from") or "другого компьютера"
    code, data = remote.forward("GET", f"/api/files/get?id={info.get('id', '')}")
    if code != 200:
        return False, f"не смог забрать «{name}» с сервера (код {code})"
    folder = _downloads() / "Трой"
    folder.mkdir(parents=True, exist_ok=True)
    target, n = folder / name, 1
    while target.exists():
        target = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    target.write_bytes(data)
    try:                                    # открыть папку с выделенным файлом — видно, что пришло
        subprocess.Popen(["explorer.exe", "/select,", str(target)], creationflags=NO_WINDOW)
    except OSError:
        pass
    return True, f"📄 получил «{target.name}» ({_mb(len(data))}) с {src} → {target.parent}"
