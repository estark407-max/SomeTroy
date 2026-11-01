"""Локальный режим: серверная часть Троя работает прямо внутри программы на этом компьютере.

Настройки и база — в %LOCALAPPDATA%\\SomeTroy\\server. Связь программы с ней — только через
127.0.0.1 (наружу ничего не открывается), поэтому сертификат не нужен.
"""
import os
import socket
import sys
import threading
import time
from pathlib import Path

_state = {"bot": None, "port": 0, "error": ""}


def server_home(data_dir: Path) -> Path:
    return data_dir / "server"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _import_bot(data_dir: Path, port: int = 0):
    """Подключить код сервера с настройками из data_dir. Один раз за запуск программы."""
    if _state["bot"]:
        return _state["bot"]
    home = server_home(data_dir)
    home.mkdir(parents=True, exist_ok=True)
    os.environ["TROY_HOME"] = str(home)
    os.environ["TROY_LOCAL"] = "1"
    os.environ["TROY_LOCAL_PORT"] = str(port or _free_port())
    src = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent / "server"))
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
    elif str(src) not in sys.path:
        sys.path.insert(0, str(src))
    import bot                                  # noqa: E402  (код сервера собран вместе с программой)
    _state["bot"] = bot
    return bot


def set_password_and_login(data_dir: Path, password: str, device: str) -> str:
    """Для мастера: задать пароль программы и сразу получить вход (без повторного ввода)."""
    bot = _import_bot(data_dir)
    bot.init_db()
    bot.AppAuth.set_password(password)
    session, _err = bot.AppAuth(notify=lambda _t: None).login(password, device, "127.0.0.1")
    return session or ""


def start(data_dir: Path, timeout: float = 20) -> int:
    """Запустить Троя в фоне; вернуть порт для программы. Исключение — если не поднялся."""
    bot = _import_bot(data_dir)
    port = int(os.environ["TROY_LOCAL_PORT"])

    def run():
        try:
            bot.serve()
        except BaseException as e:            # SystemExit из serve() — тоже сюда
            _state["error"] = str(e) or e.__class__.__name__

    threading.Thread(target=run, daemon=True, name="troy-server").start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _state["error"]:
            raise RuntimeError(_state["error"])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                _state["port"] = port
                return port
        except OSError:
            time.sleep(0.3)
    raise RuntimeError("локальный сервер Троя не запустился")
