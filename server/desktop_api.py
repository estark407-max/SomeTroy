"""HTTPS API для программы Трой на компьютере.

POST /api/login                 — {"password", "device"} → {"session": "..."}  (пароль задаётся в Telegram: /password)
POST /api/logout                — завершить свою сессию
GET  /api/ping                  — проверка связи
GET  /api/history               — последние сообщения (общие с Telegram)
GET  /api/events?after=N        — сработавшие напоминания
GET  /api/pc/poll               — команды для этого компьютера (заодно «я в сети»)
POST /api/pc/result             — {"id", "ok", "text"} — чем закончилась команда
POST /api/pc/choose             — {"id", "device"} — выбор компьютера кнопкой
POST /api/message               — {"text": "...", "files": [{"name", "mime", "data"(base64)}]} → {"reply": "..."}

Всё, кроме входа, — только с заголовком Authorization: Bearer <session>.
"""
import json
import logging
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("assistant.desktop")
MAX_BODY = 25 * 1024 * 1024


def make_handler(auth, pc, on_message, get_history, get_events, bot_name: str, conns=None):
    """auth: объект с методами login(password, device, ip) -> (session|None, error), check(session) -> bool,
    logout(session)."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "Troy"
        timeout = 300            # медленный или зависший клиент не держит сервер вечно

        def log_message(self, fmt, *args):   # без лишнего шума в журнале
            pass

        def reply(self, code: int, data: dict) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def session(self) -> str:
            got = self.headers.get("Authorization", "")
            return got[7:] if got.startswith("Bearer ") else ""

        def authorized(self) -> bool:
            if auth.check(self.session()):
                return True
            self.reply(401, {"error": "login"})
            return False

        def body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.reply(413, {"error": "файл слишком большой (до 20 МБ)"})
                return None
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self.reply(400, {"error": "неверный запрос"})
                return None

        def do_GET(self):
            if not self.authorized():
                return
            url = urlparse(self.path)
            if url.path == "/api/ping":
                self.reply(200, {"ok": True, "name": bot_name})
            elif url.path == "/api/history":
                after = int((parse_qs(url.query).get("after") or ["0"])[0] or 0)
                self.reply(200, {"messages": get_history(after)})
            elif url.path == "/api/files/get" and hasattr(pc, "file_download"):    # получатель забирает файл
                got = pc.file_download(auth.device(self.session()) or "", (parse_qs(url.query).get("id") or [""])[0])
                if not got:
                    self.reply(404, {"error": "файл не найден или уже доставлен"})
                    return
                data, _name = got
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif url.path == "/api/pc/poll":         # компьютер спрашивает команды для себя
                ver = (parse_qs(url.query).get("v") or [""])[0][:20]
                self.reply(200, {"commands": pc.poll(auth.device(self.session()) or "компьютер", ver)})
            elif url.path == "/api/connections" and conns is not None:     # список подключений (без секретов)
                self.reply(200, {"connections": conns.listing()})
            elif url.path == "/api/events":
                after = int((parse_qs(url.query).get("after") or ["0"])[0] or 0)
                self.reply(200, {"events": get_events(after)})
            else:
                self.reply(404, {"error": "нет такого адреса"})

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/login":
                data = self.body()
                if data is None:
                    return
                session, error = auth.login(str(data.get("password") or ""), str(data.get("device") or "компьютер")[:60],
                                            self.client_address[0])
                if session:
                    self.reply(200, {"session": session, "name": bot_name})
                else:
                    self.reply(403, {"error": error})
                return
            if not self.authorized():
                return
            if path == "/api/logout":
                auth.logout(self.session())
                self.reply(200, {"ok": True})
                return
            if path == "/api/files/upload" and hasattr(pc, "file_upload"):        # источник отдаёт файл
                q = parse_qs(urlparse(self.path).query)
                length = int(self.headers.get("Content-Length") or 0)
                if length > pc.FILE_MAX:
                    self.reply(413, {"error": f"файл больше {pc.FILE_MAX // 1024 // 1024} МБ"})
                    return
                data = self.rfile.read(length)
                ok, err = pc.file_upload(auth.device(self.session()) or "", int((q.get("cmd") or ["0"])[0] or 0),
                                         (q.get("name") or ["файл"])[0], data)
                self.reply(200 if ok else 400, {"ok": ok, "error": err})
                return
            if path == "/api/pc/result":
                data = self.body()
                if data is None:
                    return
                pc.result(auth.device(self.session()), int(data.get("id") or 0), bool(data.get("ok")),
                          str(data.get("text") or ""), bool(data.get("late")))
                self.reply(200, {"ok": True})
                return
            if path in ("/api/connections", "/api/connections/remove") and conns is not None:
                data = self.body()
                if data is None:
                    return
                cid = str(data.get("id") or "")
                if path.endswith("/remove"):
                    conns.disconnect(cid)
                    self.reply(200, {"ok": True, "connections": conns.listing()})
                    return
                err = conns.connect(cid, data.get("values") or {})
                self.reply(200 if not err else 400, {"ok": not err, "error": err or "", "note": getattr(conns, "note", ""),
                                                     "connections": conns.listing()})
                return
            if path == "/api/pc/choose":
                data = self.body()
                if data is None:
                    return
                self.reply(200, {"reply": pc.choose(int(data.get("id") or 0), str(data.get("device") or ""))})
                return
            if path != "/api/message":
                self.reply(404, {"error": "нет такого адреса"})
                return
            data = self.body()
            if data is None:
                return
            try:
                files = data.get("files") or ([data["file"]] if data.get("file") else [])
                answer = on_message(str(data.get("text") or ""), files)
                self.reply(200, answer if isinstance(answer, dict) else {"reply": answer})
            except Exception as e:
                log.exception("Ошибка обработки сообщения с компьютера")
                self.reply(500, {"error": str(e)})

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):   # обрывы и чужие TLS-рукопожатия — без простыней в журнале
        log.debug("Соединение с %s прервано", client_address[0])


def start(port: int, auth, pc, cert, key, on_message, get_history, get_events, bot_name: str, host: str = "0.0.0.0",
          conns=None):
    """cert=None — без шифрования; так можно только на 127.0.0.1 (локальный режим: связь не выходит наружу)."""
    server = _Server((host, port), make_handler(auth, pc, on_message, get_history, get_events, bot_name, conns))
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        # рукопожатие TLS — уже в потоке запроса, чтобы зависший клиент не блокировал приём остальных
        server.socket = ctx.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    elif host != "127.0.0.1":
        raise ValueError("без сертификата — только 127.0.0.1")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Доступ для программы на компьютере: порт %d", port)
    return server
