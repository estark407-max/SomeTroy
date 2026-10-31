"""Трой на компьютере.

Программа поднимает маленький сервер только для этого компьютера (127.0.0.1) и открывает окно
Edge без адресной строки. Окно рисует ui.html, а все запросы к Трою идут через эту программу:
она добавляет пароль и проверяет, что на том конце именно твой сервер (сертификат troy-cert.pem).

Вход — по паролю, который владелец задаёт в Telegram (/password). Пароль спрашивается после
установки и после «Выйти из аккаунта»; «Закрыть Троя» вход сохраняет.

Крестик закрывает только окно — Трой остаётся в трее (у часов) и присылает напоминания
уведомлениями Windows. В трее: «Открыть», «Выйти из аккаунта», «Закрыть Троя».
Запуск с ключом --tray — сразу в трей, без окна (для автозапуска с Windows).

Рядом с программой: config.json (адрес сервера и пароль) и troy-cert.pem.
"""
import json
import logging
import traceback
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
CONFIG = APP_DIR / "config.json"
CERT = APP_DIR / "troy-cert.pem"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SomeTroy"
INSTANCE = DATA_DIR / "instance.json"
SESSION = DATA_DIR / "session.json"
SETTINGS = DATA_DIR / "settings.json"
APP_VERSION = "0.1.0"
HOTKEY_HITS = {"n": 0, "down": False}   # нажатия клавиши голоса и держат ли её сейчас (окно следит)

LOG = DATA_DIR / "troy.log"
last_alive = 0.0
log = logging.getLogger("troy")


def setup_log() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:   # журнал не растёт бесконечно
        if LOG.exists() and LOG.stat().st_size > 1_000_000:
            LOG.unlink()
    except OSError:
        pass
    logging.basicConfig(filename=str(LOG), level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
WINDOW_TITLE = "Трой"


def message_box(text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, "Трой", 0x10)
    except Exception:
        print(text)


def jbytes(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()


class Remote:
    """Связь с сервером Троя: вход по паролю + проверка сертификата."""

    def __init__(self, cfg: dict):
        self.base = cfg["server"].rstrip("/")
        self.s = requests.Session()
        self.verify = str(CERT)   # передаём в каждый запрос: переменные окружения не перебьют
        try:
            self.session = json.loads(SESSION.read_text(encoding="utf-8")).get("session", "")
        except Exception:
            self.session = ""

    @property
    def logged_in(self) -> bool:
        return bool(self.session)

    def _save(self, session: str) -> None:
        self.session = session
        if session:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            SESSION.write_text(json.dumps({"session": session}), encoding="utf-8")
        else:
            try:
                SESSION.unlink()
            except OSError:
                pass

    def login(self, password: str):
        device = os.environ.get("COMPUTERNAME") or "компьютер"
        code, body = self._send("POST", "/api/login", jbytes({"password": password, "device": device}), auth=False)
        if code == 200:
            self._save(json.loads(body).get("session", ""))
        return code, body

    def logout(self) -> None:
        if self.session:
            self._send("POST", "/api/logout", b"{}")
        self._save("")

    def forward(self, method: str, path: str, body: bytes = b""):
        if not self.session:
            return 401, jbytes({"error": "login"})
        code, resp = self._send(method, path, body)
        if code == 401:            # пароль сменили или вышли везде — нужен новый вход
            self._save("")
        return code, resp

    def _send(self, method: str, path: str, body: bytes = b"", auth: bool = True):
        headers = {"Content-Type": "application/json"}
        if auth and self.session:
            headers["Authorization"] = f"Bearer {self.session}"
        try:
            if method == "GET":
                r = self.s.get(self.base + path, timeout=30, verify=self.verify, headers=headers)
            else:
                r = self.s.post(self.base + path, data=body, timeout=300, verify=self.verify, headers=headers)
            if r.status_code >= 400:
                log.info("%s %s → %s", method, path.split("?")[0], r.status_code)
            return r.status_code, r.content
        except requests.exceptions.SSLError:
            log.error("Сертификат сервера не совпал")
            return 502, json.dumps({"error": "сертификат сервера не совпадает — нужен свежий troy-cert.pem"},
                                   ensure_ascii=False).encode()
        except requests.exceptions.ConnectionError as e:
            log.warning("Нет связи с сервером: %s", e)
            return 502, json.dumps({"error": "нет связи с сервером"}, ensure_ascii=False).encode()
        except requests.exceptions.Timeout:
            return 504, json.dumps({"error": "сервер долго не отвечает"}, ensure_ascii=False).encode()


def make_handler(remote: Remote, key: str):
    prefix = f"/{key}"
    files = {"/": ("ui.html", "text/html; charset=utf-8"), "/favicon.png": ("favicon.png", "image/png")}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def route(self):
            url = urlparse(self.path)
            if not url.path.startswith(prefix):      # чужие программы на компьютере не знают ключа
                self.send(404, b"{}")
                return None, None
            return url.path[len(prefix):] or "/", url

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except Exception:
                log.error("Ошибка обработки запроса:\n%s", traceback.format_exc())
                try:
                    self.send(500, jbytes({"error": "ошибка в программе — подробности в troy.log"}))
                except Exception:
                    pass

        def do_GET(self):
            global last_alive
            path, url = self.route()
            if path is None:
                return
            if path in files:
                name, ctype = files[path]
                self.send(200, (RES_DIR / name).read_bytes(), ctype)
            elif path == "/show":
                threading.Thread(target=show_window, daemon=True).start()
                self.send(200, b"{}")
            elif path == "/hk":
                self.send(200, jbytes({"n": HOTKEY_HITS["n"], "down": HOTKEY_HITS["down"]}))
            elif path == "/settings":
                self.send(200, jbytes(load_settings()))
            elif path == "/version":
                self.send(200, jbytes({"version": APP_VERSION}))
            elif path == "/session":
                self.send(200, jbytes({"logged_in": remote.logged_in}))
            elif path == "/alive":
                last_alive = time.time()
                self.send(200, b"{}")
            elif path == "/open":
                link = (parse_qs(url.query).get("url") or [""])[0]
                if link.startswith(("http://", "https://")):
                    webbrowser.open(link)
                self.send(200, b"{}")
            elif path.startswith("/api/"):
                code, body = remote.forward("GET", path + (f"?{url.query}" if url.query else ""))
                self.send(code, body)
            else:
                self.send(404, b"{}")

        def do_POST(self):
            path, _ = self.route()
            if path is None:
                return
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if path == "/login":
                try:
                    password = json.loads(body or b"{}").get("password", "")
                except ValueError:
                    password = ""
                code, resp = remote.login(password)
                self.send(code, resp)
                return
            if path == "/logout":
                remote.logout()
                self.send(200, b"{}")
                return
            if path == "/settings":
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    data = {}
                ok, err = set_hotkey(data.get("hotkey"))
                self.send(200 if ok else 400, jbytes({"ok": ok, "error": err, **load_settings()}))
                return
            if path == "/api/pc/choose":
                code, resp = remote.forward("POST", path, body)
                self.send(code, resp)
                return
            if path != "/api/message":
                self.send(404, b"{}")
                return
            code, resp = remote.forward("POST", path, body)
            self.send(code, resp)

    return Handler


def find_browser():
    pf = [os.environ.get(v, "") for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for base in pf:
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            p = Path(base) / rel
            if base and p.exists():
                return str(p)
    return None


DEFAULT_SIZE = (500, 880)        # по умолчанию — узкое вертикальное окно, как боковая панель


def save_settings(**kw) -> None:
    try:
        SETTINGS.write_text(json.dumps({**load_settings(), **kw}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def window_args() -> list:
    """Размер и место окна — как было в прошлый раз."""
    w = load_settings().get("window") or {}
    if w.get("max"):
        return ["--start-maximized"]
    try:
        width, height = int(w["w"]), int(w["h"])
        if width < 420 or height < 320:          # что-то странное сохранилось — начинаем с обычного размера
            raise ValueError
        args = [f"--window-size={width},{height}"]
        if -200 < int(w["x"]) < 10000 and -200 < int(w["y"]) < 10000:
            args.append(f"--window-position={int(w['x'])},{int(w['y'])}")
        return args
    except (KeyError, TypeError, ValueError):
        return [f"--window-size={DEFAULT_SIZE[0]},{DEFAULT_SIZE[1]}"]


def remember_window() -> None:
    """Раз в 2 секунды запоминаем размер и место окна (свёрнутое не трогаем)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    while True:
        try:
            hwnd = find_window() if window_open() else None
            if hwnd and not user32.IsIconic(hwnd):
                old = load_settings().get("window") or {}
                if user32.IsZoomed(hwnd):
                    new = {**old, "max": True}
                else:
                    r = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(r))
                    k = 1.0
                    try:                     # Edge ждёт размеры без учёта масштаба Windows (125%, 150%…)
                        if user32.IsProcessDPIAware():
                            k = user32.GetDpiForWindow(hwnd) / 96 or 1.0
                    except Exception:
                        pass
                    new = {"x": round(r.left / k), "y": round(r.top / k),
                           "w": round((r.right - r.left) / k), "h": round((r.bottom - r.top) / k), "max": False}
                if new != old and (new.get("max") or new.get("w", 0) > 100):
                    save_settings(window=new)
        except Exception:
            log.debug("remember_window", exc_info=True)
        time.sleep(2)


def open_window(url: str) -> None:
    browser = find_browser()
    if not browser:
        webbrowser.open(url)
        return
    subprocess.Popen([
        browser, f"--app={url}", f"--user-data-dir={DATA_DIR / 'window'}", *window_args(),
        "--no-first-run", "--no-default-browser-check", "--disable-features=Translate",
        "--autoplay-policy=no-user-gesture-required",   # звук напоминания, даже если по окну ещё не кликали
    ], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


APP_URL = ""


def find_window():
    """Окно Троя (Edge в режиме приложения) — по заголовку."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if buf.value == WINDOW_TITLE and cls.value.startswith("Chrome_WidgetWin"):
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def window_open() -> bool:
    return bool(last_alive) and time.time() - last_alive < 12


def show_window() -> None:
    """Показать окно: развернуть, если открыто, иначе открыть заново."""
    hwnd = find_window() if window_open() else None
    if hwnd:
        import ctypes
        ctypes.windll.user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    else:
        open_window(APP_URL)


def close_window() -> None:
    hwnd = find_window()
    if hwnd:
        import ctypes
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE


def already_running() -> bool:
    """Если Трой уже запущен — просто показать его окно."""
    try:
        info = json.loads(INSTANCE.read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{info['port']}/{info['key']}/"
        requests.get(url + "show", timeout=3).raise_for_status()
        return True
    except Exception:
        return False


# ── команды для этого компьютера ──
def pc_worker(remote: Remote) -> None:
    """Каждые 4 секунды спрашиваем сервер, нет ли команд (заодно сервер видит, что компьютер в сети)."""
    import pc_control
    while True:
        try:
            if remote.logged_in:
                code, body = remote.forward("GET", f"/api/pc/poll?v={APP_VERSION}")
                if code == 200:
                    for cmd in json.loads(body).get("commands", []):
                        log.info("Команда: %s %s", cmd["action"], cmd.get("arg", ""))
                        try:
                            def late(ok2, text2, cid=cmd["id"]):      # например, «выключение отменили на компьютере»
                                remote.forward("POST", "/api/pc/result",
                                               jbytes({"id": cid, "ok": ok2, "text": text2, "late": True}))
                            ok, text = pc_control.execute(cmd["action"], cmd.get("arg", ""), late)
                        except Exception as e:
                            log.error("Команда упала:\n%s", traceback.format_exc())
                            ok, text = False, f"ошибка: {e}"
                        remote.forward("POST", "/api/pc/result", jbytes({"id": cmd["id"], "ok": ok, "text": text}))
        except Exception:
            log.error("pc_worker:\n%s", traceback.format_exc())
        time.sleep(4)


# ── горячая клавиша для голосовой команды ──
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
_hk = {"thread": None, "tid": 0}


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return {"hotkey": None}


def on_hotkey() -> None:
    HOTKEY_HITS["n"] += 1
    if window_open():
        show_window()
    else:                       # окно закрыто — открываем и сразу начинаем запись
        open_window(APP_URL + "?voice=1")


def _hotkey_loop(mods: int, vk: int, ready: dict) -> None:
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    _hk["tid"] = kernel32.GetCurrentThreadId()
    ready["ok"] = bool(user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk))
    ready["event"].set()
    if not ready["ok"]:
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == 0x0312:      # WM_HOTKEY — клавишу нажали
            HOTKEY_HITS["down"] = True
            try:
                on_hotkey()
            except Exception:
                log.error("hotkey:\n%s", traceback.format_exc())
            # Windows сообщает только о нажатии — отпускание ловим сами (как у рации: держишь — говоришь)
            started = time.time()
            while user32.GetAsyncKeyState(vk) & 0x8000 and time.time() - started < 300:
                time.sleep(0.03)
            HOTKEY_HITS["down"] = False
    user32.UnregisterHotKey(None, 1)


def apply_hotkey(hk) -> tuple:
    """Снимает старую клавишу и ставит новую. Возвращает (успех, ошибка)."""
    if _hk["thread"] and _hk["tid"]:
        try:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(_hk["tid"], 0x0012, 0, 0)   # WM_QUIT
            _hk["thread"].join(2)
        except Exception:
            pass
        _hk["thread"], _hk["tid"] = None, 0
    if not hk:
        return True, ""
    ready = {"event": threading.Event(), "ok": False}
    t = threading.Thread(target=_hotkey_loop, args=(int(hk["mods"]), int(hk["vk"]), ready), daemon=True)
    t.start()
    ready["event"].wait(3)
    if not ready["ok"]:
        return False, f"Сочетание {hk.get('label', '')} уже занято другой программой — выбери другое"
    _hk["thread"] = t
    return True, ""


def set_hotkey(hk) -> tuple:
    if hk is not None:
        try:
            hk = {"mods": int(hk["mods"]), "vk": int(hk["vk"]), "label": str(hk.get("label", ""))[:40]}
        except (KeyError, TypeError, ValueError):
            return False, "неверная клавиша"
    ok, err = apply_hotkey(hk)
    if ok:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        save_settings(hotkey=hk)
    else:
        apply_hotkey(load_settings().get("hotkey"))   # вернуть прежнюю
    return ok, err


def watch_events(remote: Remote, icon) -> None:
    """Пока окно закрыто, напоминания приходят уведомлениями Windows."""
    last = None
    while True:
        try:
            if not remote.logged_in:
                last = None
                time.sleep(5)
                continue
            code, body = remote.forward("GET", f"/api/events?after={last or 0}")
            if code == 200:
                events = json.loads(body).get("events", [])
                if last is None:
                    last = max([e["id"] for e in events], default=0)
                else:
                    for e in events:
                        last = max(last, e["id"])
                        if not window_open():
                            icon.notify(e["text"], WINDOW_TITLE)
        except Exception:
            pass
        time.sleep(15)


def tray_hint_once(icon) -> None:
    """Когда окно закрыли впервые — подсказать, что Трой остался в трее."""
    flag = DATA_DIR / "tray-hint-shown"
    was_open = False
    while not flag.exists():
        time.sleep(3)
        if window_open():
            was_open = True
        elif was_open:
            icon.notify("Я остался в трее у часов. Щёлкни по иконке, чтобы открыть; "
                        "правая кнопка → «Выйти».", WINDOW_TITLE)
            flag.write_text("1")


def main():
    global APP_URL
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        assert cfg.get("server")
    except Exception:
        message_box(f"Нет настроек: положи config.json рядом с программой\n{CONFIG}")
        return
    if not CERT.exists():
        message_box(f"Нет сертификата сервера: положи troy-cert.pem рядом с программой\n{CERT}")
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if already_running():
        return

    import pystray
    from PIL import Image

    remote = Remote(cfg)
    key = secrets.token_urlsafe(16)
    # постоянный адрес — чтобы окно запомнило разрешения (микрофон) между запусками
    server = None
    for port in [47315, 47316, 47317, 47318, 0]:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(remote, key))
            break
        except OSError:
            continue
    server.daemon_threads = True
    port = server.server_address[1]
    APP_URL = f"http://127.0.0.1:{port}/{key}/"
    INSTANCE.write_text(json.dumps({"port": port, "key": key}), encoding="utf-8")
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def on_close(icon, _item):            # закрыть программу, вход сохраняется
        close_window()
        icon.stop()

    def on_logout(icon, _item):           # выйти из аккаунта: в следующий раз спросит пароль
        remote.logout()
        icon.notify("Ты вышел из аккаунта. Чтобы снова пользоваться Троем, введи пароль.", WINDOW_TITLE)
        show_window()

    def on_about(_icon, _item):
        import ctypes
        threading.Thread(target=lambda: ctypes.windll.user32.MessageBoxW(
            0, f"Трой {APP_VERSION}\nЛичный ассистент — в Telegram и на компьютере.\n\n"
               "«Сей модуль является симбиозом дружеского взаимодействия лысого бородатого человека и сверхумной, но очень приятной машины»\n\n"
               "Авторы: Claude и Egor Zhidetskiy\n© 2026 Claude и Egor Zhidetskiy",
            "О программе", 0x40 | 0x10000), daemon=True).start()

    icon = pystray.Icon("troy", Image.open(RES_DIR / "favicon.png"), WINDOW_TITLE, menu=pystray.Menu(
        pystray.MenuItem("Открыть Троя", lambda icon, item: show_window(), default=True),
        pystray.MenuItem("О программе", on_about),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выйти из аккаунта", on_logout, enabled=lambda item: remote.logged_in),
        pystray.MenuItem("Закрыть Троя", on_close),
    ))

    def setup(icon):
        icon.visible = True
        threading.Thread(target=watch_events, args=(remote, icon), daemon=True).start()
        threading.Thread(target=pc_worker, args=(remote,), daemon=True).start()
        threading.Thread(target=remember_window, daemon=True).start()
        hk = load_settings().get("hotkey")
        if hk and not apply_hotkey(hk)[0]:
            icon.notify(f"Горячая клавиша {hk.get('label', '')} занята другой программой — назначь другую.", WINDOW_TITLE)
        if "--tray" not in sys.argv:
            open_window(APP_URL)
            threading.Thread(target=tray_hint_once, args=(icon,), daemon=True).start()

    icon.run(setup=setup)       # живёт, пока не выбрали «Выйти»
    try:
        INSTANCE.unlink()
    except OSError:
        pass
    server.shutdown()


if __name__ == "__main__":
    setup_log()
    log.info("Запуск %s", " ".join(sys.argv[1:]) or "")
    try:
        main()
    except Exception:
        log.error("Программа упала:\n%s", traceback.format_exc())
        message_box(f"Трой не смог запуститься. Подробности — в файле\n{LOG}")
        raise
