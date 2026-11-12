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
APP_VERSION = "0.6.1"
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
WINDOW_TITLE = "Трой"                 # заголовок окна (по нему программа находит своё окно)
PRODUCT = "Troy"                  # название программы: значок у часов, «О программе», установщик


def message_box(text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, PRODUCT, 0x10)
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
        except requests.exceptions.SSLError as e:
            log.error("Сертификат сервера не совпал: %s", str(e)[-300:])
            return 502, json.dumps({"error": "сертификат сервера не совпадает — нужен свежий troy-cert.pem"},
                                   ensure_ascii=False).encode()
        except requests.exceptions.ConnectionError as e:
            log.warning("Нет связи с сервером: %s", e)
            return 502, json.dumps({"error": "нет связи с сервером"}, ensure_ascii=False).encode()
        except requests.exceptions.Timeout:
            return 504, json.dumps({"error": "сервер долго не отвечает"}, ensure_ascii=False).encode()


WIZARD = {"w": None, "restart": None}      # мастер первой настройки, пока он идёт


def cert_check(base: str) -> str:
    """Что за сертификат на самом деле отвечает на порту: наш, другой наш (переустановка) или чужой (антивирус)."""
    import ssl
    import tempfile
    host, _, port = base.split("://", 1)[-1].rstrip("/").partition(":")
    try:
        pem = ssl.get_server_certificate((host, int(port or 443)), timeout=10)
    except OSError as e:
        return f"\n   Не получил сертификат с сервера: {e}"
    try:
        mine = CERT.read_text(encoding="utf-8")
    except OSError:
        mine = ""
    der = lambda p: ssl.PEM_cert_to_DER_cert(p) if "BEGIN CERTIFICATE" in p else b""
    if mine and der(pem) == der(mine):
        return "\n   Сертификат на сервере тот же, что у программы — подробности ошибки в журнале программы (troy.log)."
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
            f.write(pem)
        info = ssl._ssl._test_decode_cert(f.name)
        os.unlink(f.name)
        issuer = ", ".join(v for part in info.get("issuer", ()) for k, v in part if k in ("organizationName", "commonName"))
    except Exception:
        issuer = ""
    if issuer.strip() == "Troy":
        return ("\n   На сервере другой сертификат Троя (его создали заново при переустановке). Нажми ⓘ «О программе» → «🛠 Обновить и починить сервер» "
                "— он заберёт свежий сертификат с сервера.")
    return (f"\n   Вместо сертификата сервера приходит чужой: «{issuer or 'неизвестный'}». Так делает антивирус или прокси, "
            "который проверяет защищённые соединения. Добавь в нём исключение для программы SomeTroy.exe или для адреса "
            f"{host} (в Касперском: Настройки → Сетевые параметры → Проверка защищённых соединений → Исключения).")


def bundled_server_version() -> str:
    """Какую версию Троя для сервера везёт с собой эта программа."""
    import re
    try:
        m = re.search(r'^SERVER_VERSION = "([\d.]+)"', (SERVER_SRC / "bot.py").read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


def server_outdated(remote: "Remote") -> str:
    """Трой на сервере старее программы? Тогда подсказка, как обновить (пустая строка — всё в порядке)."""
    if not str(remote.base).startswith("https://"):
        return ""                                   # локальный режим: код обновляется вместе с программой
    code, body = remote._send("GET", "/api/ping")
    if code != 200:
        return ""
    try:
        have = json.loads(body).get("version") or "0"
    except ValueError:
        return ""
    want = bundled_server_version()
    num = lambda v: tuple(int(x) for x in v.split(".") if x.isdigit())
    if want and num(have) < num(want):
        return (f"Трой на сервере старее программы ({'старая версия' if have == '0' else have} → {want}): новые возможности "
                "не заработают. Обнови его: ⓘ «О программе» → «🛠 Обновить и починить сервер» (попросит пароль сервера).")
    return ""


def server_check_loop(remote: "Remote", icon) -> None:
    time.sleep(20)
    note = server_outdated(remote)
    if note:
        log.warning(note)
        try:
            icon.notify(note, WINDOW_TITLE)
        except Exception:
            pass


def port_check(base: str) -> str:
    """Почему не достучались до сервера: порт закрыт (Трой там не запущен) или ответа нет вовсе (файрвол, сервер выключен)."""
    import socket
    host, _, port = base.split("://", 1)[-1].rstrip("/").partition(":")
    port = int(port or 443)
    try:
        socket.create_connection((host, 22), timeout=6).close()
        server_up = True
    except OSError:
        server_up = False
    try:
        socket.create_connection((host, port), timeout=8).close()
        return " Порт отвечает — похоже, связь только что восстановилась; повтори /diag."
    except ConnectionRefusedError:
        return (f"\n   Сервер на связи, но на порту {port} никто не отвечает — служба Троя там не запущена или упала. "
                "Зайди на сервер и посмотри: systemctl status sometroy и journalctl -u sometroy -n 50 "
                "(или пришли это разработчикам: /bug).")
    except OSError:
        if server_up:
            return (f"\n   Сервер на связи (SSH отвечает), а порт {port} молчит — скорее всего, его закрывает файрвол "
                    f"хостинга. Открой в панели управления сервером входящий порт {port} (TCP). "
                    "Telegram при этом работает и без него.")
        return ("\n   Сервер не отвечает совсем: выключен, неверный адрес или нет интернета на этом компьютере. "
                "Проверь сервер в панели хостинга.")


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
        hint = " Перезапусти программу." if mode == "local" else (
            cert_check(remote.base) if "сертификат" in why else port_check(remote.base))
        lines.append(f"❌ Связи с Троем нет: {why or 'код ' + str(code)}." + hint)
    outdated = server_outdated(remote) if code == 200 else ""
    if outdated:
        lines.append("⚠️ " + outdated)
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


# ── обновления: новая версия на GitHub (Releases) → скачать и поставить прямо из программы ──
RELEASES_API = "https://api.github.com/repos/estark407-max/SomeTroy/releases/latest"
UPD = {"state": "", "done": 0, "total": 0, "error": "", "version": ""}


def check_update() -> dict:
    """Есть ли на GitHub версия новее этой программы. Без ключей и входа — публичная страница релизов."""
    try:
        r = requests.get(RELEASES_API, timeout=15, headers={"Accept": "application/vnd.github+json",
                                                              "User-Agent": f"SomeTroy/{APP_VERSION}"})
    except requests.RequestException as e:
        return {"current": APP_VERSION, "error": f"нет связи с GitHub: {e.__class__.__name__}"}
    if r.status_code == 404:
        return {"current": APP_VERSION, "newer": False, "latest": APP_VERSION}       # релизов ещё нет
    if r.status_code != 200:
        return {"current": APP_VERSION, "error": f"GitHub ответил {r.status_code}"}
    rel = r.json()
    latest = str(rel.get("tag_name") or "").lstrip("vV")
    asset = next((a for a in rel.get("assets", []) if re.match(r"SomeTroySetup.*\.exe$", a.get("name", ""), re.I)), None)
    return {"current": APP_VERSION, "latest": latest, "newer": bool(asset) and _vnum(latest) > _vnum(APP_VERSION),
            "notes": (rel.get("body") or "")[:4000], "url": asset["browser_download_url"] if asset else "",
            "size": asset.get("size", 0) if asset else 0, "name": asset["name"] if asset else ""}


def install_update() -> None:
    """Скачать установщик новой версии и поставить его тихо; после установки Трой запустится сам."""
    import tempfile
    info = check_update()
    if not info.get("newer"):
        UPD.update(state="error", error=info.get("error") or "новой версии нет")
        return
    if not info["url"].startswith("https://github.com/"):          # установщик — только со страницы релизов GitHub
        UPD.update(state="error", error="ссылка на обновление ведёт не на GitHub")
        return
    UPD.update(state="download", done=0, total=info["size"], error="", version=info["latest"])
    target = Path(tempfile.gettempdir()) / info["name"]
    try:
        with requests.get(info["url"], stream=True, timeout=60, headers={"User-Agent": f"SomeTroy/{APP_VERSION}"}) as r:
            r.raise_for_status()
            with open(target, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    f.write(chunk)
                    UPD["done"] += len(chunk)
        if info["size"] and target.stat().st_size != info["size"]:
            raise OSError("файл скачался не полностью")
    except (requests.RequestException, OSError) as e:
        UPD.update(state="error", error=f"не скачалось: {e}")
        return
    UPD["state"] = "install"
    log.info("Ставлю обновление %s", info["latest"])
    try:
        subprocess.Popen([str(target), "/SP-", "/SILENT", "/NOCANCEL", "/SUPPRESSMSGBOXES"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))   # установщик сам закроет Троя
    except OSError as e:                          # антивирус удалил файл, Windows запретила запуск…
        UPD.update(state="error", error=f"установщик не запустился: {e}")


BUG = {"shot": b""}                  # скриншот к отчёту (PNG), пока окно отчёта открыто


def _with_timeout(fn, seconds: float, fallback):
    """Медленную проверку не ждём бесконечно: отчёт должен собираться за секунды, а не минуты."""
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("v", fn()), daemon=True)
    t.start()
    t.join(seconds)
    return box.get("v", fallback)


def bug_details(remote: "Remote") -> str:
    """Подробности для разработчика: версии, система, режим, журнал программы и диагностика Троя."""
    import platform
    cfg = load_config()
    mode = cfg.get("mode") or ("сервер" if cfg.get("server") else "не настроена")
    lines = [f"Программа: Troy {APP_VERSION} (код сервера в программе: {bundled_server_version() or '?'})",
             f"Windows: {platform.release()} {platform.version()} ({platform.machine()}), "
             f"Python {platform.python_version()}, {'собранная программа' if getattr(sys, 'frozen', False) else 'из исходников'}",
             f"Режим: {'на этом компьютере' if mode == 'local' else 'сервер ' + str(remote.base)}",
             f"Браузер для окна: {'Edge/Chrome найден' if find_browser() else 'не найден'}; "
             f"тема: {load_settings().get('theme', 'по умолчанию')}"]
    code, body = _with_timeout(lambda: remote._send("GET", "/api/ping"), 8, (0, b""))
    try:
        ping = json.loads(body) if code == 200 else {}
    except ValueError:
        ping = {}
    lines.append(f"Связь с Троем: {'есть' if code == 200 else 'нет (код ' + str(code) + ')'}"
                 + (f", Трой на сервере {ping.get('version') or 'старой версии'}" if code == 200 and mode != "local" else ""))
    try:                                            # хвост журнала программы — самое полезное при разборе
        tail = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
    except OSError:
        tail = []
    if tail:
        lines += ["", "Журнал программы (последние строки):"] + tail
    if code == 200:
        c2, b2 = _with_timeout(lambda: remote.forward("POST", "/api/message", jbytes({"text": "/diag"})), 20, (0, b""))
        try:
            reply = json.loads(b2).get("reply") if c2 == 200 else ""
        except ValueError:
            reply = ""
        lines += ["", reply or f"(диагностика Троя не пришла за 20 с, код {c2})"]
    return scrub("\n".join(lines))


def bug_report(remote: "Remote", text: str) -> dict:
    """Текст для GitHub Issues + ссылка «новая задача» с ним. Ссылка не длиннее ~7500 символов:
    если подробности не влезают — обрезаем, а полный отчёт программа сохраняет в файл."""
    from urllib.parse import quote
    text = (text or "").strip()
    first = re.sub(r"\s+", " ", text.splitlines()[0] if text else "")[:80]
    title = "Ошибка: " + (first or "без описания")
    try:
        diag = bug_details(remote)
    except Exception as e:
        diag = f"(подробности не собрались: {e})"
    head = (f"**Что случилось**\n{text or '(не описано)'}\n\n**Версия:** Troy {APP_VERSION}\n\n"
            + ("**Скриншот:** вставьте сюда — он уже в буфере обмена (Ctrl+V)\n\n" if BUG["shot"] else ""))

    def body_with(d: str) -> str:
        return head + "<details><summary>Подробности для разработчика</summary>\n\n```\n" + d + "\n```\n</details>\n"

    full = body_with(diag)
    url = lambda b: f"{BUG_REPO}/issues/new?labels=bug&title={quote(title)}&body={quote(b)}"
    d, trimmed = diag, False
    while len(url(body_with(d))) > 7500 and len(d) > 200:
        d, trimmed = d[: int(len(d) * 0.8)], True
    body = body_with(d + ("\n… (обрезано — полный отчёт сохранён в файл, приложите его к задаче)" if trimmed else ""))
    return {"url": url(body), "full": full, "trimmed": trimmed, "title": title}


def bug_shot_window() -> bytes:
    """Снимок окна Троя (PNG). Окно отчёта при этом видно — это нормально, главное, что видно и чат."""
    import ctypes
    import io
    from ctypes import wintypes
    from PIL import ImageGrab
    hwnd = find_window()
    if not hwnd:
        raise RuntimeError("окно Троя не найдено")
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def image_to_clipboard(png: bytes) -> bool:
    """Скриншот — в буфер обмена: на странице GitHub его вставляют в задачу через Ctrl+V."""
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png)
        ps = ("Add-Type -AssemblyName System.Windows.Forms, System.Drawing; "
              f"$i = [System.Drawing.Image]::FromFile('{f.name}'); [System.Windows.Forms.Clipboard]::SetImage($i); $i.Dispose()")
        subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception as e:
        log.warning("Скриншот в буфер: %s", e)
        return False


def bug_save(rep_: dict) -> str:
    """Отчёт файлом (без GitHub): архив с описанием, скриншотом и журналом — в «Загрузки\\Трой»."""
    import zipfile
    import pc_control
    folder = pc_control._downloads() / "Трой"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"SomeTroy — отчёт об ошибке {time.strftime('%Y-%m-%d %H-%M')}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("отчёт.md", rep_["full"])
        if BUG["shot"]:
            z.writestr("скриншот.png", BUG["shot"])
    try:
        subprocess.Popen(["explorer.exe", "/select,", str(target)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass
    return str(target)


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
            elif path == "/server/repair":
                cfg_ = load_config()
                self.send(200, jbytes({**REPAIR, "mode": cfg_.get("mode") or ("vps" if cfg_.get("server") else ""),
                                       "host": remote.base.split("://", 1)[-1].split(":")[0],
                                       "outdated": _with_timeout(lambda: server_outdated(remote), 6, "")
                                       if "full=1" in (url.query or "") and remote.logged_in and REPAIR["state"] != "run" else ""}))
            elif path == "/update/check":
                self.send(200, jbytes(check_update()))
            elif path == "/update/status":
                self.send(200, jbytes(UPD))
            elif path == "/win":                         # окно без системной полосы: развёрнуто ли
                import window_frame
                hwnd = find_window() if os.name == "nt" else None
                sh = _shell()
                self.send(200, jbytes({"frameless": window_frame.STATE["enabled"] and window_frame.frameless(hwnd),
                                       "shell": bool(sh and sh.started()), "max": window_frame.is_max(hwnd)}))
            elif path == "/tabs":                        # вкладки сайтов в шапке
                sh = _shell()
                if sh and sh.started():
                    self.send(200, jbytes({"tabs": sh.listing()}))
                else:
                    import site_tabs
                    self.send(200, jbytes({"tabs": site_tabs.listing()}))
            elif path == "/whatsnew":
                self.send(200, jbytes({**whats_new(), "product": PRODUCT}))
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
            if path == "/micsettings":                      # «Открыть настройки микрофона» (Windows)
                try:
                    os.startfile("ms-settings:privacy-microphone")
                except Exception as e:
                    log.warning("Настройки микрофона: %s", e)
                self.send(200, b"{}")
                return
            if path == "/micinfo":                          # почему не дали микрофон — в журнал (для разбора)
                try:
                    info = json.loads(body or b"{}")
                except ValueError:
                    info = {}
                sh = _shell()
                log.warning("Микрофон не дали: %s %s (окно: %s, Windows %s)", info.get("name"), info.get("message"),
                            "WebView2" if sh and sh.started() else "Edge", getattr(sys, "getwindowsversion", lambda: "?")())
                self.send(200, b"{}")
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
            if path == "/server/repair":                 # пароль сервера — только сюда (127.0.0.1), не в чат и не в нейросеть
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    data = {}
                if REPAIR["state"] != "run":
                    threading.Thread(target=repair_server, args=(remote, str(data.get("address") or ""),
                                                                 str(data.get("password") or "")), daemon=True).start()
                self.send(200, b"{}")
                return
            if path == "/update/install":
                if UPD["state"] not in ("download", "install"):
                    threading.Thread(target=install_update, daemon=True).start()
                self.send(200, b"{}")
                return
            if path == "/win":                           # кнопки окна в шапке Троя: свернуть, развернуть, закрыть, тянуть
                import window_frame
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    data = {}
                hwnd = find_window() if os.name == "nt" else None
                act = str(data.get("action") or "")
                if hwnd:
                    if act == "min":
                        window_frame.minimize(hwnd)
                    elif act == "max":
                        window_frame.toggle_max(hwnd)
                    elif act == "drag":
                        window_frame.drag(hwnd, str(data.get("edge") or ""))
                    elif act == "close":
                        threading.Thread(target=close_window, daemon=True).start()
                self.send(200, jbytes({"max": window_frame.is_max(hwnd)}))
                return
            if path in ("/tabs/show", "/tabs/close", "/tabs/layout") and _shell() and _shell().started():
                sh = _shell()
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    data = {}
                if "top" in data:
                    sh.set_top(data.get("top"))
                if path == "/tabs/show":
                    sh.show_tab(data.get("id"))
                elif path == "/tabs/close":
                    sh.close(data.get("id"))
                self.send(200, jbytes({"tabs": sh.listing()}))
                return
            if path in ("/tabs/show", "/tabs/close", "/tabs/layout"):
                import site_tabs
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    data = {}
                if "top" in data:                       # высота шапки с вкладками — в настоящих пикселях
                    site_tabs.STATE["top"] = float(data.get("top") or 0)
                site_tabs.STATE["find_troy"] = find_window
                if path == "/tabs/show":
                    site_tabs.show(data.get("id"))
                elif path == "/tabs/close":
                    site_tabs.close(data.get("id"))
                self.send(200, jbytes({"tabs": site_tabs.listing()}))
                return
            if path == "/whatsnew/seen":
                save_settings(seen_version=APP_VERSION)
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
            if path in ("/bugreport", "/bugshot"):           # окно «Сообщить об ошибке»
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    data = {}
                if path == "/bugshot":                  # скриншот: снимок окна Троя, свой файл или убрать
                    import base64
                    try:
                        if data.get("clear"):
                            BUG["shot"] = b""
                        elif data.get("b64"):
                            BUG["shot"] = base64.b64decode(data["b64"])[:15 * 1024 * 1024]
                        else:
                            BUG["shot"] = bug_shot_window()
                        self.send(200, jbytes({"ok": True, "size": len(BUG["shot"]),
                                               "preview": base64.b64encode(BUG["shot"]).decode() if BUG["shot"] else ""}))
                    except Exception as e:
                        self.send(200, jbytes({"ok": False, "error": str(e)}))
                    return
                rep_ = bug_report(remote, str(data.get("text") or ""))
                action = data.get("action") or ("open" if data.get("open") else "")
                if action == "open":
                    if BUG["shot"]:
                        rep_["shot_in_clipboard"] = image_to_clipboard(BUG["shot"])
                    if rep_["trimmed"] or BUG["shot"]:
                        rep_["saved"] = bug_save(rep_)
                    webbrowser.open(rep_["url"])
                elif action == "save":
                    rep_["saved"] = bug_save(rep_)
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


WHATS_NEW = [
 {
  "version": "0.6.1",
  "added": [],
  "fixed": [
   "Меню, «Подключения» и окна Троя больше не прячутся под открытым сайтом — Трой сам переключается на чат.",
   "Окно: при открытом сайте его можно тянуть за края; щелчок по шапке развёрнутого окна его не сворачивает; окно не откроется за пределами экрана, если отключили монитор; развёрнутое окно открывается развёрнутым.",
   "«Повторить» не трогает набранный текст и вложения.",
   "Битрикс24: «да» выполняет все показанные действия (например, две задачи), а старое «Делаю?» не срабатывает на «да» по другому поводу.",
   "Трой не принимает за пароль обычные вопросы про пароли и не путает «логин» и «логотип» с просьбой сохранить переписку.",
   "Если до платной модели не достучаться, отвечает запасная. Напоминание «каждый месяц 31-го» не сползает на 28-е.",
   "После перезапуска сервера напоминания и ответы компьютеров снова приходят в открытое окно программы.",
   "Защита: ключи и адреса с ключами не попадают в журнал и в тексты ошибок; закрыты две лазейки в серверной части.",
   "Обновление из программы: если установщик не запустился, Трой скажет об этом, а не будет «ставить» бесконечно."
  ]
 },
 {
  "version": "0.6.0",
  "added": [
   "Новое окно Троя: теперь это своё окно программы (на движке WebView2), а не окно Edge. Белой полосы Windows нет ни в Windows 10, ни в 11 — кнопки «свернуть / развернуть / закрыть» в шапке Троя. Окно тянется за шапку, разворачивается двойным щелчком и растягивается за края.",
   "Сайты открываются настоящими вкладками внутри окна Троя: «💬 Чат | 🌐 сайт ×». Вход на сайтах запоминается; 🛡 — сайт открыт через сервер.",
   "Кнопка «Повторить» у сообщений: у твоего — отправить его ещё раз, у ответа Троя — ещё раз задать вопрос, на который он отвечал.",
   "Своя модель: в «Подключениях» можно подключить платную модель — ChatGPT, Claude, Gemini, DeepSeek или через OpenRouter. Бесплатные модели остаются запасными. (Нужно обновить Троя на сервере.)"
  ],
  "fixed": [
   "Шапка аккуратно помещается в узком окне.",
   "Битрикс24: задачи ставятся надёжно — Трой показывает, что именно поставит, и после «да» делает это сам; исполнителя можно назвать по имени. Аналитика: правильные названия стадий, продажи считаются по дате закрытия, суммы — по валютам.",
   "Нет доступа к микрофону — Трой пишет, в чём дело, и даёт кнопку «Открыть настройки микрофона» Windows."
  ]
 },
 {
  "version": "0.5.2",
  "added": [
   "Программа теперь называется Troy (проект — SomeTroy). Ассистента по-прежнему зовут Трой.",
   "Сайты открываются вкладками прямо в окне Троя: «💬 Чат | 🌐 сайт ×» под шапкой. Переключайтесь между чатом и сайтами, закрывайте крестиком. 🛡 — сайт открыт через сервер."
  ],
  "fixed": []
 },
 {
  "version": "0.5.1",
  "added": [
   "«🛠 Обновить и починить сервер» — прямо в программе (ⓘ «О программе» и меню у значка возле часов): Трой на сервере обновится до версии программы, порт и сертификат проверятся. Нужен только пароль сервера.",
   "Если связи с сервером нет, Трой сам предложит «Починить»."
  ],
  "fixed": []
 },
 {
  "version": "0.5.0",
  "added": [
   "«Проверить обновления» в «О программе»: как только выходит новая версия, Трой предложит поставить её прямо из программы — без поиска установщика.",
   "Отчёт об ошибке со скриншотом: «Снимок окна Троя» или своя картинка. На GitHub скриншот вставляется через Ctrl+V.",
   "«Сохранить в файл» — отчёт архивом, если нет аккаунта GitHub: его можно просто переслать разработчику.",
   "«Открой ya.ru» — сайт с адресом открывается сразу, без раздумий нейросети."
  ],
  "fixed": [
   "Отчёт об ошибке больше не зависает: собирается за секунды и подробнее — с журналом программы и версией Троя на сервере.",
   "Если Трой не нашёл программу с таким названием, он подскажет: «если это сайт — скажи „открой сайт …“»."
  ]
 },
 {
  "version": "0.4.1",
  "added": [
   "Окно «Что нового» — вы его сейчас читаете: после каждого обновления Трой коротко рассказывает, что изменилось.",
   "Пуск → «Трой — обновить и починить сервер»: обновляет Троя на вашем сервере до версии программы, открывает порт и обновляет сертификат.",
   "Программа сама замечает, что Трой на сервере устарел, и подсказывает, как его обновить."
  ],
  "fixed": [
   "Трой всегда спрашивает «напрямую или через сервер», когда сайт открывается впервые, — нейросеть больше не выбирает за вас."
  ]
 },
 {
  "version": "0.4.0",
  "added": [
   "Сайты в отдельном окне браузера: «открой сайт рутрекер». Напрямую или через сервер — как VPN; выбор запоминается для каждого сайта.",
   "Кнопка «Ответить» под сообщением Троя — он поймёт, о чём именно речь. В Telegram — обычный ответ на сообщение бота."
  ],
  "fixed": []
 },
 {
  "version": "0.3.0",
  "added": [
   "Светлая и тёмная тема и 7 цветов — кнопка с палитрой в шапке.",
   "Переезд на свой сервер: 🔌 «Подключения» → «Сервер (VPS)» — только IP и пароль, переписка и идеи переезжают.",
   "Файлы между компьютерами: «перекинь отчёт с одного ПК на другой» (до 50 МБ).",
   "«Сохрани переписку» или /log — переписка файлом. Идеи — в ваш репозиторий GitHub.",
   "Понимает текст в неправильной раскладке: «ghbdtn» → «привет».",
   "🐞 «Сообщить об ошибке» и /bug; пароли и ключи из чата не уходят в нейросеть."
  ],
  "fixed": [
   "Установка на сервер сама открывает порт, работает на старых серверах (Python 3.8) и делает правильный сертификат.",
   "«Открой проводник» открывает Проводник, а не Internet Explorer. Одинаковые идеи не записываются дважды."
  ]
 }
]


def _vnum(v: str) -> tuple:
    return tuple(int(x) for x in str(v).split(".") if x.isdigit())


def whats_new() -> dict:
    """Что показать после обновления: версии новее последней увиденной (при первом показе — только текущую)."""
    seen = load_settings().get("seen_version")
    notes = [n for n in WHATS_NEW if _vnum(n["version"]) <= _vnum(APP_VERSION)
             and (_vnum(n["version"]) > _vnum(seen) if seen else n["version"] == APP_VERSION)]
    return {"version": APP_VERSION, "notes": notes[:3]}


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
                import window_frame
                if user32.IsZoomed(hwnd) or window_frame.is_max(hwnd):
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


def _shell():
    """Окно Троя на WebView2 (своё окно, без полосы Windows) — если оно работает на этом компьютере."""
    if os.name != "nt":
        return None
    try:
        import shell_window
    except Exception:
        return None
    return shell_window


def open_window(url: str) -> None:
    sh = _shell()
    if sh and (sh.started() or sh.available()):
        if sh.started():
            sh.show(url)
            return
        sh.S["on_fail"] = lambda: _open_edge_window(url)
        sh.start(url, DATA_DIR, WINDOW_TITLE, load_settings().get("window") or {})
        return
    _open_edge_window(url)


def _open_edge_window(url: str) -> None:
    """Запасной путь: окно Edge в режиме приложения (полосу Windows снимаем снаружи)."""
    if os.name == "nt":
        import window_frame
        window_frame.keep(find_window)
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
    """Окно Троя: своё окно на WebView2, а запасное (Edge в режиме приложения) — по заголовку."""
    sh = _shell()
    if sh and sh.started():
        return sh.hwnd_of()
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
    visible = [h for h in found if user32.IsWindowVisible(h)]     # служебные окна Edge с тем же заголовком — мимо
    return (visible or found or [None])[0]


def window_open() -> bool:
    sh = _shell()
    if sh and sh.started():
        return sh.visible()
    return bool(last_alive) and time.time() - last_alive < 12


def show_window() -> None:
    """Показать окно: развернуть, если открыто, иначе открыть заново."""
    sh = _shell()
    if sh and sh.started():
        sh.show()
        return
    hwnd = find_window() if window_open() else None
    if hwnd:
        import ctypes
        ctypes.windll.user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    else:
        open_window(APP_URL)


def close_window() -> None:
    sh = _shell()
    if sh and sh.started():
        sh.hide()
        return
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


# ── окно браузера: напрямую или «через сервер» (как VPN, через туннель к своему Трою) ──
_PROXY = {"port": 0, "remote": None}


def _tunnel(remote: "Remote", host: str, port: int):
    """Соединение с сайтом через сервер Троя: TLS к серверу (его сертификат) + CONNECT с сессией программы."""
    import socket
    import ssl
    from urllib.parse import urlparse
    u = urlparse(remote.base)
    ctx = ssl.create_default_context(cafile=str(CERT))
    raw = socket.create_connection((u.hostname, u.port or 443), timeout=20)
    s = ctx.wrap_socket(raw, server_hostname=u.hostname)
    s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
              f"Authorization: Bearer {remote.session}\r\n\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk:
            break
        head += chunk
    if not head.startswith((b"HTTP/1.1 200", b"HTTP/1.0 200")):
        s.close()
        raise OSError(head.split(b"\r\n")[0].decode("latin-1", "replace") or "сервер не открыл туннель")
    return s


def start_proxy(remote: "Remote") -> int:
    """Маленький прокси на 127.0.0.1 для окна браузера: всё уходит через сервер Троя."""
    import socketserver
    from urllib.parse import urlsplit
    _PROXY["remote"] = remote
    if _PROXY["port"]:
        return _PROXY["port"]

    class H(socketserver.BaseRequestHandler):
        def handle(self):
            import pc_control
            c = self.request
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 65536:
                chunk = c.recv(4096)
                if not chunk:
                    return
                head += chunk
            line, _, rest = head.partition(b"\r\n")
            try:
                method, target, ver = line.decode("latin-1").split(" ", 2)
            except ValueError:
                return
            try:
                if method.upper() == "CONNECT":
                    host, _, port = target.rpartition(":")
                    up = _tunnel(_PROXY["remote"], host, int(port or 443))
                    c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                else:                                   # обычный http://… — тоже через туннель
                    u = urlsplit(target)
                    up = _tunnel(_PROXY["remote"], u.hostname, u.port or 80)
                    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                    up.sendall(f"{method} {path} {ver}\r\n".encode("latin-1") + rest)
            except OSError as e:
                log.warning("Браузер через сервер: %s", e)
                c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            pc_control.relay(c, up)

    class S(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="troy-proxy").start()
    _PROXY["port"] = srv.server_address[1]
    return _PROXY["port"]


def open_site(remote: "Remote", arg: str):
    """Отдельное окно браузера со своим профилем: вход на сайтах запоминается, основной браузер не трогаем."""
    info = json.loads(arg or "{}")
    url, mode = info.get("url", ""), info.get("mode", "direct")
    host = url.split("://", 1)[-1].split("/")[0]
    sh = _shell()
    if sh and (sh.started() or sh.available()):  # вкладкой прямо в окне Троя (WebView2)
        port = 0
        if mode == "server":
            if not str(remote.base).startswith("https://"):
                return False, "через сервер можно, только когда Трой живёт на сервере (VPS)"
            port = start_proxy(remote)
        if not sh.visible():
            open_window(APP_URL)
            sh.wait_ready(15)
        if sh.started():
            return sh.open_tab(url, host, mode, port)
    browser = find_browser()
    if not browser:
        if mode == "server":
            return False, "для режима «через сервер» нужен Edge или Chrome — не нашёл их на компьютере"
        webbrowser.open(url)
        return True, f"открыл {host} в браузере по умолчанию"
    args = [browser, f"--user-data-dir={DATA_DIR / ('browser-server' if mode == 'server' else 'browser')}",
            "--no-first-run", "--no-default-browser-check"]
    tabbed = sys.platform == "win32"
    if mode == "server":
        if not str(remote.base).startswith("https://"):
            return False, "через сервер можно, только когда Трой живёт на сервере (VPS)"
        port = start_proxy(remote)
        args += [f"--proxy-server=http://127.0.0.1:{port}", "--proxy-bypass-list=<-loopback>"]
    if tabbed:                                   # вкладкой внутри окна Троя (окно сайта без рамки, под шапкой Троя)
        import site_tabs
        site_tabs.STATE["find_troy"] = find_window
        if not window_open():                    # окно Троя закрыто в трей — открываем, вкладке нужно куда встать
            open_window(APP_URL)
            for _ in range(40):
                if find_window():
                    break
                time.sleep(0.25)
        try:
            return site_tabs.open_tab(args + [f"--app={url}"], url, host, mode)
        except Exception as e:
            log.warning("Вкладка сайта: %s", e)
    subprocess.Popen(args + ["--new-window", url], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return True, f"открыл {host} {'через сервер' if mode == 'server' else 'напрямую'}"


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
                        log.info("Команда: %s", cmd["action"])       # без подробностей: адреса и имена файлов в журнал не пишем
                        try:
                            def late(ok2, text2, cid=cmd["id"]):      # например, «выключение отменили на компьютере»
                                remote.forward("POST", "/api/pc/result",
                                               jbytes({"id": cid, "ok": ok2, "text": text2, "late": True}))
                            if cmd["action"] == "file_send":      # переслать файл на другой компьютер
                                ok, text = pc_control.file_send(remote, cmd["id"], cmd.get("arg", ""))
                            elif cmd["action"] == "file_get":     # забрать файл, присланный с другого компьютера
                                ok, text = pc_control.file_get(remote, cmd.get("arg", ""))
                            elif cmd["action"] == "open_site":    # окно браузера: напрямую или через сервер
                                ok, text = open_site(remote, cmd.get("arg", ""))
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
        save_settings(seen_version=APP_VERSION)

    def finish_vps(info: dict) -> None:
        cert = DATA_DIR / "troy-cert.pem"
        cert.write_text(info["cert"], encoding="utf-8")
        server = f"https://{info['ip']}:{info['port']}"
        (DATA_DIR / "config.json").write_text(json.dumps({"mode": "vps", "server": server}), encoding="utf-8")
        save_settings(seen_version=APP_VERSION)
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
    global CERT                                   # сертификат сервера — в папку данных: папку программы затирает установщик
    CERT = DATA_DIR / "troy-cert.pem"
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


# ── «Обновить и починить сервер» прямо из программы: тот же сценарий, что в fix-connection.bat, но без консоли ──
REPAIR = {"state": "", "log": [], "error": ""}


def _repair_script() -> str:
    """Серверная часть сценария — берём из fix-connection.bat, чтобы обе дороги делали одно и то же."""
    for bat in (APP_DIR / "tools" / "fix-connection.bat", APP_DIR.parent / "tools" / "fix-connection.bat"):
        if bat.exists():
            t = bat.read_text(encoding="utf-8")
            return t[t.rfind("#SERVER-SCRIPT#"):].replace("\r", "")
    raise RuntimeError("не нашёл tools\\fix-connection.bat рядом с программой — переустанови Троя")


def repair_server(remote: "Remote", address: str, password: str) -> None:
    """Обновить Троя на сервере до версии программы, открыть порт 443, обновить сертификат, переключить программу."""
    import paramiko
    import setup_wizard
    say = lambda t: (REPAIR["log"].append(t), log.info("Сервер: %s", t))
    REPAIR.update(state="run", log=[], error="")
    try:
        host = remote.base.split("://", 1)[-1].split(":")[0]
        ip, user, ssh_port = setup_wizard.parse_vps_address(address or host) or (host, "root", 22)
        script = _repair_script()
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        say(f"Подключаюсь к {ip}…")
        try:
            cli.connect(ip, port=ssh_port, username=user, password=password, timeout=20, look_for_keys=False,
                        allow_agent=False, banner_timeout=20, auth_timeout=20)
        except paramiko.AuthenticationException:
            raise RuntimeError(f"сервер не принял пароль пользователя {user}")
        try:
            cli.exec_command("rm -rf /tmp/troyfix && mkdir -p /tmp/troyfix")[1].channel.recv_exit_status()
            sftp = cli.open_sftp()
            for name in ("bot.py", "bitrix.py", "connectors.py", "desktop_api.py", "requirements.txt"):
                with sftp.open(f"/tmp/troyfix/{name}", "wb") as f:
                    f.write((SERVER_SRC / name).read_bytes().replace(b"\r\n", b"\n"))
            with sftp.open("/tmp/troyfix/fix.sh", "wb") as f:
                f.write(script.encode("utf-8"))
            say(f"Обновляю Троя на сервере до {bundled_server_version()} — это займёт до пары минут…")
            _in, out, _err = cli.exec_command(f"bash /tmp/troyfix/fix.sh {ip} 2>&1", timeout=900)
            text = []
            in_cert = False
            for raw in iter(out.readline, ""):
                for line in raw.splitlines():
                    line = line.rstrip()
                    text.append(line)
                    in_cert = (in_cert or line == "CERT-BEGIN") and line != "CERT-END"
                    if line and not in_cert and line not in ("CERT-END", "TROY_OK"):
                        say(line)
        finally:
            cli.close()
        full = "\n".join(text)
        if "NOTROY" in full:
            raise RuntimeError("Трой на этом сервере не найден (/opt/sometroy) — проверь адрес")
        if "BUSY443" in full:
            raise RuntimeError("порт 443 на сервере занят другой программой — открой порт 8443 в панели хостинга")
        if "TROY_OK" not in full:
            raise RuntimeError("Трой на сервере не запустился — строки выше пригодятся разработчику")
        m = re.search(r"CERT-BEGIN\s*(-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----)", full, re.S)
        if not setup_wizard.tcp_open(ip, 443):
            raise RuntimeError("Трой на сервере обновлён, но порт 443 закрыт в панели хостинга — открой его (TCP) и повтори")
        if m:
            (DATA_DIR / "troy-cert.pem").write_text(m.group(1) + "\n", encoding="utf-8")
        cfg = load_config()
        cfg.update(mode="vps", server=f"https://{ip}:443")
        (DATA_DIR / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        if CONFIG != DATA_DIR / "config.json" and CONFIG.exists():   # старая раскладка: настройки рядом с программой
            try:
                CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
            except OSError:
                pass
        say("✅ Готово: Трой на сервере обновлён, связь настроена. Программа перезапустится через пару секунд.")
        REPAIR["state"] = "done"
        if APP_HOOKS["restart"]:
            threading.Timer(3, APP_HOOKS["restart"]).start()
    except Exception as e:
        REPAIR.update(state="error", error=str(e))
        log.warning("Обновление сервера: %s", e)


def show_diag() -> None:
    """Из трея: открыть окно и сразу показать диагностику."""
    hwnd = find_window() if window_open() else None
    if hwnd:
        close_window()
        time.sleep(0.8)
    open_window(APP_URL + "?diag=1")


def show_repair() -> None:
    """Из трея: открыть окно и сразу «Обновить и починить сервер»."""
    if window_open():
        close_window()
        time.sleep(0.8)
    open_window(APP_URL + "?repair=1")


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

    if os.name == "nt" and _shell() and _shell().available():
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass

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
        sh = _shell()
        if sh and sh.started():
            sh.exit_app()
        close_window()
        icon.stop()

    def on_logout(icon, _item):           # выйти из аккаунта: в следующий раз спросит пароль
        remote.logout()
        icon.notify("Ты вышел из аккаунта. Чтобы снова пользоваться Троем, введи пароль.", WINDOW_TITLE)
        show_window()

    def on_about(_icon, _item):
        import ctypes
        threading.Thread(target=lambda: ctypes.windll.user32.MessageBoxW(
            0, f"{PRODUCT} {APP_VERSION}\nЛичный ассистент — в Telegram и на компьютере.\n\n"
               "«Сей модуль является симбиозом дружеского взаимодействия лысого бородатого человека и сверхумной, но очень приятной машины»\n\n"
               "Авторы: Claude и Egor Zhidetskiy\n© 2026 Claude и Egor Zhidetskiy",
            "О программе", 0x40 | 0x10000), daemon=True).start()

    icon = pystray.Icon("troy", Image.open(RES_DIR / "favicon.png"), PRODUCT, menu=pystray.Menu(
        pystray.MenuItem("Открыть Троя", lambda icon, item: show_window(), default=True),
        pystray.MenuItem("О программе", on_about),
        pystray.MenuItem("Диагностика", lambda icon, item: show_diag()),
        pystray.MenuItem("Обновить и починить сервер", lambda icon, item: show_repair(),
                         visible=lambda item: load_config().get("mode") != "local"),
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
        threading.Thread(target=server_check_loop, args=(remote, icon), daemon=True).start()
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
