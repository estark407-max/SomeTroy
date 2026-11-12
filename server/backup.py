"""Резервная копия памяти Троя: переписка, напоминания, подключения, места, идеи — одним файлом под паролем.

Файл шифруется паролем программы (тем, которым входят в Троя). Без пароля его не прочитать,
а новый Трой примет копию, только если назвать пароль, который был у прежнего.

Шифрование — только средствами Python (лишних библиотек серверу не нужно):
  ключ    — PBKDF2-HMAC-SHA256 из пароля и случайной «соли», 600 000 повторов;
  шифр    — поток из BLAKE2b с ключом в режиме счётчика (данные складываются с ним по XOR);
  подпись — HMAC-SHA256 от всего файла: неверный пароль или испорченный файл обнаруживаются до чтения.
Формат: «TROYBAK1» + соль (16) + метка (16) + зашифрованные данные + подпись (32).
Внутри — сжатый JSON.
"""
import hashlib
import hmac
import json
import os
import re
import time
import zlib
from contextlib import closing
from pathlib import Path
from typing import Callable

MAGIC = b"TROYBAK1"
ITER = 600_000
MAX_FILE = 64 * 1024 * 1024
TABLES = {                                   # что переносим: таблица → столбцы
    "history": ["chat_id", "role", "text", "ts"],
    "message_log": ["chat_id", "role", "text", "ts"],
    "reminders": ["chat_id", "due", "text", "repeat", "active", "anchor"],
}
SKIP_KV = {"app_password"}                   # пароль программы у нового Троя свой — его не трогаем


class BackupError(RuntimeError):
    pass


def _keys(password: str, salt: bytes) -> tuple:
    k = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITER, dklen=64)
    return k[:32], k[32:]


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hashlib.blake2b(nonce + i.to_bytes(8, "big"), key=key, digest_size=64).digest()
        i += 1
    return bytes(out[:n])


def _xor(data: bytes, stream: bytes) -> bytes:
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(len(data), "big") if data else b""


def pack(data: dict, password: str) -> bytes:
    if not password:
        raise BackupError("нужен пароль программы")
    plain = zlib.compress(json.dumps(data, ensure_ascii=False).encode("utf-8"), 6)
    salt, nonce = os.urandom(16), os.urandom(16)
    enc_key, mac_key = _keys(password, salt)
    body = MAGIC + salt + nonce + _xor(plain, _stream(enc_key, nonce, len(plain)))
    return body + hmac.new(mac_key, body, hashlib.sha256).digest()


def unpack(blob: bytes, password: str) -> dict:
    if len(blob) < len(MAGIC) + 64 or not blob.startswith(MAGIC):
        raise BackupError("это не файл копии Троя")
    body, tag = blob[:-32], blob[-32:]
    salt, nonce = body[8:24], body[24:40]
    enc_key, mac_key = _keys(password or "", salt)
    if not hmac.compare_digest(hmac.new(mac_key, body, hashlib.sha256).digest(), tag):
        raise BackupError("пароль не подошёл (нужен пароль программы того Троя, с которого сделана копия) "
                          "или файл повреждён")
    try:
        cipher = body[40:]
        return json.loads(zlib.decompress(_xor(cipher, _stream(enc_key, nonce, len(cipher)))).decode("utf-8"))
    except (ValueError, zlib.error):
        raise BackupError("файл копии повреждён")


def _columns(con, table: str) -> list:
    return [c[1] for c in con.execute(f"PRAGMA table_info({table})")]


def make(db: Callable, owner: str, ideas_dir: Path, ideas_in_github: bool, bot_name: str, version: str = "") -> dict:
    """Собрать всё, что Трой помнит."""
    data = {"v": 1, "made": int(time.time()), "bot": bot_name, "version": version, "owner": str(owner or ""),
            "tables": {}, "kv": {}, "ideas": {}, "ideas_in_github": bool(ideas_in_github)}
    with closing(db()) as con:
        for table, cols in TABLES.items():
            have = [c for c in cols if c in _columns(con, table)]
            rows = con.execute(f"SELECT {', '.join(have)} FROM {table} ORDER BY id").fetchall()
            data["tables"][table] = {"cols": have, "rows": [list(r) for r in rows]}
        for row in con.execute("SELECT key, value FROM kv"):
            if row[0] not in SKIP_KV:
                data["kv"][row[0]] = row[1]
    if not ideas_in_github and ideas_dir.exists():           # идеи в GitHub и так лежат в репозитории
        for f in sorted(ideas_dir.rglob("*.md")):
            data["ideas"][f.relative_to(ideas_dir).as_posix()] = f.read_text(encoding="utf-8")
    return data


def summary(data: dict) -> str:
    t = data.get("tables") or {}

    def n(name: str) -> int:
        return len((t.get(name) or {}).get("rows") or [])
    conns = 0
    try:
        conns = len(json.loads((data.get("kv") or {}).get("connections") or "{}"))
    except ValueError:
        pass
    made = time.strftime("%d.%m.%Y %H:%M", time.localtime(data.get("made") or 0))
    parts = [f"сообщений в журнале — {n('message_log')}", f"напоминаний — {n('reminders')}",
             f"подключений — {conns}"]
    if data.get("ideas"):
        parts.append(f"файлов с идеями — {len(data['ideas'])}")
    return f"копия от {made}: " + ", ".join(parts)


def restore(db: Callable, data: dict, owner: str, ideas_dir: Path) -> str:
    """Заменить память этого Троя содержимым копии. Пароль программы и входы на компьютерах не меняются."""
    if data.get("v") != 1 or not isinstance(data.get("tables"), dict):
        raise BackupError("копия сделана другой версией Троя — этот Трой её не понимает")
    old_owner, owner = str(data.get("owner") or ""), str(owner or "")
    with closing(db()) as con, con:                          # одной операцией: либо всё, либо ничего
        for table, cols in TABLES.items():
            part = data["tables"].get(table)
            if not part:
                continue
            have = _columns(con, table)
            use = [c for c in part.get("cols") or [] if c in cols and c in have]
            if "chat_id" not in use:
                continue
            idx = [part["cols"].index(c) for c in use]
            who = use.index("chat_id")
            con.execute(f"DELETE FROM {table}")
            for row in part.get("rows") or []:
                vals = [row[i] for i in idx]
                if owner and str(vals[who]) == old_owner:    # у нового Троя хозяин может называться иначе
                    vals[who] = owner
                con.execute(f"INSERT INTO {table}({', '.join(use)}) VALUES ({', '.join('?' * len(use))})", vals)
        for key, value in (data.get("kv") or {}).items():
            if key not in SKIP_KV and isinstance(value, str):
                con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, value))
    ideas = 0
    for rel, text in (data.get("ideas") or {}).items():
        if not re.fullmatch(r"[\w .-]+(?:/[\w .-]+)*\.md", rel) or ".." in rel or not isinstance(text, str):
            continue                                          # только обычные имена внутри папки идей
        target = ideas_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        ideas += 1
    return "Загружена " + summary(data) + "."
