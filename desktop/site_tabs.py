"""Сайты вкладками внутри окна Троя.

Сайт нельзя показать прямо на странице Троя (почти все сайты запрещают встраивать себя — защита от подделок),
поэтому каждый сайт открывается своим окном Edge в режиме приложения (без адресной строки и вкладок браузера),
а мы «пристёгиваем» это окно к окну Троя:
- убираем у него рамку и заголовок;
- делаем окно Троя его владельцем — сайт всегда поверх Троя, сворачивается и закрывается вместе с ним;
- держим его ровно под шапкой Троя с вкладками «💬 Чат | 🌐 сайт ×» и двигаем вслед за окном Троя.
Переключение вкладок — показать/спрятать окно сайта. Работает только в Windows.
"""
import ctypes
import itertools
import logging
import subprocess
import threading
import time

log = logging.getLogger("troy.tabs")

TABS: dict = {}                      # id -> {"hwnd", "url", "host", "mode", "title"}
STATE = {"active": None, "top": 0, "find_troy": None, "watching": False}
_ids = itertools.count(1)
_lock = threading.Lock()

GWL_STYLE, GWLP_HWNDPARENT = -16, -8
WS_CAPTION, WS_THICKFRAME, WS_SYSMENU, WS_MINIMIZEBOX, WS_MAXIMIZEBOX = 0x00C00000, 0x00040000, 0x00080000, 0x00020000, 0x00010000
SWP_NOACTIVATE, SWP_SHOWWINDOW, SWP_FRAMECHANGED, SWP_NOZORDER = 0x0010, 0x0040, 0x0020, 0x0004
SW_HIDE, SW_SHOWNA, SW_RESTORE = 0, 8, 9
WM_CLOSE = 0x0010


def _user32():
    u = ctypes.windll.user32
    try:                                         # координаты — в настоящих пикселях экрана (масштаб 125%, 150%…)
        u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    return u


def _set_long(u, hwnd, index, value):
    fn = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    return fn(hwnd, index, value)


def _get_style(u, hwnd) -> int:
    fn = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
    fn.argtypes, fn.restype = [ctypes.c_void_p, ctypes.c_int], ctypes.c_ssize_t
    return fn(hwnd, GWL_STYLE)


def _browser_windows() -> set:
    """Видимые окна Edge/Chrome с заголовком."""
    from ctypes import wintypes
    u = _user32()
    found = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(hwnd, cls, 64)
        if cls.value.startswith("Chrome_WidgetWin") and u.IsWindowVisible(hwnd) and u.GetWindowTextLengthW(hwnd) > 0:
            found.add(hwnd)
        return True

    u.EnumWindows(cb, 0)
    return found


def _title(hwnd) -> str:
    u = _user32()
    buf = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def _client_rect(hwnd) -> tuple:
    """Клиентская часть окна Троя в координатах экрана: (x, y, ширина, высота)."""
    from ctypes import wintypes
    u = _user32()
    r = wintypes.RECT()
    u.GetClientRect(hwnd, ctypes.byref(r))
    pt = wintypes.POINT(0, 0)
    u.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, r.right - r.left, r.bottom - r.top


def _place(tab: dict) -> None:
    troy = STATE["find_troy"]() if STATE["find_troy"] else None
    u = _user32()
    if not troy or u.IsIconic(troy) or not u.IsWindowVisible(troy):
        u.ShowWindow(tab["hwnd"], SW_HIDE)
        return
    x, y, w, h = _client_rect(troy)
    top = max(0, int(STATE["top"]))
    u.SetWindowPos(tab["hwnd"], 0, x, y + top, w, max(100, h - top), SWP_NOACTIVATE | SWP_NOZORDER | SWP_SHOWWINDOW)


def _embed(hwnd, troy) -> None:
    """Окно сайта — без рамки и «принадлежит» окну Троя."""
    u = _user32()
    style = _get_style(u, hwnd)
    _set_long(u, hwnd, GWL_STYLE, style & ~(WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX))
    _set_long(u, hwnd, GWLP_HWNDPARENT, troy)
    u.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | SWP_NOZORDER | SWP_FRAMECHANGED | SWP_NOACTIVATE)


def open_tab(args: list, url: str, host: str, mode: str) -> tuple:
    """Запустить окно сайта и пристегнуть его к Трою вкладкой. (получилось, текст)"""
    troy = STATE["find_troy"]() if STATE["find_troy"] else None
    before = _browser_windows()
    subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    hwnd = None
    deadline = time.time() + 15
    while time.time() < deadline and not hwnd:
        time.sleep(0.3)
        new = [w for w in _browser_windows() - before if w != troy]
        hwnd = new[0] if new else None
    if not hwnd or not troy:
        return True, f"открыл {host} {'через сервер' if mode == 'server' else 'напрямую'} (отдельным окном)"
    time.sleep(0.4)                              # даём окну дорисоваться, прежде чем снимать рамку
    _embed(hwnd, troy)
    with _lock:
        tid = next(_ids)
        TABS[tid] = {"hwnd": hwnd, "url": url, "host": host, "mode": mode, "title": host}
    show(tid)
    _watch()
    return True, f"открыл {host} {'через сервер' if mode == 'server' else 'напрямую'} — вкладкой в окне Троя"


def show(tid) -> None:
    """Показать вкладку сайта (tid) или чат (None)."""
    u = _user32()
    with _lock:
        STATE["active"] = tid if tid in TABS else None
        for i, t in TABS.items():
            if i != STATE["active"]:
                u.ShowWindow(t["hwnd"], SW_HIDE)
        tab = TABS.get(STATE["active"])
    if tab:
        _place(tab)
        u.SetForegroundWindow(tab["hwnd"])
    elif STATE["find_troy"]:
        troy = STATE["find_troy"]()
        if troy:
            u.SetForegroundWindow(troy)


def close(tid) -> None:
    u = _user32()
    with _lock:
        tab = TABS.pop(tid, None)
        if STATE["active"] == tid:
            STATE["active"] = None
    if tab:
        u.PostMessageW(tab["hwnd"], WM_CLOSE, 0, 0)


def listing() -> list:
    with _lock:
        return [{"id": i, "title": t["title"], "host": t["host"], "mode": t["mode"], "active": i == STATE["active"]}
                for i, t in TABS.items()]


def _watch() -> None:
    """Следим за окнами: двигаем сайт вслед за Троем, убираем закрытые вкладки, обновляем заголовки."""
    if STATE["watching"]:
        return
    STATE["watching"] = True

    def loop():
        u = _user32()
        while True:
            try:
                with _lock:
                    for i in [i for i, t in TABS.items() if not u.IsWindow(t["hwnd"])]:
                        TABS.pop(i, None)
                        if STATE["active"] == i:
                            STATE["active"] = None
                    for t in TABS.values():
                        t["title"] = (_title(t["hwnd"]) or t["host"])[:60]
                    tab = TABS.get(STATE["active"])
                if tab:
                    _place(tab)
            except Exception:
                log.exception("Вкладки сайтов")
            time.sleep(0.25)

    threading.Thread(target=loop, daemon=True, name="troy-tabs").start()
