"""Окно Троя на WebView2 — своё окно Windows вместо окна Edge.

Раньше окно Троя было окном Edge в режиме приложения: белую полосу Windows у него можно было только
снимать снаружи, а Edge возвращал её обратно. Теперь окно — наше собственное (Windows Forms),
а страница Троя показывается внутри него движком WebView2 (тот же Edge, но встроенный).
Рамкой управляем мы: полосы нет на Windows 10 и 11, кнопки окна — в шапке Троя.

Сайты открываются настоящими вкладками внутри этого же окна — ещё одним WebView2 под шапкой.
Режимы: «напрямую» и «через сервер» (у вкладки свой профиль с прокси к туннелю Троя).
Вход на сайтах запоминается: у каждого режима своя папка профиля.

Нужны: Windows, библиотека pythonnet и файлы WebView2 из пакета pywebview, сам WebView2 Runtime
(он есть почти везде, ставится вместе с Edge). Если чего-то нет — available() == False,
и программа открывает окно по-старому, через Edge.
"""
from __future__ import annotations

import itertools
import logging
import os
import platform
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("troy.shell")

S = {"form": None, "ui": None, "url": "", "ready": threading.Event(), "failed": False, "error": "",
     "top": 0, "active": None, "on_fail": None, "visible": False, "bg": (11, 15, 23)}
TABS: dict = {}                           # id -> {"view", "url", "host", "mode", "title"}
_ids = itertools.count(1)
_lock = threading.Lock()
_NET: dict = {}                           # загруженные классы .NET


def _safe(fn):
    """Обработчик событий окна: ошибка в Python не должна выскочить окном ошибки .NET."""
    def wrap(*a):
        try:
            return fn(*a)
        except Exception:
            log.exception("Окно Троя: %s", getattr(fn, "__name__", "событие"))
    wrap.__name__ = getattr(fn, "__name__", "wrap")
    return wrap


def _lib_dir() -> Path | None:
    """Папка с файлами WebView2 (из пакета pywebview; в собранной программе — webview/lib рядом с exe)."""
    cands = []
    if getattr(sys, "frozen", False):
        cands.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "webview" / "lib")
        cands.append(Path(sys.executable).parent / "webview" / "lib")
    try:
        import importlib.util
        spec = importlib.util.find_spec("webview")
        if spec and spec.origin:
            cands.append(Path(spec.origin).parent / "lib")
    except Exception:
        pass
    for c in cands:
        if c.is_dir() and any(c.rglob("Microsoft.Web.WebView2.Core.dll")):
            return c
    return None


def _pick(lib: Path, name: str) -> Path | None:
    """Нужная сборка .NET Framework (не .NET Core) или загрузчик под процессор этого компьютера."""
    found = list(lib.rglob(name))
    if not found:
        return None
    if name == "WebView2Loader.dll":
        arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower(), "x86")
        for f in found:
            if f"win-{arch}" in str(f).lower() or f"\\{arch}\\" in str(f).lower():
                return f
    for f in found:                       # для .NET Framework — сборки без «netcoreapp»
        if "netcoreapp" not in str(f).lower() and "net5" not in str(f).lower():
            return f
    return found[0]


def _load() -> bool:
    if _NET:
        return True
    lib = _lib_dir()
    if not lib:
        S["error"] = "нет файлов WebView2 (пакет pywebview)"
        return False
    import clr                                                   # pythonnet
    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")
    clr.AddReference(str(_pick(lib, "Microsoft.Web.WebView2.Core.dll")))
    clr.AddReference(str(_pick(lib, "Microsoft.Web.WebView2.WinForms.dll")))
    import System
    from System import Action, Uri
    from System.Drawing import Color, Icon, Rectangle, Size
    from System.Windows.Forms import (Application, AnchorStyles, DockStyle, Form, FormBorderStyle,
                                      FormStartPosition, UnhandledExceptionMode)
    from Microsoft.Web.WebView2.Core import (CoreWebView2Environment, CoreWebView2PermissionState)
    from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2
    loader = _pick(lib, "WebView2Loader.dll")
    if loader:
        try:
            CoreWebView2Environment.SetLoaderDllFolderPath(str(loader.parent))
        except Exception:
            os.environ["PATH"] = str(loader.parent) + os.pathsep + os.environ.get("PATH", "")
    _NET.update(System=System, Action=Action, Uri=Uri, Color=Color, Icon=Icon, Rectangle=Rectangle, Size=Size,
                Application=Application, AnchorStyles=AnchorStyles, DockStyle=DockStyle, Form=Form,
                FormBorderStyle=FormBorderStyle, FormStartPosition=FormStartPosition, UEM=UnhandledExceptionMode,
                Env=CoreWebView2Environment, Perm=CoreWebView2PermissionState,
                Props=CoreWebView2CreationProperties, WebView2=WebView2)
    return True


def available() -> bool:
    """Можно ли открыть окно на WebView2 на этом компьютере."""
    if sys.platform != "win32" or S["failed"] or os.environ.get("TROY_EDGE_WINDOW"):
        return False
    try:
        if not _load():
            return False
        _NET["Env"].GetAvailableBrowserVersionString()           # бросит ошибку, если WebView2 Runtime нет
        return True
    except Exception as e:
        S["error"] = str(e)
        log.warning("Окно на WebView2 недоступно: %s", e)
        return False


def started() -> bool:
    return S["form"] is not None and not S["failed"]


def _ui(fn, wait=True):
    """Выполнить в потоке окна (Windows Forms разрешает трогать окно только оттуда)."""
    form = S["form"]
    if form is None:
        return None
    box = {}

    def run():
        try:
            box["r"] = fn()
        except Exception as e:
            box["e"] = e
            log.exception("Окно Троя")
    try:
        if form.InvokeRequired:
            (form.Invoke if wait else form.BeginInvoke)(_NET["Action"](run))
        else:
            run()
    except Exception as e:
        log.warning("Окно Троя: %s", e)
        return None
    return box.get("r")


def _dpi_scale() -> float:
    try:
        import ctypes
        return (ctypes.windll.user32.GetDpiForSystem() or 96) / 96
    except Exception:
        return 1.0


def _props(folder: Path, args: str):
    p = _NET["Props"]()
    p.UserDataFolder = str(folder)
    if args:
        p.AdditionalBrowserArguments = args
    return p


def start(url: str, data_dir: Path, title: str, geometry: dict, visible: bool = True) -> None:
    """Создать окно (в отдельном потоке Windows) и показать страницу Троя."""
    if S["form"] is not None:
        show(url)
        return
    S["url"], S["data"] = url, Path(data_dir)
    from System.Threading import ApartmentState, Thread, ThreadStart

    def run():
        try:
            N = _NET
            try:                                                 # ошибки окна — в журнал, а не окном на экран
                N["Application"].SetUnhandledExceptionMode(N["UEM"].CatchException)
                N["Application"].ThreadException += _safe(lambda _s, e: log.error("Окно Троя: %s", e.Exception))
            except Exception:
                pass
            N["Application"].EnableVisualStyles()
            form = N["Form"]()
            form.Text = title                                    # по заголовку программа находит своё окно
            form.FormBorderStyle = getattr(N["FormBorderStyle"], "None")
            form.BackColor = N["Color"].FromArgb(*S["bg"])
            form.StartPosition = N["FormStartPosition"].Manual
            form.MinimumSize = N["Size"](420, 320)
            try:
                form.Icon = N["Icon"].ExtractAssociatedIcon(sys.executable)
            except Exception:
                pass
            k = _dpi_scale()
            g = geometry or {}
            w, h = int((g.get("w") or 1100) * k), int((g.get("h") or 780) * k)
            x, y = g.get("x"), g.get("y")
            if x is None or y is None or not (-200 < int(x) < 10000 and -200 < int(y) < 10000):
                x, y = 120, 80
            form.Bounds = N["Rectangle"](int(int(x) * k), int(int(y) * k), max(w, 420), max(h, 320))

            ui = N["WebView2"]()
            ui.Dock = N["DockStyle"].Fill
            ui.DefaultBackgroundColor = N["Color"].FromArgb(*S["bg"])
            ui.CreationProperties = _props(S["data"] / "webview",
                                           "--autoplay-policy=no-user-gesture-required --disable-features=Translate")
            ui.CoreWebView2InitializationCompleted += _safe(_ui_ready)
            form.Controls.Add(ui)
            form.HandleCreated += _safe(lambda *_: S.update(hwnd=int(form.Handle.ToInt64())))
            form.FormClosing += _safe(_on_closing)
            form.Resize += _safe(lambda *_: _layout())
            form.Shown += _safe(lambda *_: _taskbar_style())
            S["form"], S["ui"] = form, ui
            ui.Source = N["Uri"](S["url"])
            S["visible"] = visible
            if visible:
                N["Application"].Run(form)
            else:
                form.Opacity = 0                                 # «--tray»: окно есть, но не видно
                N["Application"].Run(form)
        except Exception as e:
            _fail(f"окно не создалось: {e}")
        finally:
            S["ready"].set()

    t = Thread(ThreadStart(run))
    t.SetApartmentState(ApartmentState.STA)
    t.IsBackground = True
    t.Start()


def _taskbar_style() -> None:
    """Окну без рамки — сворачивание щелчком по значку на панели задач (без полосы заголовка)."""
    try:
        import ctypes
        hwnd = hwnd_of()
        u = ctypes.windll.user32
        fn = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
        fn.restype = ctypes.c_ssize_t
        fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
        style = fn(hwnd, -16)
        setf = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
        setf.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        setf(hwnd, -16, style | 0x00020000 | 0x00080000)        # WS_MINIMIZEBOX | WS_SYSMENU
    except Exception:
        log.debug("taskbar style", exc_info=True)
    if not S["visible"]:
        _ui(lambda: S["form"].Hide(), wait=False)


def _fail(why: str) -> None:
    log.warning("Окно на WebView2: %s — открываю окно по-старому, через Edge", why)
    S["failed"], S["error"] = True, why
    form, S["form"] = S["form"], None
    if form is not None:
        try:
            form.Dispose()
        except Exception:
            pass
    cb = S.get("on_fail")
    if cb:
        threading.Thread(target=cb, daemon=True).start()


def _ui_ready(sender, args) -> None:
    if not args.IsSuccess:
        err = getattr(args, "InitializationException", None)
        _fail(f"WebView2 не запустился: {err}")
        return
    core = S["ui"].CoreWebView2
    st = core.Settings
    st.IsStatusBarEnabled = False
    st.IsZoomControlEnabled = True
    try:
        st.IsGeneralAutofillEnabled = False
    except Exception:
        pass

    def perm(_s, e):                                         # своей странице — микрофон и буфер обмена без вопросов
        e.State = _NET["Perm"].Allow
    core.PermissionRequested += _safe(perm)

    def new_window(_s, e):                                   # ссылки «в новом окне» из чата — в обычном браузере
        e.Handled = True
        import webbrowser
        threading.Thread(target=webbrowser.open, args=(str(e.Uri),), daemon=True).start()
    core.NewWindowRequested += _safe(new_window)
    log.info("Окно на WebView2 готово (WebView2 %s)", core.Environment.BrowserVersionString)


def _on_closing(_sender, e) -> None:
    """Крестик / Alt+F4 — прячем окно в трей (Трой продолжает работать)."""
    if S.get("exiting"):
        return
    e.Cancel = True
    S["visible"] = False
    S["form"].Hide()


def hwnd_of():
    """Окно Троя для Windows (запоминаем, когда окно создано: трогать Handle из других потоков нельзя)."""
    return S.get("hwnd") if S["form"] is not None else None


def visible() -> bool:
    return started() and S["visible"]


def show(url: str = "") -> None:
    """Показать окно (и открыть адрес, если он другой — например, «?diag=1»)."""
    def run():
        form, ui = S["form"], S["ui"]
        if url and url != S["url"]:
            S["url"] = url
            ui.Source = _NET["Uri"](url)
        form.Opacity = 1
        if not form.Visible:
            form.Show()
        S["visible"] = True
        import ctypes
        h = hwnd_of()
        if h:
            if ctypes.windll.user32.IsIconic(h):
                ctypes.windll.user32.ShowWindow(h, 9)            # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(h)
        form.Activate()
    _ui(run)


def hide() -> None:
    def run():
        S["visible"] = False
        S["form"].Hide()
    _ui(run)


def exit_app() -> None:
    def run():
        S["exiting"] = True
        for t in list(TABS.values()):
            try:
                t["view"].Dispose()
            except Exception:
                pass
        S["form"].Close()
        _NET["Application"].ExitThread()
    _ui(run)


# ── вкладки сайтов внутри окна ──
def _layout() -> None:
    """Вкладка сайта занимает всё окно под шапкой Троя с полоской вкладок."""
    form = S["form"]
    if form is None:
        return
    top = max(0, int(S["top"]))
    cw, ch = form.ClientSize.Width, form.ClientSize.Height
    for t in TABS.values():
        t["view"].Bounds = _NET["Rectangle"](0, top, cw, max(50, ch - top))


def set_top(top: float) -> None:
    S["top"] = float(top or 0)
    _ui(_layout, wait=False)


def open_tab(url: str, host: str, mode: str, proxy_port: int = 0) -> tuple:
    """Открыть сайт вкладкой в окне Троя. (получилось, текст)"""
    if not started():
        return False, "окно Троя не открыто"
    folder = S["data"] / ("web-server" if mode == "server" else "web-direct")
    args = (f"--proxy-server=http://127.0.0.1:{proxy_port} --proxy-bypass-list=<-loopback>"
            if mode == "server" else "")
    if not S["top"]:                                         # пока страница не сказала, где кончается шапка
        S["top"] = int(100 * _dpi_scale())
    with _lock:
        tid = next(_ids)
        TABS[tid] = {"view": None, "url": url, "host": host, "mode": mode, "title": host}

    def run():
        N = _NET
        v = N["WebView2"]()
        v.DefaultBackgroundColor = N["Color"].White
        v.CreationProperties = _props(folder, args)
        A = N["AnchorStyles"]
        v.Anchor = A.Top | A.Bottom | A.Left | A.Right
        v.Visible = False

        def ready(_s, a):
            if not a.IsSuccess:
                log.warning("Вкладка %s: WebView2 не запустился: %s", host, getattr(a, "InitializationException", ""))
                return
            core = v.CoreWebView2
            core.Settings.IsStatusBarEnabled = True

            def title(*_):
                t = TABS.get(tid)
                if t:
                    t["title"] = (str(core.DocumentTitle) or host)[:60]

            def new_win(_s2, e):                              # «открыть в новом окне» — в этой же вкладке
                e.Handled = True
                core.Navigate(e.Uri)
            core.DocumentTitleChanged += _safe(title)
            core.NewWindowRequested += _safe(new_win)
            core.WindowCloseRequested += _safe(lambda *_: threading.Thread(target=close, args=(tid,), daemon=True).start())
        v.CoreWebView2InitializationCompleted += _safe(ready)
        S["form"].Controls.Add(v)
        TABS[tid]["view"] = v
        _layout()
        v.Source = N["Uri"](url)
    _ui(run)
    if TABS.get(tid, {}).get("view") is None:
        TABS.pop(tid, None)
        return False, "не получилось открыть вкладку"
    show()
    show_tab(tid)
    return True, f"открыл {host} {'через сервер' if mode == 'server' else 'напрямую'} — вкладкой в окне Троя"


def show_tab(tid) -> None:
    """Показать вкладку сайта (tid) или чат (None)."""
    def run():
        S["active"] = tid if tid in TABS else None
        for i, t in TABS.items():
            if t["view"] is not None:
                t["view"].Visible = i == S["active"]
        cur = TABS.get(S["active"])
        if cur and cur["view"] is not None:
            _layout()
            cur["view"].BringToFront()
            cur["view"].Focus()
        elif S["ui"] is not None:
            S["ui"].Focus()
    _ui(run)


def close(tid) -> None:
    def run():
        t = TABS.pop(tid, None)
        if S["active"] == tid:
            S["active"] = None
        if t and t["view"] is not None:
            S["form"].Controls.Remove(t["view"])
            t["view"].Dispose()
        if S["ui"] is not None and S["active"] is None:
            S["ui"].Focus()
    _ui(run)


def listing() -> list:
    with _lock:
        return [{"id": i, "title": t["title"], "host": t["host"], "mode": t["mode"], "active": i == S["active"]}
                for i, t in TABS.items()]


def wait_ready(timeout: float = 15) -> bool:
    """Дождаться, пока окно создастся (или не создастся)."""
    end = time.time() + timeout
    while time.time() < end:
        if S["failed"]:
            return False
        if S["form"] is not None and hwnd_of():
            return True
        time.sleep(0.1)
    return started()
