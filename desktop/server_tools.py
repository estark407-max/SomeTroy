"""Сервер и копия памяти: сменить сервер, сохранить копию на компьютер, загрузить копию в Троя.

Смена сервера: программа берёт сертификат нового сервера, проверяет, что там действительно Трой и что пароль
подходит, и только потом запоминает новый адрес. Старые настройки остаются рядом (…prev) — на случай возврата.
Копия памяти — зашифрованный файл (шифрует сервер паролем программы); программа лишь кладёт его
в «Загрузки\\Трой» и отправляет обратно, содержимое она не видит.
"""
import hashlib
import json
import logging
import os
import re
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import quote

import requests

log = logging.getLogger("troy.server")


def _error(body: bytes, default: str) -> str:
    try:
        return json.loads(body).get("error") or default
    except (ValueError, AttributeError):
        return default


def backup_save(remote, password: str, folder: Path) -> dict:
    """Попросить у Троя копию памяти и положить её в папку загрузок."""
    if not password:
        return {"error": "Введи пароль программы — им шифруется копия."}
    code, body = remote._send("POST", "/api/backup/export", b"", headers={"X-Troy-Password": quote(password)})
    if code == 404:
        return {"error": "Трой на сервере старой версии и не умеет делать копии — сначала обнови его."}
    if code != 200:
        return {"error": _error(body, f"не получилось (код {code})")}
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"Troy-копия-{time.strftime('%Y-%m-%d-%H%M')}.troybak"
    n = 1
    while target.exists():
        target = folder / f"Troy-копия-{time.strftime('%Y-%m-%d-%H%M')} ({n}).troybak"
        n += 1
    target.write_bytes(body)
    log.info("Копия памяти сохранена: %s (%d байт)", target.name, len(body))
    return {"path": str(target), "size": len(body)}


def backup_load(remote, password: str, blob: bytes) -> dict:
    """Отправить файл копии Трою: он проверит пароль и заменит свою память содержимым копии."""
    if not blob.startswith(b"TROYBAK1"):
        return {"error": "Это не файл копии Троя (нужен файл .troybak)."}
    if not password:
        return {"error": "Введи пароль программы того Троя, с которого сделана копия."}
    code, body = remote._send("POST", "/api/backup/import", blob, headers={"X-Troy-Password": quote(password)})
    if code == 404:
        return {"error": "Трой на сервере старой версии и не умеет загружать копии — сначала обнови его."}
    if code != 200:
        return {"error": _error(body, f"не получилось (код {code})")}
    try:
        return {"ok": True, "text": json.loads(body).get("text") or "Копия загружена."}
    except ValueError:
        return {"ok": True, "text": "Копия загружена."}


def parse_address(address: str, default_port: int) -> tuple:
    m = re.fullmatch(r"\s*(?:https?://)?([A-Za-z0-9.-]+)(?::(\d{2,5}))?/?\s*", address or "")
    if not m:
        raise ValueError("Адрес сервера выглядит так: 203.0.113.10 или 203.0.113.10:8443")
    return m.group(1), int(m.group(2) or default_port)


def fetch_cert(host: str, port: int) -> tuple:
    """Сертификат сервера (PEM) и его отпечаток SHA-256."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE              # сертификат у Троя свой; дальше работаем только с ним
    with socket.create_connection((host, port), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(True)
    fp = hashlib.sha256(der).hexdigest().upper()
    return ssl.DER_cert_to_PEM_cert(der), ":".join(fp[i:i + 2] for i in range(0, 16, 2))


def switch_server(remote, address: str, password: str, data_dir: Path, mode_key: bool) -> dict:
    """Перейти на другой сервер, где Трой уже установлен. Ничего не меняем, пока вход туда не удался."""
    try:
        cur = re.search(r":(\d+)$", remote.base)
        host, port = parse_address(address, int(cur.group(1)) if cur and remote.base.startswith("https") else 443)
    except ValueError as e:
        return {"error": str(e)}
    if not password:
        return {"error": "Введи пароль программы Троя на том сервере."}
    server = f"https://{host}:{port}"
    if server == remote.base:
        return {"error": "Программа уже подключена к этому серверу."}
    try:
        pem, fingerprint = fetch_cert(host, port)
    except (OSError, ssl.SSLError) as e:
        log.warning("Смена сервера: %s:%s не ответил: %s", host, port, e)
        return {"error": f"Сервер {host}:{port} не отвечает. Проверь адрес и порт, и что Трой там установлен и запущен."}
    data_dir.mkdir(parents=True, exist_ok=True)
    new_cert = data_dir / "troy-cert.new.pem"
    new_cert.write_text(pem, encoding="utf-8")
    device = os.environ.get("COMPUTERNAME") or "компьютер"
    try:
        r = requests.post(server + "/api/login", json={"password": password, "device": device}, timeout=25,
                          verify=str(new_cert))
        session = r.json().get("session", "") if r.status_code == 200 else ""
        err = "" if session else _error(r.content, "по этому адресу отвечает не Трой")
    except requests.exceptions.SSLError:
        err = ("сертификат сервера выписан на другой адрес. Обнови Троя на том сервере "
               "(«Обновить и починить сервер» или deploy.bat) и попробуй снова")
    except (requests.RequestException, ValueError):
        err = "по этому адресу отвечает не Трой (или сервер недоступен)"
    if err:
        try:
            new_cert.unlink()
        except OSError:
            pass
        return {"error": err[:1].upper() + err[1:] + "."}
    try:                                         # прежний сервер: выходим, настройки оставляем рядом
        remote.logout()
    except Exception:
        pass
    cert, cfg = data_dir / "troy-cert.pem", data_dir / "config.json"
    for f in (cert, cfg):
        if f.exists():
            f.replace(f.with_name(f.name + ".prev"))
    new_cert.replace(cert)
    cfg.write_text(json.dumps({"mode": "vps", "server": server} if mode_key else {"server": server}), encoding="utf-8")
    remote.base, remote.verify, remote.s = server, str(cert), requests.Session()
    remote._save(session)
    log.info("Сервер сменён на %s (сертификат %s)", server, fingerprint)
    return {"ok": True, "server": server, "fingerprint": fingerprint}
