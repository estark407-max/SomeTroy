"""Трой на Mac: окно, значок в строке меню, уведомления и прочее, что в macOS устроено иначе, чем в Windows.

Окно Троя на Mac — это окно браузера на движке Chromium (Chrome, Edge, Яндекс, Brave…) в режиме приложения:
без адресной строки и вкладок, со своим отдельным профилем. В таком окне работают микрофон, звук напоминаний
и всё остальное так же, как в Windows. Если такого браузера нет — Трой открывается вкладкой в Safari.
Значок Троя живёт в строке меню вверху экрана (как значок у часов в Windows).
"""
import logging
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("troy.mac")

BROWSERS = ["Google Chrome.app/Contents/MacOS/Google Chrome", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "Yandex.app/Contents/MacOS/Yandex", "Brave Browser.app/Contents/MacOS/Brave Browser",
            "Chromium.app/Contents/MacOS/Chromium", "Vivaldi.app/Contents/MacOS/Vivaldi",
            "Opera.app/Contents/MacOS/Opera"]
WIN = {"proc": None}
AGENT = Path.home() / "Library" / "LaunchAgents" / "io.github.sometroy.troy.plist"


def data_dir(name: str) -> Path:
    return Path.home() / "Library" / "Application Support" / name


def find_browser():
    for base in ("/Applications", str(Path.home() / "Applications")):
        for rel in BROWSERS:
            p = Path(base) / rel
            if p.exists():
                return str(p)
    return None


def osa(script: str, timeout: int = 30) -> tuple:
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout if r.returncode == 0 else r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def _q(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def dialog(text: str, title: str = "Troy", icon: str = "note") -> None:
    osa(f'display dialog "{_q(text)}" with title "{_q(title)}" buttons {{"OK"}} default button "OK" with icon {icon}', 600)


def notify(text: str, title: str = "Трой") -> None:
    osa(f'display notification "{_q(text)[:300]}" with title "{_q(title)}"', 10)


def open_window(browser: str, url: str, profile: Path, size: tuple) -> None:
    """Окно Троя: браузер в режиме приложения со своим профилем (вход и разрешение на микрофон запоминаются)."""
    proc = subprocess.Popen([
        browser, f"--app={url}", f"--user-data-dir={profile}", f"--window-size={size[0]},{size[1]}",
        "--no-first-run", "--no-default-browser-check", "--disable-features=Translate",
        "--autoplay-policy=no-user-gesture-required",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    old = WIN["proc"]
    if old is None or old.poll() is not None:        # первый запуск профиля — это и есть его главный процесс
        WIN["proc"] = proc


def activate() -> bool:
    """Вывести окно Троя вперёд (если его браузер ещё запущен)."""
    proc = WIN["proc"]
    if proc is None or proc.poll() is not None:
        return False
    try:
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(proc.pid)
        if app is None:
            return False
        app.unhide()
        return bool(app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))
    except Exception as e:
        log.debug("activate: %s", e)
        return False


def close_window() -> None:
    proc = WIN["proc"]
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    WIN["proc"] = None


def reveal(path) -> None:
    """Показать файл в Finder."""
    try:
        subprocess.Popen(["open", "-R", str(path)])
    except OSError:
        pass


def open_file(path) -> None:
    try:
        subprocess.Popen(["open", str(path)])
    except OSError:
        pass


def mic_settings() -> None:
    subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"])


def screenshot() -> bytes:
    """Снимок экрана (macOS может один раз спросить разрешение на запись экрана)."""
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "shot.png"
        subprocess.run(["screencapture", "-x", str(target)], timeout=20)
        if not target.exists():
            raise RuntimeError("macOS не дала сделать снимок экрана (Системные настройки → Конфиденциальность → Запись экрана)")
        return target.read_bytes()


def image_to_clipboard(png: bytes) -> bool:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png)
    ok, out = osa(f'set the clipboard to (read (POSIX file "{_q(f.name)}") as «class PNGf»)', 20)
    try:
        os.unlink(f.name)
    except OSError:
        pass
    if not ok:
        log.warning("Скриншот в буфер: %s", out)
    return ok


def autostart(enable: bool = None) -> bool:
    """Запускать Троя при входе в систему (значком в строке меню). Без аргумента — включено ли сейчас."""
    if enable is None:
        return AGENT.exists()
    if not enable:
        try:
            AGENT.unlink()
        except OSError:
            pass
        return False
    args = [sys.executable] + ([] if getattr(sys, "frozen", False) else [str(Path(sys.argv[0]).resolve())]) + ["--tray"]
    AGENT.parent.mkdir(parents=True, exist_ok=True)
    with open(AGENT, "wb") as f:
        plistlib.dump({"Label": AGENT.stem, "ProgramArguments": args, "RunAtLoad": True,
                       "ProcessType": "Interactive"}, f)
    return True
