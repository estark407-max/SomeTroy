"""Окно Троя без системной полосы Windows — всё в одном дизайне.

Окно Троя — это Edge в режиме приложения, и у него есть белая полоса Windows с «— □ ×».
Мы её убираем (снимаем у окна заголовок и рамку), а кнопки «свернуть / развернуть / закрыть»
рисует сама страница Троя в своей шапке. Перетаскивание за шапку и растягивание за края
делаем сами: страница говорит «начали тянуть», а мы двигаем окно за курсором, пока
левая кнопка мыши зажата. Работает только в Windows; где не получилось — остаётся обычная рамка.
"""
import ctypes
import logging
import threading
import time

log = logging.getLogger("troy.frame")

GWL_STYLE = -16
WS_CAPTION, WS_THICKFRAME = 0x00C00000, 0x00040000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_FRAMECHANGED, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0004, 0x0020, 0x0010
SW_MINIMIZE, SW_RESTORE = 6, 9
VK_LBUTTON = 0x01
MIN_W, MIN_H = 420, 320

STATE = {"max": {}, "busy": False, "enabled": True}     # max: hwnd -> прежнее место окна (x, y, w, h)


def _u():
    u = ctypes.windll.user32
    try:                                          # всё в настоящих пикселях экрана (масштаб 125%, 150%…)
        u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    return u


def _style(u, hwnd) -> int:
    fn = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
    fn.argtypes, fn.restype = [ctypes.c_void_p, ctypes.c_int], ctypes.c_ssize_t
    return fn(hwnd, GWL_STYLE)


def _set_style(u, hwnd, value) -> None:
    fn = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    fn(hwnd, GWL_STYLE, value)
    u.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED | SWP_NOACTIVATE)


def _rect(u, hwnd) -> tuple:
    from ctypes import wintypes
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def _cursor(u) -> tuple:
    from ctypes import wintypes
    p = wintypes.POINT()
    u.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _work_area(u, hwnd) -> tuple:
    """Рабочая область монитора, где окно (без панели задач)."""
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    u.MonitorFromWindow.restype = ctypes.c_void_p
    u.MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    mon = u.MonitorFromWindow(hwnd, 2)            # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    u.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi))
    r = mi.rcWork
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def _move(u, hwnd, x, y, w, h) -> None:
    u.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h), SWP_NOZORDER | SWP_NOACTIVATE)


def strip(hwnd) -> bool:
    """Убрать системную полосу и рамку. True — если что-то поменяли."""
    u = _u()
    if u.IsZoomed(hwnd):                          # Edge сам развернул окно — разворачиваем «по-нашему»
        u.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.2)
        maximize(hwnd)
    style = _style(u, hwnd)
    if not style & (WS_CAPTION | WS_THICKFRAME):
        return False
    _set_style(u, hwnd, style & ~(WS_CAPTION | WS_THICKFRAME))
    return True


def is_max(hwnd) -> bool:
    return bool(hwnd) and hwnd in STATE["max"]


def maximize(hwnd) -> None:
    u = _u()
    if hwnd in STATE["max"]:
        return
    STATE["max"][hwnd] = _rect(u, hwnd)
    _move(u, hwnd, *_work_area(u, hwnd))


def restore(hwnd) -> None:
    u = _u()
    old = STATE["max"].pop(hwnd, None)
    if old:
        _move(u, hwnd, *old)


def toggle_max(hwnd) -> None:
    restore(hwnd) if is_max(hwnd) else maximize(hwnd)


def minimize(hwnd) -> None:
    _u().ShowWindow(hwnd, SW_MINIMIZE)


def _follow(hwnd, edge: str) -> None:
    """Пока зажата левая кнопка мыши — двигаем (edge="") или тянем за край окно вслед за курсором."""
    u = _u()
    try:
        cx, cy = _cursor(u)
        x, y, w, h = _rect(u, hwnd)
        if not edge and is_max(hwnd):             # тянем развёрнутое окно — сначала вернуть обычный размер под курсором
            ox, oy, ow, oh = STATE["max"].pop(hwnd)
            rel = (cx - x) / max(1, w)
            x, y, w, h = int(cx - ow * rel), y, ow, oh
            _move(u, hwnd, x, y, w, h)
        elif edge:
            STATE["max"].pop(hwnd, None)
        start = time.time()
        while u.GetAsyncKeyState(VK_LBUTTON) & 0x8000 and time.time() - start < 120:
            px, py = _cursor(u)
            dx, dy = px - cx, py - cy
            if not edge:
                _move(u, hwnd, x + dx, y + dy, w, h)
            else:
                nx, ny, nw, nh = x, y, w, h
                if "e" in edge:
                    nw = max(MIN_W, w + dx)
                if "s" in edge:
                    nh = max(MIN_H, h + dy)
                if "w" in edge:
                    nw = max(MIN_W, w - dx)
                    nx = x + w - nw
                if "n" in edge:
                    nh = max(MIN_H, h - dy)
                    ny = y + h - nh
                _move(u, hwnd, nx, ny, nw, nh)
            time.sleep(0.01)
        if not edge:                              # бросили у верхнего края экрана — развернуть
            px, py = _cursor(u)
            wx, wy, _, _ = _work_area(u, hwnd)
            if py <= wy + 2:
                maximize(hwnd)
    except Exception:
        log.exception("Перетаскивание окна")
    finally:
        STATE["busy"] = False


def drag(hwnd, edge: str = "") -> None:
    if not hwnd or STATE["busy"]:
        return
    edge = "".join(c for c in (edge or "") if c in "nsew")
    STATE["busy"] = True
    threading.Thread(target=_follow, args=(hwnd, edge), daemon=True, name="troy-drag").start()


def keep(find_window) -> None:
    """Следим за окном Троя: как только появилось — убираем системную полосу (и после каждого открытия)."""
    def loop():
        while True:
            try:
                if STATE["enabled"]:
                    hwnd = find_window()
                    if hwnd:
                        strip(hwnd)
                    for h in [h for h in STATE["max"] if not _u().IsWindow(h)]:
                        STATE["max"].pop(h, None)
            except Exception:
                log.debug("window_frame", exc_info=True)
            time.sleep(0.7)

    threading.Thread(target=loop, daemon=True, name="troy-frame").start()
