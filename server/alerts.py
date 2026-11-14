"""Трой пишет сам: важное, не дожидаясь вопроса.

- Битрикс: до срока задачи остался час (и задача не закрыта); новое личное сообщение или упоминание тебя в чате.
- Почта: письмо от важного отправителя (список задаёшь сам: «сообщай о письмах от …»).
Котёл здесь не нужен: его автоматика сама пишет о топливе и авариях.
Каждое событие сообщается один раз; ночью (23–8) Трой молчит и рассказывает утром.
"""
import email
import json
import logging
import re
import time
from contextlib import closing
from datetime import datetime, timedelta
from typing import Callable, Optional

log = logging.getLogger("troy.alerts")
KEY = "alerts"
DEFAULT = {"deadlines": True, "bitrix_messages": True, "mail": True, "senders": [], "seen": {}}
QUIET = (23, 8)


def settings(db: Callable) -> dict:
    with closing(db()) as con:
        row = con.execute("SELECT value FROM kv WHERE key=?", (KEY,)).fetchone()
    try:
        cfg = json.loads(row[0]) if row else {}
    except ValueError:
        cfg = {}
    out = {**DEFAULT, **cfg}
    out["seen"] = dict(out.get("seen") or {})
    return out


def save(db: Callable, cfg: dict) -> None:
    now = time.time()                                       # старые отметки «уже сообщал» — не дольше 30 дней
    cfg["seen"] = {k: v for k, v in (cfg.get("seen") or {}).items() if now - v < 30 * 86400}
    with closing(db()) as con, con:
        con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (KEY, json.dumps(cfg, ensure_ascii=False)))


def describe(cfg: dict, has_bx: bool, has_mail: bool) -> str:
    on = lambda x: "вкл" if x else "выкл"                   # noqa: E731
    lines = ["Пишу сам:"]
    if has_bx:
        lines.append(f"- Битрикс, за час до срока задачи: {on(cfg['deadlines'])}")
        lines.append(f"- Битрикс, личные сообщения и упоминания: {on(cfg['bitrix_messages'])}")
    else:
        lines.append("- Битрикс не подключён")
    if has_mail:
        who = ", ".join(cfg["senders"]) if cfg["senders"] else "список пуст — скажи «сообщай о письмах от …»"
        lines.append(f"- Письма от важных отправителей ({on(cfg['mail'])}): {who}")
    else:
        lines.append("- Почта не подключена (её можно подключить в программе: «Подключения»)")
    lines.append("Ночью (23–8) молчу, накопленное пришлю утром.")
    return "\n".join(lines)


def configure(db: Callable, a: dict, has_bx: bool, has_mail: bool) -> str:
    cfg = settings(db)
    act, kind = a.get("action") or "show", a.get("kind") or ""
    if act in ("on", "off"):
        kinds = ["deadlines", "bitrix_messages", "mail"] if kind in ("", "all") else [kind]
        for k in kinds:
            if k in DEFAULT:
                cfg[k] = act == "on"
    elif act == "add_sender":
        s = str(a.get("sender") or "").strip().lower()
        if not s:
            return "Ошибка: чей адрес или имя отслеживать?"
        if s not in cfg["senders"]:
            cfg["senders"].append(s)
        cfg["mail"] = True
    elif act == "remove_sender":
        s = str(a.get("sender") or "").strip().lower()
        cfg["senders"] = [x for x in cfg["senders"] if s not in x and x not in s]
    save(db, cfg)
    return describe(cfg, has_bx, has_mail)


def quiet(now: datetime) -> bool:
    return now.hour >= QUIET[0] or now.hour < QUIET[1]


# ── Битрикс ──
def bitrix_deadlines(bx, cfg: dict, now: datetime) -> list:
    me = bx.me()["ID"]
    soon = now + timedelta(minutes=65)
    items = bx.list_all("tasks.task.list", {
        "filter": {"RESPONSIBLE_ID": me, "<REAL_STATUS": 5, ">DEADLINE": now.strftime("%Y-%m-%dT%H:%M:%S"),
                   "<=DEADLINE": soon.strftime("%Y-%m-%dT%H:%M:%S")},
        "select": ["ID", "TITLE", "DEADLINE"]}, "tasks", 20)
    out = []
    for t in items:
        key = f"dl:{t['id']}:{t.get('deadline')}"
        if key in cfg["seen"]:
            continue
        cfg["seen"][key] = time.time()
        dl = (t.get("deadline") or "")[11:16]
        out.append(f"⏳ Битрикс: скоро срок задачи #{t['id']} «{t['title']}» (до {dl}).")
    return out


def _clean_bb(text: str) -> str:
    text = re.sub(r"\[USER=\d+\](.*?)\[/USER\]", r"@\1", text or "")
    return re.sub(r"\[/?[A-Z]+[^\]]*\]", "", text).strip()


def bitrix_messages(bx, cfg: dict) -> list:
    me = str(bx.me()["ID"])
    items = (bx.call("im.recent.list", {"SKIP_OPENLINES": "Y"}).get("result") or {}).get("items") or []
    out = []
    for i in items:
        if int(i.get("counter") or 0) <= 0:
            continue
        msg = i.get("message") or {}
        mid, text = str(msg.get("id") or ""), msg.get("text") or ""
        if str(msg.get("author_id") or "") == me:
            continue
        private = i.get("type") == "user" or not str(i.get("id", "")).startswith("chat")
        mention = f"[USER={me}]" in text or f"[user={me}]" in text.lower()
        if not (private or mention):                         # общие чаты — только если позвали тебя
            continue
        key = f"im:{i.get('id')}:{mid}"
        if not mid or key in cfg["seen"]:
            continue
        cfg["seen"][key] = time.time()
        where = i.get("title") or "чат"
        head = f"💬 Битрикс, {where}" if private else f"💬 Битрикс: тебя упомянули в «{where}»"
        n = int(i.get("counter") or 0)
        out.append(f"{head}" + (f" ({n} непрочит.)" if n > 1 else "") + f": {_clean_bb(text)[:300]}")
    return out


# ── Почта ──
def _hdr(v) -> str:
    try:
        return str(email.header.make_header(email.header.decode_header(v or "")))
    except Exception:
        return str(v or "")


def mail_from_senders(imap_factory: Callable, mail_cfg: dict, cfg: dict) -> list:
    if not cfg["senders"]:
        return []
    m = imap_factory(mail_cfg)
    out = []
    try:
        m.select("INBOX", readonly=True)
        since = (datetime.now() - timedelta(days=2)).strftime("%d-%b-%Y")
        typ, data = m.search(None, "UNSEEN", "SINCE", since)
        for i in (data[0] or b"").split()[-30:]:
            typ, d = m.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])")
            msg = email.message_from_bytes(d[0][1])
            frm, subj = _hdr(msg["From"]), _hdr(msg["Subject"])
            mid = (msg["Message-ID"] or f"{frm}|{subj}").strip()
            if not any(s in frm.lower() for s in cfg["senders"]):
                continue
            key = f"mail:{mid}"
            if key in cfg["seen"]:
                continue
            cfg["seen"][key] = time.time()
            out.append(f"📧 Письмо от {frm}: «{subj or 'без темы'}». Спроси «что в письме от …» — перескажу.")
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return out


def check_once(db: Callable, bx, mail_cfg: Optional[dict], imap_factory: Optional[Callable], now: datetime,
               first_run: bool = False) -> list:
    """Что нового стоит сообщить. first_run — только запоминаем, что уже есть (не засыпаем старым при запуске)."""
    cfg = settings(db)
    out: list = []
    for name, on, f in (("сроки Битрикса", bx and cfg["deadlines"], lambda: bitrix_deadlines(bx, cfg, now)),
                        ("сообщения Битрикса", bx and cfg["bitrix_messages"], lambda: bitrix_messages(bx, cfg)),
                        ("почта", mail_cfg and imap_factory and cfg["mail"],
                         lambda: mail_from_senders(imap_factory, mail_cfg, cfg))):
        if not on:
            continue
        try:
            out += f()
        except Exception as e:
            log.warning("Сам пишу — %s: %s", name, e)
    save(db, cfg)
    if first_run:                                # сроки важны и сразу; старые сообщения и письма — уже не новость
        out = [x for x in out if x.startswith("⏳")]
    return out
