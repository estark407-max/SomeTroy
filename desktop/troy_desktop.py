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
import re
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
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SomeTroy"
# настройки, которые сделал мастер, — в папке данных; ручная установка — рядом с программой
CONFIG = DATA_DIR / "config.json" if (DATA_DIR / "config.json").exists() else APP_DIR / "config.json"
CERT = DATA_DIR / "troy-cert.pem" if (DATA_DIR / "troy-cert.pem").exists() else APP_DIR / "troy-cert.pem"
# код сервера: для локального режима и для установки на VPS из мастера
SERVER_SRC = RES_DIR / "server_src" if getattr(sys, "frozen", False) else APP_DIR.parent / "server"
INSTANCE = DATA_DIR / "instance.json"
SESSION = DATA_DIR / "session.json"
SETTINGS = DATA_DIR / "settings.json"
APP_VERSION = "0.2.0"
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
                r = self.s.get(self.base + path, timeout=300 if path.startswith("/api/files/") else 30,
                               verify=self.verify, headers=headers)
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


WIZARD = {"w": None, "restart": None}      # мастер первой настройки, пока он идёт


def app_diagnostics(remote: "Remote") -> str:
    """/diag в программе: сначала проверки этого компьютера, потом — отчёт самого Троя."""
    cfg = load_config()
    mode = cfg.get("mode") or ("сервер" if cfg.get("server") else "не настроена")
    lines = [f"🖥 Программа Трой {APP_VERSION} на {os.environ.get('COMPUTERNAME') or 'этом компьютере'}",
             f"✅ Режим: {'на этом компьютере' if mode == 'local' else 'сервер ' + remote.base}"]
    if mode == "local":
        try:
            import local_server
            err = local_server._state.get("error")
            lines.append(f"❌ Трой на этом компьютере остановился: {err}" if err else "✅ Трой на этом компьютере запущен")
        except Exception as e:
            lines.append(f"❌ Локальный Трой: {e}")
    elif mode != "не настроена":
        lines.append(("✅" if CERT.exists() else "❌") + f" Сертификат сервера: {'есть' if CERT.exists() else 'нет — ' + str(CERT)}")
    code, body = remote._send("GET", "/api/ping")
    if code == 200:
        lines.append("✅ Связь с Троем: есть, вход выполнен")
    elif code == 401:
        lines.append("⚠️ Связь есть, но вход не выполнен — введи пароль (или задай новый в Telegram: /password)")
    else:
        try:
            why = json.loads(body).get("error", "")
        except ValueError:
            why = ""
        lines.append(f"❌ Связи с Троем нет: {why or 'код ' + str(code)}. Проверь интернет; если Трой на сервере — "
                     "что сервер включён и порт открыт.")
    lines.append(("✅" if find_browser() else "⚠️") + " Окно: " + ("Edge/Chrome найден" if find_browser()
                 else "Edge и Chrome не найдены — окно откроется в браузере по умолчанию"))
    hk = load_settings().get("hotkey")
    lines.append(f"⚪ Голос по клавише: {hk.get('label') if hk else 'не назначен'}")
    try:                                        # последние ошибки программы из troy.log
        errs = [l for l in LOG.read_text(encoding="utf-8", errors="replace").splitlines()
                if " ERROR " in l or " WARNING " in l][-5:]
    except OSError:
        errs = []
    lines.append("Последние проблемы программы:\n" + "\n".join("- " + e[:200] for e in errs) if errs
                 else "Проблем в журнале программы нет.")
    if code == 200:
        c2, b2 = remote.forward("POST", "/api/message", jbytes({"text": "/diag"}))
        try:
            lines.append("\n" + (json.loads(b2).get("reply") or "") if c2 == 200 else f"\n❌ Трой не ответил на /diag (код {c2})")
        except ValueError:
            pass
    lines.append(f"\nЖурнал программы: {LOG}")
    return "\n".join(lines)


# ── сообщить об ошибке: готовый отчёт открывается на GitHub, человек проверяет и отправляет ──
BUG_REPO = "https://github.com/estark407-max/SomeTroy"
_SCRUB = [(re.compile(r"(bot)\d{5,12}:[\w-]{20,}|(gsk_)[\w-]{8,}|(/rest/\d+/)[\w]{6,}|(y0_)[\w-]{10,}"),
           lambda m: next(g for g in m.groups() if g) + "•••"),
          (re.compile(r"troy://\S+"), lambda m: "troy://•••"),
          (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), lambda m: "•.•.•.•"),
          (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), lambda m: "•••@•••"),
          (re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.I), lambda m: m.group(0)[:3] + "Users\\•••")]


def scrub(text: str) -> str:
    """Перед отправкой отчёта: без ключей, адресов серверов, почты и имени пользователя Windows."""
    for rx, fn in _SCRUB:
        text = rx.sub(fn, text)
    return text


def bug_report(remote: "Remote", text: str) -> dict:
    """Текст для GitHub Issues + ссылка «новая задача» с ним. Ссылка не длиннее ~7500 символов:
    если диагностика не влезает — обрезаем её, а полный отчёт программа кладёт в буфер обмена."""
    from urllib.parse import quote
    text = (text or "").strip()
    first = re.sub(r"\s+", " ", text.splitlines()[0] if text else "")[:80]
    title = "Ошибка: " + (first or "без описания")
    try:
        diag = scrub(app_diagnostics(remote))
    except Exception as e:
        diag = f"(диагностика не собралась: {e})"
    import platform
    head = (f"**Что случилось**\n{text or '(не описано)'}\n\n"
            f"**Версия:** SomeTroy {APP_VERSION}, Windows {platform.release()} ({platform.version()})\n\n")

    def body_with(d: str) -> str:
        return head + "<details><summary>Диагностика</summary>\n\n```\n" + d + "\n```\n</details>\n"

    full = body_with(diag)
    url = lambda b: f"{BUG_REPO}/issues/new?labels=bug&title={quote(title)}&body={quote(b)}"
    d, trimmed = diag, False
    while len(url(body_with(d))) > 7500 and len(d) > 200:
        d, trimmed = d[: int(len(d) * 0.8)], True
    body = body_with(d + ("\n… (обрезано — полный отчёт в буфере обмена, вставь сюда Ctrl+V)" if trimmed else ""))
    return {"url": url(body), "full": full, "trimmed": trimmed, "title": title}


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
                self.send(200, jbytes({"logged_in": remote.logged_in, "setup": WIZARD["w"] is not None}))
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
            if path == "/setup" and WIZARD["w"]:            # шаг мастера настройки
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    data = {}
                w = WIZARD["w"]
                if data.get("action") == "restart":
                    self.send(200, b"{}")
                    threading.Thread(target=WIZARD["restart"], daemon=True).start()
                    return
                msgs = w.start() if data.get("action") == "start" else w.handle(str(data.get("text") or ""))
                self.send(200, jbytes({"messages": msgs}))
                return
            if path == "/savefile":                         # файл от Троя (например, переписка) → «Загрузки\\Трой»
                import base64
                import pc_control
                try:
                    data = json.loads(body or b"{}")
                    name = "".join(c for c in str(data.get("name") or "Трой.txt") if c not in '\\/:*?"<>|')[:120]
                    folder = pc_control._downloads() / "Трой"
                    folder.mkdir(parents=True, exist_ok=True)
                    target, n = folder / name, 1
                    while target.exists():
                        target = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
                        n += 1
                    target.write_bytes(base64.b64decode(data.get("b64") or ""))
                    try:
                        subprocess.Popen(["explorer.exe", "/select,", str(target)], creationflags=0x08000000)
                    except OSError:
                        pass
                    self.send(200, jbytes({"path": str(target)}))
                except Exception as e:
                    log.error("savefile: %s", e)
                    self.send(500, jbytes({"error": str(e)}))
                return
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
            if path == "/bugreport":                       # окно «Сообщить об ошибке»
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    data = {}
                rep_ = bug_report(remote, str(data.get("text") or ""))
                if data.get("open"):
                    webbrowser.open(rep_["url"])
                self.send(200, jbytes(rep_))
                return
            if path in ("/api/pc/choose", "/api/connections", "/api/connections/remove"):
                code, resp = remote.forward("POST", path, body)
                self.send(code, resp)
                return
            if path != "/api/message":
                self.send(404, b"{}")
                return
            try:
                raw = (json.loads(body or b"{}").get("text") or "").strip()
            except ValueError:
                raw = ""
            text = raw.lower()
            if text.split(" ")[0] in ("/bug", "/ошибка"):          # «/bug не открывается музыка»
                rep_ = bug_report(remote, raw.partition(" ")[2])
                webbrowser.open(rep_["url"])
                self.send(200, jbytes({"reply": "🐞 Открыл GitHub с готовым отчётом об ошибке — проверь текст и нажми "
                                                "**Submit new issue** (нужен бесплатный аккаунт GitHub). Ключи, пароли и "
                                                "адреса серверов из отчёта убраны.", "copy": rep_["full"] if rep_["trimmed"] else ""}))
                return
            if text in ("/diag", "/диагностика", "диагностика"):
                self.send(200, jbytes({"reply": app_diagnostics(remote)}))
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
                            if cmd["action"] == "file_send":      # переслать файл на другой компьютер
                                ok, text = pc_control.file_send(remote, cmd["id"], cmd.get("arg", ""))
                            elif cmd["action"] == "file_get":     # забрать файл, присланный с другого компьютера
                                ok, text = pc_control.file_get(remote, cmd.get("arg", ""))
                            else:
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


def load_config() -> dict:
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        return cfg if cfg.get("mode") == "local" or cfg.get("server") else {}
    except Exception:
        return {}


def make_wizard():
    """Мастер первой настройки: что делать, когда человек всё ввёл."""
    import setup_wizard
    import local_server
    device = os.environ.get("COMPUTERNAME") or "компьютер"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    def finish_local(settings: str, password: str) -> None:
        home = local_server.server_home(DATA_DIR)
        home.mkdir(parents=True, exist_ok=True)
        (home / "settings.env").write_text(settings, encoding="utf-8")
        session = local_server.set_password_and_login(DATA_DIR, password, device)
        if session:
            SESSION.write_text(json.dumps({"session": session}), encoding="utf-8")
        (DATA_DIR / "config.json").write_text(json.dumps({"mode": "local"}), encoding="utf-8")

    def finish_vps(info: dict) -> None:
        cert = DATA_DIR / "troy-cert.pem"
        cert.write_text(info["cert"], encoding="utf-8")
        server = f"https://{info['ip']}:{info['port']}"
        (DATA_DIR / "config.json").write_text(json.dumps({"mode": "vps", "server": server}), encoding="utf-8")
        try:                                    # сразу войти, чтобы не спрашивать только что придуманный пароль
            r = requests.post(server + "/api/login", json={"password": info["password"], "device": device},
                              timeout=15, verify=str(cert))
            if r.status_code == 200:
                SESSION.write_text(json.dumps({"session": r.json().get("session", "")}), encoding="utf-8")
        except requests.RequestException:
            pass                                # сервер пока недоступен с компьютера — войдёт по паролю позже

    def finish_connect(info: dict):
        """Ещё один компьютер к Трою на сервере: вход по паролю. None — получилось, иначе причина."""
        cert = DATA_DIR / "troy-cert.pem"
        cert.write_text(info["cert"], encoding="utf-8")
        server = f"https://{info['ip']}:{info['port']}"
        try:
            r = requests.post(server + "/api/login", json={"password": info["password"], "device": device},
                              timeout=20, verify=str(cert))
        except requests.RequestException:
            return "Сервер не ответил. Попробуй ещё раз через минуту."
        if r.status_code != 200:
            try:
                return r.json().get("error") or "Пароль не подошёл."
            except ValueError:
                return "Пароль не подошёл."
        SESSION.write_text(json.dumps({"session": r.json().get("session", "")}), encoding="utf-8")
        (DATA_DIR / "config.json").write_text(json.dumps({"mode": "vps", "server": server}), encoding="utf-8")
        return None

    template = (SERVER_SRC / "settings.env.example").read_text(encoding="utf-8")
    return setup_wizard.Wizard(finish_local, finish_vps, template, SERVER_SRC, DATA_DIR, finish_connect)


APP_HOOKS = {"restart": None}


def move_to_vps(values: dict) -> tuple:
    """«Подключения → Сервер (VPS)»: перенести Троя с этого компьютера на свой сервер.
    Ставим на сервер, переносим базу (переписка, напоминания, идеи, пароль программы), переключаем программу."""
    import sqlite3
    import tempfile
    import local_server
    import setup_wizard
    got = setup_wizard.parse_vps_address(values.get("address", ""))
    if not got:
        return "Нужен IP-адрес сервера — четыре числа через точку, например 203.0.113.10.", ""
    ip, user, ssh_port = got
    password = values.get("password", "")
    home = local_server.server_home(DATA_DIR)
    settings = (home / "settings.env").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:          # снимок базы целиком, даже пока Трой работает
        src = sqlite3.connect(home / "assistant.db")
        dst = sqlite3.connect(Path(tmp) / "db")
        src.backup(dst)
        dst.close()
        src.close()
        extra = {"assistant.db": (Path(tmp) / "db").read_bytes()}
    ideas = home / "ideas"
    for f in ideas.rglob("*") if ideas.exists() else []:
        if f.is_file():
            extra["ideas/" + f.relative_to(ideas).as_posix()] = f.read_bytes()
    setup_wizard.password_holder["pw"] = ""
    log.info("Переезд на сервер %s", ip)
    res = setup_wizard.install_vps(ip, user, password, settings, SERVER_SRC, lambda t: log.info("VPS: %s", t),
                                   ssh_port, extra)
    if not res.get("ok"):
        tail = f"\nПоследние строки установки:\n{res['log']}" if res.get("log") else ""
        return res.get("error", "не получилось") + tail, ""
    CERT.write_text(res["cert"], encoding="utf-8")
    (DATA_DIR / "config.json").write_text(json.dumps({"mode": "vps", "server": f"https://{ip}:{res['port']}"}),
                                          encoding="utf-8")
    reach = setup_wizard.server_reachable(ip, res["port"], CERT)
    if APP_HOOKS["restart"]:
        threading.Timer(4, APP_HOOKS["restart"]).start()
    return None, (f"🎉 Готово: Трой переехал на сервер {ip} и теперь работает круглосуточно. Переписка, напоминания и идеи "
                  "переехали с ним, пароль программы тот же. Сейчас программа перезапустится."
                  + ("" if reach else f"\n\n⚠️ Пока не достучался до сервера по порту {res['port']}: если у хостинга есть "
                     f"файрвол в панели управления — открой там порт {res['port']} (TCP). Telegram работает и без этого.")
                  + "\n\nКопия данных осталась на этом компьютере.")


def show_diag() -> None:
    """Из трея: открыть окно и сразу показать диагностику."""
    hwnd = find_window() if window_open() else None
    if hwnd:
        close_window()
        time.sleep(0.8)
    open_window(APP_URL + "?diag=1")


def restart_program() -> None:
    """После мастера: запустить программу заново уже с настройками."""
    args = [sys.executable] + ([] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve())])
    subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def main():
    global APP_URL
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if already_running():
        return
    cfg = load_config()
    if not cfg:                                 # первый запуск — мастер настройки в окне
        WIZARD["w"] = make_wizard()
        cfg = {"server": "http://127.0.0.1:9"}
    elif cfg.get("mode") == "local":            # Трой живёт прямо здесь
        import local_server
        try:
            port = local_server.start(DATA_DIR)
            local_server._state["bot"].VPS_MOVE = move_to_vps      # «Подключения → Сервер (VPS)»
        except Exception as e:
            log.error("Локальный сервер: %s", e)
            message_box(f"Трой не смог запуститься на этом компьютере: {e}\nПодробности — в {LOG}")
            return
        cfg = {**cfg, "server": f"http://127.0.0.1:{port}"}
    elif not CERT.exists():
        message_box(f"Нет сертификата сервера: положи troy-cert.pem рядом с программой\n{CERT}")
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
        pystray.MenuItem("Диагностика", lambda icon, item: show_diag()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выйти из аккаунта", on_logout, enabled=lambda item: remote.logged_in),
        pystray.MenuItem("Закрыть Троя", on_close),
    ))

    def after_wizard():
        time.sleep(0.5)
        close_window()
        try:
            INSTANCE.unlink()
        except OSError:
            pass
        server.shutdown()
        server.server_close()                   # освободить порт для нового запуска
        restart_program()
        icon.stop()

    WIZARD["restart"] = after_wizard
    APP_HOOKS["restart"] = after_wizard

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
