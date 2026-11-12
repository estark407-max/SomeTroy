#!/usr/bin/env python3
"""Личный ассистент в Telegram на Groq.

Понимает текст, голосовые, фото и PDF. Ставит напоминания (сам пишет в нужное время),
ищет в интернете, записывает идеи по проектам в приватный репозиторий GitHub.
Отвечает только владельцу (ALLOWED_USER_ID).

Запуск:
    python bot.py          # работает постоянно
    python bot.py --check  # проверить ключи и доступ к Telegram, Groq, GitHub
"""
import argparse
import base64
import calendar
import hashlib
import hmac
import json
import logging
import os
import re
import random
import secrets
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional
try:
    from zoneinfo import ZoneInfo
except ImportError:                      # Python 3.8 (Ubuntu 20.04): та же библиотека отдельным пакетом
    from backports.zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

import backup
import bitrix
import connectors
import desktop_api

# TROY_HOME — папка с настройками и базой. Программа на компьютере в локальном режиме
# указывает свою (%LOCALAPPDATA%\SomeTroy\server); на сервере — папка рядом с bot.py.
SERVER_VERSION = "0.6.1"          # программа сравнивает со своей версией и подсказывает обновить Троя на сервере
BASE = Path(os.environ.get("TROY_HOME") or Path(__file__).resolve().parent)
load_dotenv(BASE / "settings.env", override=True)
LOCAL_MODE = os.environ.get("TROY_LOCAL") == "1"     # всё на этом компьютере: без сертификата, только 127.0.0.1


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


TELEGRAM_TOKEN = env("TELEGRAM_TOKEN")
TELEGRAM_PROXY = env("TELEGRAM_PROXY")
TELEGRAM_BOT = env("TELEGRAM_BOT")               # адрес бота — для ссылок (не секрет)
TELEGRAM_LINK_CODE = env("TELEGRAM_LINK_CODE")   # одноразовый код: Telegram подключили позже, ждём хозяина
ALLOWED_USER_ID = env("ALLOWED_USER_ID")
USER_NAME = env("USER_NAME")
BOT_NAME = env("BOT_NAME", "Трой")

GROQ_API_KEY = env("GROQ_API_KEY")
GROQ_MODEL = env("GROQ_MODEL", "qwen/qwen3.8-27b")                  # понимает картинки и функции
GROQ_FALLBACK_MODEL = env("GROQ_FALLBACK_MODEL", "openai/gpt-oss-120b")
GROQ_SEARCH_MODEL = env("GROQ_SEARCH_MODEL", "openai/gpt-oss-120b")  # со встроенным поиском в интернете
GROQ_STT_MODEL = env("GROQ_STT_MODEL", "whisper-large-v3-turbo")     # распознавание голосовых
GROQ_PROXY = env("GROQ_PROXY")
# Цепочка моделей: у каждой модели Groq свой лимит, упёрлась одна — берём следующую
GROQ_MODELS = [m.strip() for m in env(
    "GROQ_MODELS", f"{GROQ_MODEL},{GROQ_FALLBACK_MODEL},llama-3.3-70b-versatile,openai/gpt-oss-20b").split(",") if m.strip()]
GROQ_MODELS = list(dict.fromkeys(GROQ_MODELS))
VISION_MODELS = {m.strip() for m in env("VISION_MODELS", "qwen/qwen3.8-27b,mistral-small-latest").split(",")}
# Запасной провайдер — Mistral (бесплатный режим без карты). Пусто — не используется
MISTRAL_API_KEY = env("MISTRAL_API_KEY")
MISTRAL_MODEL = env("MISTRAL_MODEL", "mistral-small-latest")
# Своя (обычно платная) модель — любой сервис с API как у OpenAI: OpenAI, Anthropic (Claude), Google Gemini,
# OpenRouter, DeepSeek и другие. Если указана — Трой отвечает ею, а бесплатные модели остаются запасными.
LLM_API_URL = env("LLM_API_URL")        # например https://api.openai.com/v1/
LLM_API_KEY = env("LLM_API_KEY")
LLM_MODEL = env("LLM_MODEL")            # название модели — как в кабинете сервиса
LLM_NAME = env("LLM_NAME")              # как называть в ответах «кто отвечает» (необязательно)
LLM_PROXY = env("LLM_PROXY")            # прокси, если сервис не открывается с сервера (необязательно)
LLM_VISION = env("LLM_VISION", "1")     # 1 — модель понимает картинки

GITHUB_TOKEN = env("GITHUB_TOKEN")
GITHUB_REPO = env("GITHUB_REPO")          # владелец/репозиторий
GITHUB_BRANCH = env("GITHUB_BRANCH", "main")

BITRIX_WEBHOOK = env("BITRIX_WEBHOOK")    # https://портал.bitrix24.ru/rest/1/xxxxxxxx/

DESKTOP_PORT = int(os.environ.get("TROY_LOCAL_PORT") or env("DESKTOP_PORT", "8443"))   # локальный режим — свободный порт

TZ_NAME = env("TIMEZONE", "Europe/Moscow")
TZ = ZoneInfo(TZ_NAME)
HISTORY_MESSAGES = int(env("HISTORY_MESSAGES", "10"))
HISTORY_CHARS = int(env("HISTORY_CHARS", "500"))     # длинные старые ответы в истории обрезаем — экономим лимит

DB_FILE = BASE / "assistant.db"
TLS_DIR = BASE / "tls"
LOCAL_IDEAS_DIR = BASE / "ideas"           # если GitHub не настроен
MAX_FILE_MB = 19                           # Telegram отдаёт ботам файлы до 20 МБ

log = logging.getLogger("assistant")
STARTED = time.time()


_SECRET_RE = re.compile(r"(bot)\d{5,12}:[\w-]{20,}|(gsk_)[\w-]{8,}|(/rest/\d+/)[\w]{6,}|(github_pat_|ghp_)[\w]{10,}|"
                        r"\b(sk-)[\w-]{16,}|\b(y0_)[\w-]{16,}|(Bearer )[\w.-]{16,}")


def redact(text: str) -> str:
    """Убрать из текста ошибки токены и ключи (Telegram вставляет токен прямо в адрес запроса)."""
    return _SECRET_RE.sub(lambda m: next(g for g in m.groups() if g) + "•••", str(text))


def human_error(e: BaseException) -> str:
    """Понятная причина вместо простыни из requests."""
    if isinstance(e, requests.exceptions.ProxyError):
        return "не удалось соединиться через прокси"
    if isinstance(e, requests.exceptions.SSLError):
        return "ошибка защищённого соединения (возможно, мешает антивирус или прокси)"
    if isinstance(e, requests.exceptions.Timeout):
        return "сервис долго не отвечает"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "нет соединения — проверь интернет; возможно, сайт заблокирован (нужен VPN или прокси)"
    return redact(str(e))[:200]


class _RedactFormatter(logging.Formatter):
    """Журнал целиком, вместе с текстом исключений, — без токенов и ключей."""
    def format(self, record):
        return redact(super().format(record))


class _RedactFilter(logging.Filter):
    def filter(self, record):
        try:
            msg_ = record.getMessage()
        except Exception:
            return True
        clean = redact(msg_)
        if clean != msg_:
            record.msg, record.args = clean, ()
        return True


class _RecentProblems(logging.Handler):
    """Последние предупреждения и ошибки — для /diag: видно, что именно пошло не так."""
    def __init__(self):
        super().__init__(logging.WARNING)
        self.items: list = []

    def emit(self, record):
        try:
            text = record.getMessage().splitlines()[0][:220]
        except Exception:
            return
        self.items = (self.items + [(record.created, record.levelname, text)])[-30:]


RECENT = _RecentProblems()
logging.getLogger().addHandler(RECENT)
for _h in logging.getLogger().handlers:          # и в журнал файлов/консоли токены не попадают
    _h.addFilter(_RedactFilter())
log.addFilter(_RedactFilter())
TG_STATE = {"ok": None, "error": "", "at": 0.0}     # связь с Telegram — для /diag

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
REPEATS = ["none", "daily", "weekdays", "weekly", "monthly", "yearly"]
REPEAT_RU = {"none": "", "daily": "каждый день", "weekdays": "по будням", "weekly": "каждую неделю",
             "monthly": "каждый месяц", "yearly": "каждый год"}


def make_session(proxy: str = "") -> requests.Session:
    s = requests.Session()
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    s.headers["User-Agent"] = "sometroy/0.1"
    return s


def now_local() -> datetime:
    return datetime.now(TZ)


def fmt_dt(dt: datetime) -> str:
    return f"{dt:%d.%m.%Y %H:%M}, {WEEKDAYS[dt.weekday()]}"


def split_text(text: str, limit: int = 4000) -> List[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text.strip():
        chunks.append(text)
    return chunks or ["…"]


# ───────────────────────── Telegram ─────────────────────────

class TelegramError(RuntimeError):
    def __init__(self, method: str, code, description: Optional[str]):
        self.code = code
        self.description = description or ""
        super().__init__(f"Telegram {method}: {code} {self.description}")


class Telegram:
    def __init__(self, token: str):
        self.token = token
        self.api = f"https://api.telegram.org/bot{token}/"
        self.s = make_session(TELEGRAM_PROXY)

    def call(self, method: str, timeout: int = 60, **kwargs):
        for _ in range(5):
            r = self.s.post(self.api + method, timeout=timeout, **kwargs)
            try:
                data = r.json()
            except ValueError:
                raise TelegramError(method, r.status_code, f"HTTP {r.status_code}")
            if data.get("ok"):
                return data["result"]
            retry = data.get("parameters", {}).get("retry_after")
            if r.status_code == 429 and retry:
                time.sleep(retry + 1)
                continue
            raise TelegramError(method, data.get("error_code", r.status_code), data.get("description"))
        raise TelegramError(method, 429, "слишком много повторов")

    def send(self, chat_id: str, text: str, buttons: Optional[list] = None) -> None:
        # Без parse_mode: модель может прислать любые символы, и Telegram не споткнётся о разметку
        chunks = split_text(text)
        for i, chunk in enumerate(chunks):
            data = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}
            if buttons and i == len(chunks) - 1:
                data["reply_markup"] = json.dumps({"inline_keyboard": buttons}, ensure_ascii=False)
            self.call("sendMessage", data=data)

    def typing(self, chat_id: str) -> None:
        try:
            self.call("sendChatAction", timeout=10, data={"chat_id": chat_id, "action": "typing"})
        except Exception:
            pass

    def download(self, file_id: str) -> bytes:
        info = self.call("getFile", data={"file_id": file_id})
        r = self.s.get(f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}", timeout=120)
        r.raise_for_status()
        return r.content


class KeepTyping:
    """Показывает «печатает…», пока бот думает (Telegram гасит статус через 5 секунд)."""

    def __init__(self, tg: Telegram, chat_id: str):
        self.tg, self.chat_id, self.stop = tg, chat_id, threading.Event()

    def __enter__(self):
        def loop():
            while not self.stop.is_set():
                self.tg.typing(self.chat_id)
                self.stop.wait(4.5)
        threading.Thread(target=loop, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.stop.set()


# ───────────────────────── База: напоминания и история ─────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    due     INTEGER NOT NULL,
    text    TEXT NOT NULL,
    repeat  TEXT NOT NULL DEFAULT 'none',
    active  INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_sessions (
    token_hash TEXT PRIMARY KEY,
    device     TEXT NOT NULL,
    ip         TEXT NOT NULL,
    created    INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pc_commands (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    device  TEXT,
    action  TEXT NOT NULL,
    arg     TEXT NOT NULL DEFAULT '',
    choices TEXT NOT NULL DEFAULT '',
    status  TEXT NOT NULL,
    channel TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    created INTEGER NOT NULL,
    result  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    role    TEXT NOT NULL,
    text    TEXT NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS message_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    role    TEXT NOT NULL,
    text    TEXT NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS message_log_chat_ts ON message_log(chat_id, ts);
"""


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with closing(db()) as con, con:
        con.executescript(SCHEMA)
        if "anchor" not in [c[1] for c in con.execute("PRAGMA table_info(reminders)")]:
            con.execute("ALTER TABLE reminders ADD COLUMN anchor INTEGER")   # исходное число месяца для повторов
        # журнал появился позже истории — при первом запуске переносим то, что уже есть
        con.execute("INSERT INTO message_log(chat_id, role, text, ts) SELECT chat_id, role, text, ts FROM history "
                    "WHERE NOT EXISTS (SELECT 1 FROM message_log) ORDER BY id")
        # старые обмены «открой X» → «Выбери компьютер.» учили модель отвечать без вызова функции — убираем их
        con.execute("DELETE FROM history WHERE id IN (SELECT h.id + 0 FROM history h WHERE h.role = 'assistant' AND "
                    "(h.text LIKE 'Выбери компьютер%' OR h.text LIKE 'Есть: %') UNION "
                    "SELECT h.id - 1 FROM history h WHERE h.role = 'assistant' AND "
                    "(h.text LIKE 'Выбери компьютер%' OR h.text LIKE 'Есть: %'))")


LOG_KEEP_DAYS = 365


def add_history(chat_id: str, role: str, text: str) -> None:
    with closing(db()) as con, con:
        con.execute("INSERT INTO history(chat_id, role, text, ts) VALUES (?,?,?,?)",
                    (chat_id, role, text, int(time.time())))
        con.execute("INSERT INTO message_log(chat_id, role, text, ts) VALUES (?,?,?,?)",
                    (chat_id, role, text, int(time.time())))
        if random.random() < 0.01:                      # журнал храним год
            con.execute("DELETE FROM message_log WHERE ts < ?", (int(time.time()) - LOG_KEEP_DAYS * 86400,))
        con.execute("DELETE FROM history WHERE chat_id=? AND id NOT IN "
                    "(SELECT id FROM history WHERE chat_id=? ORDER BY id DESC LIMIT 200)", (chat_id, chat_id))


def get_history(chat_id: str) -> list:
    with closing(db()) as con:
        rows = con.execute("SELECT role, text FROM history WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                           (chat_id, HISTORY_MESSAGES)).fetchall()
    rows = list(reversed(rows))
    while rows and rows[0]["role"] not in ("user", "pc_user"):   # разговор должен начинаться с пользователя
        rows.pop(0)
    def cut(t: str) -> str:
        return t if len(t) <= HISTORY_CHARS else t[:HISTORY_CHARS] + "…"
    out: list = []
    for r in rows:
        role, text = r["role"], r["text"]
        if role == "pc_reply":       # не «Выбери компьютер.» — иначе модель повторяет эту фразу без вызова функции
            text = "[передал команду компьютеру через pc_command]"
        elif role == "pc_result":
            text = f"[результат с компьютера: {text}]"
        role = "user" if role in ("user", "pc_user") else "assistant"
        if out and out[-1]["role"] == role:                       # подряд два ответа — склеиваем
            out[-1]["content"] += "\n" + cut(text)
        else:
            out.append({"role": role, "content": cut(text)})
    return out


# «Сохрани переписку за неделю» — журнал сообщений файлом (в Telegram — документом, в программе — в «Загрузки\\Трой»)
LOG_RE = re.compile(r"^(?:(?:трой|эй)[\s,!.:—-]+)?(?:сохрани|выгрузи|экспортируй|скачай|скинь|пришли|отправь|дай|сделай|"
                    r"собери|сформируй|выведи|запиши в файл)\s+(?:мне\s+)?(?:(?:весь|всю|полный|полную|текстовый|"
                    r"текстовую|нашу|наш|эту|этот)\s+){0,2}(?:лог(?:и|а|у|ов)?|переписк\w*|чат|диалог|"
                    r"истори\w*\s+(?:сообщений|переписки|чата|разговора))\b(.*)$", re.I)
_LOG_NUM = {"один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "десять": 10}


def log_period(rest: str) -> tuple:
    """(с какого времени, подпись) по словам «за сегодня», «за неделю», «за 3 дня», «за месяц»…"""
    r = (rest or "").lower()
    today = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    if "сегодн" in r:
        return today.timestamp(), "за сегодня", None
    if "вчера" in r:
        return (today - timedelta(days=1)).timestamp(), "за вчера", today.timestamp()
    m = re.search(r"(\d+|" + "|".join(_LOG_NUM) + r")?\s*(?<![а-яё])(час|д(?:ень|ня|ней)|сутк|недел|месяц|год)", r)
    if m:
        n = int(m.group(1)) if (m.group(1) or "").isdigit() else _LOG_NUM.get(m.group(1) or "", 1)
        unit = m.group(2)
        sec = {"час": 3600, "сутк": 86400, "недел": 7 * 86400, "месяц": 30 * 86400, "год": 365 * 86400}.get(
            unit, 86400) * n
        word = {"час": "ч", "сутк": "сут", "недел": "нед", "месяц": "мес", "год": "г"}.get(unit, "дн")
        return time.time() - sec, f"за {n} {word}." if n > 1 else {"час": "за час", "сутк": "за сутки",
               "недел": "за неделю", "месяц": "за месяц", "год": "за год"}.get(unit, "за день"), None
    return 0, "вся сохранённая", None


def export_log(chat_id: str, rest: str = "") -> Optional[tuple]:
    """(имя файла, текст, сколько сообщений) или None, если за период пусто."""
    since, label, until = log_period(rest)
    with closing(db()) as con:
        rows = con.execute("SELECT role, text, ts FROM message_log WHERE chat_id=? AND ts>=? AND ts<? ORDER BY id",
                           (chat_id, int(since), int(until or time.time() + 60))).fetchall()
    if not rows:
        return None
    me = USER_NAME or "Ты"
    out = [f"Переписка — {BOT_NAME}, {label}", f"Сохранено: {now_local():%d.%m.%Y %H:%M}, сообщений: {len(rows)}", ""]
    day = ""
    for r in rows:
        dt = datetime.fromtimestamp(r["ts"], TZ)
        if f"{dt:%d.%m.%Y}" != day:
            day = f"{dt:%d.%m.%Y}"
            out += ["", f"── {day} ──"]
        who = {"user": me, "pc_user": me, "assistant": BOT_NAME, "pc_reply": BOT_NAME}.get(r["role"])
        out.append(f"{dt:%H:%M} {who}: {r['text']}" if who else f"{dt:%H:%M} 🖥 {r['text']}")
    name = f"Трой — переписка {now_local():%Y-%m-%d %H-%M}.txt"
    return name, "\n".join(out) + "\n", len(rows)


# Пароли, ключи и токены в чате: нейросети не отправляем, в историю не пишем
SECRET_IN_TEXT = re.compile(
    r"парол\w*\s*[:=]\s*\S+|парол\w*(?:\s+(?:от|для|на|к)\s+\S+)?\s+(?=\S*\d)(?=\S*[A-Za-z])\S{6,}[\s.!]*$|"
    r"\bpassword\s*[:=]\s*\S+|"
    r"ssh-(?:rsa|ed25519|ecdsa)\s+AAAA[\w+/=]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\bgsk_[\w-]{10,}|\b\d{6,12}:AA[\w-]{30,}|\bgithub_pat_\w{10,}|\bghp_\w{20,}|\bsk-[\w-]{20,}|\by0_[\w-]{20,}", re.I)


def secret_guard(text: str) -> Optional[str]:
    if not SECRET_IN_TEXT.search(text or ""):
        return None
    tip = ("Если это данные сервера — подключи его в программе: 🔌 «Подключения» → «Сервер (VPS)»."
           if LOCAL_MODE else "Ключи и доступы вводятся в программе: 🔌 «Подключения».")
    return ("🔒 Похоже, в сообщении пароль или ключ. Я не отправил его нейросети и не сохранил в переписке. "
            f"Пароли и ключи в чат не пиши. {tip}\n\nЕсли этот пароль уже где-то засветился — лучше смени его.")


def log_request(assistant, chat_id: str, text: str) -> Optional[str]:
    """Просьба сохранить журнал — без нейросети. Файл кладём в assistant.export, канал его доставит."""
    m = LOG_RE.match(re.sub(r"^\[голосовое\]\s*", "", (text or "").strip(), flags=re.I))
    if not m:
        return None
    got = export_log(chat_id, m.group(1))
    if not got:
        return "За этот период сообщений нет."
    name, body, n = got
    assistant.export = (name, body.encode("utf-8"))
    return f"🗂 Сохранил переписку: {n} сообщ. ({log_period(m.group(1))[1]})."


def clear_history(chat_id: str) -> None:
    with closing(db()) as con, con:
        con.execute("DELETE FROM history WHERE chat_id=?", (chat_id,))


def parse_when(when: str) -> datetime:
    when = when.strip().replace("T", " ")
    for f in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(when[:19], f)
            if f == "%Y-%m-%d":
                dt = dt.replace(hour=9)
            return dt.replace(tzinfo=TZ)
        except ValueError:
            continue
    raise ValueError(f"не понял время «{when}», нужен формат ГГГГ-ММ-ДД ЧЧ:ММ")


def next_due(dt: datetime, repeat: str, anchor: int = 0) -> Optional[datetime]:
    """anchor — исходное число месяца: «31-го» после февраля снова 31-е, а не 28-е."""
    day = anchor or dt.day
    if repeat == "daily":
        return dt + timedelta(days=1)
    if repeat == "weekdays":
        n = dt + timedelta(days=1)
        while n.weekday() >= 5:
            n += timedelta(days=1)
        return n
    if repeat == "weekly":
        return dt + timedelta(weeks=1)
    if repeat == "monthly":
        y, m = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
        return dt.replace(year=y, month=m, day=min(day, calendar.monthrange(y, m)[1]))
    if repeat == "yearly":
        y = dt.year + 1
        return dt.replace(year=y, day=min(day, calendar.monthrange(y, dt.month)[1]))
    return None


def add_reminder(chat_id: str, when: str, text: str, repeat: str = "none") -> str:
    repeat = repeat if repeat in REPEATS else "none"
    dt = parse_when(when)
    if dt <= now_local() and repeat == "none":
        return f"Ошибка: {fmt_dt(dt)} уже прошло. Уточни время."
    anchor = dt.day
    while dt <= now_local():
        dt = next_due(dt, repeat, anchor)
    with closing(db()) as con, con:
        cur = con.execute("INSERT INTO reminders(chat_id, due, text, repeat, anchor) VALUES (?,?,?,?,?)",
                          (chat_id, int(dt.timestamp()), text, repeat, anchor))
    rep = f" ({REPEAT_RU[repeat]})" if repeat != "none" else ""
    return f"Напоминание №{cur.lastrowid} поставлено на {fmt_dt(dt)}{rep}: {text}"


def list_reminders(chat_id: str) -> str:
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM reminders WHERE chat_id=? AND active=1 ORDER BY due",
                           (chat_id,)).fetchall()
    if not rows:
        return "Активных напоминаний нет."
    lines = []
    for r in rows:
        rep = f" ({REPEAT_RU[r['repeat']]})" if r["repeat"] != "none" else ""
        lines.append(f"№{r['id']} — {fmt_dt(datetime.fromtimestamp(r['due'], TZ))}{rep}: {r['text']}")
    return "\n".join(lines)


def delete_reminder(chat_id: str, rid: int) -> str:
    with closing(db()) as con, con:
        cur = con.execute("UPDATE reminders SET active=0 WHERE id=? AND chat_id=? AND active=1", (rid, chat_id))
    return f"Напоминание №{rid} удалено." if cur.rowcount else f"Напоминания №{rid} нет среди активных."


EVENTS: list = []                  # напоминания для программы на компьютере: {"id", "ts", "text"}
EVENTS_LOCK = threading.Lock()


def push_event(text: str) -> None:
    with EVENTS_LOCK:
        EVENTS.append({"id": (EVENTS[-1]["id"] + 1) if EVENTS else int(time.time() * 1000), "ts": int(time.time()), "text": text})
        del EVENTS[:-100]


def events_after(after: int) -> list:
    with EVENTS_LOCK:
        return [e for e in EVENTS if e["id"] > after]


def reminder_loop(stop: threading.Event) -> None:
    tg = None                       # своя сессия: requests.Session не любит несколько потоков
    pushed: set = set()
    while not stop.is_set():
        try:
            if tg_linked() and (tg is None or tg.token != TELEGRAM_TOKEN):
                tg = Telegram(TELEGRAM_TOKEN)
            now = int(time.time())
            with closing(db()) as con:
                rows = con.execute("SELECT * FROM reminders WHERE active=1 AND due<=?", (now,)).fetchall()
            for r in rows:
                if r["chat_id"] == ALLOWED_USER_ID and (r["id"], r["due"]) not in pushed:
                    pushed.add((r["id"], r["due"]))   # на компьютер — один раз, даже если Telegram недоступен
                    push_event(f"⏰ {r['text']}")
                try:
                    if tg_linked():         # без Telegram напоминание приходит только в программу
                        tg.send(r["chat_id"], f"⏰ {r['text']}")
                except requests.RequestException as e:
                    log.warning("Нет связи с Telegram, напоминание №%s отправлю позже: %s", r["id"], e)
                    continue
                except TelegramError as e:
                    log.error("Не отправил напоминание №%s: %s", r["id"], e)
                anchor = r["anchor"] or 0
                nxt = next_due(datetime.fromtimestamp(r["due"], TZ), r["repeat"], anchor)
                while nxt and nxt.timestamp() <= now:   # пропущенные (бот был выключен) не дублируем
                    nxt = next_due(nxt, r["repeat"], anchor)
                with closing(db()) as con, con:
                    if nxt:
                        con.execute("UPDATE reminders SET due=? WHERE id=?", (int(nxt.timestamp()), r["id"]))
                    else:
                        con.execute("UPDATE reminders SET active=0 WHERE id=?", (r["id"],))
        except Exception:
            log.exception("Ошибка в цикле напоминаний")
        stop.wait(15)


# ───────────────────────── Идеи (GitHub или локальная папка) ─────────────────────────

def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9а-яё]+", "-", name.strip().lower()).strip("-")
    return s[:60] or "inbox"


def _idea_norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (text or "").lower().replace("ё", "е")).split())


def idea_duplicate(content: str, text: str) -> Optional[str]:
    """Дата записи, если такая идея в проекте уже есть (модель любит сохранять одно и то же дважды)."""
    import difflib
    new = _idea_norm(text)
    if not new:
        return None
    for line in reversed(content.splitlines()[-200:]):
        m = re.match(r"- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) — (.*)", line)
        if not m:
            continue
        old = _idea_norm(m.group(2))
        if old == new or (len(new) > 20 and new in old) or difflib.SequenceMatcher(None, old, new).ratio() >= 0.85:
            return m.group(1)
    return None


class Ideas:
    def __init__(self):
        self.github = bool(GITHUB_TOKEN and GITHUB_REPO)
        self.s = make_session()
        if self.github:
            self.s.headers.update({"Authorization": f"Bearer {GITHUB_TOKEN}",
                                   "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28"})
        self._projects: List[str] = []
        self._projects_at = 0.0

    def _url(self, path: str) -> str:
        return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"

    def _get(self, path: str):
        """(текст, sha) или (None, None), если файла нет."""
        if not self.github:
            p = LOCAL_IDEAS_DIR / path
            return (p.read_text(encoding="utf-8"), None) if p.exists() else (None, None)
        r = self.s.get(self._url(path), params={"ref": GITHUB_BRANCH}, timeout=30)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        data = r.json()
        return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]

    def _put(self, path: str, text: str, sha: Optional[str], message: str) -> None:
        if not self.github:
            p = LOCAL_IDEAS_DIR / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            return
        body = {"message": message, "branch": GITHUB_BRANCH,
                "content": base64.b64encode(text.encode("utf-8")).decode()}
        if sha:
            body["sha"] = sha
        r = self.s.put(self._url(path), json=body, timeout=30)
        if r.status_code == 409:
            raise RuntimeError("conflict")
        r.raise_for_status()

    def projects(self) -> List[str]:
        if time.time() - self._projects_at < 600:
            return self._projects
        try:
            if self.github:
                r = self.s.get(self._url("projects"), params={"ref": GITHUB_BRANCH}, timeout=30)
                items = r.json() if r.status_code == 200 else []
                names = [i["name"] for i in items if isinstance(i, dict) and i.get("name", "").endswith(".md")]
            else:
                d = LOCAL_IDEAS_DIR / "projects"
                names = [p.name for p in d.glob("*.md")] if d.exists() else []
            self._projects = sorted(n[:-3] for n in names)
            self._projects_at = time.time()
        except Exception as e:
            log.warning("Не получил список проектов: %s", e)
        return self._projects

    def save(self, project: str, text: str) -> str:
        slug = slugify(project)
        path = f"projects/{slug}.md"
        stamp = now_local().strftime("%Y-%m-%d %H:%M")
        for attempt in range(3):
            content, sha = self._get(path)
            if content is None:
                content = f"# {project.strip() or slug}\n\n"
            dup = idea_duplicate(content, text)
            if dup:
                return f"Такая идея уже записана в «{slug}» ({dup}) — повтор не сохраняю."
            if not content.endswith("\n"):
                content += "\n"
            content += f"- {stamp} — {text.strip()}\n"
            try:
                self._put(path, content, sha, f"Идея: {slug}")
                break
            except RuntimeError:   # файл успели изменить — перечитываем
                time.sleep(1)
        else:
            return "Ошибка: не удалось записать идею, попробуй ещё раз."
        self._projects_at = 0
        where = "в GitHub" if self.github else "на сервере"
        return f"Записано {where} в проект «{slug}»."

    def read(self, project: str) -> str:
        content, _ = self._get(f"projects/{slugify(project)}.md")
        if content is None:
            return f"Проекта «{slugify(project)}» пока нет. Есть: {', '.join(self.projects()) or 'ничего'}."
        return content[-6000:]


# ───────────────────────── Groq ─────────────────────────

class LLMError(RuntimeError):
    def __init__(self, status: int, message: str, provider: str = "Groq"):
        self.status = status
        self.text = message
        super().__init__(f"{provider} {status}: {message}")


class Groq:
    API = "https://api.groq.com/openai/v1/"
    NAME = "Groq"
    MAX_TOKENS_FIELD = "max_completion_tokens"

    def __init__(self, key: str = "", proxy: str = ""):
        self.s = make_session(proxy or GROQ_PROXY)
        self.s.headers["Authorization"] = f"Bearer {key or GROQ_API_KEY}"

    def _raise(self, r: requests.Response):
        try:
            msg = r.json().get("error", {})
            msg = msg.get("message", r.text[:300]) if isinstance(msg, dict) else (r.json().get("message") or r.text[:300])
        except ValueError:
            msg = r.text[:300]
        raise LLMError(r.status_code, msg, self.NAME)

    def prepare(self, messages: list) -> list:
        return messages

    def chat(self, model: str, messages: list, tools: Optional[list] = None, **extra) -> dict:
        body = {"model": model, "messages": self.prepare(messages), "temperature": 0.4,
                self.MAX_TOKENS_FIELD: 2048, **extra}
        if tools:
            body["tools"] = tools
        for attempt in range(2):
            r = self.s.post(self.API + "chat/completions", json=body, timeout=180)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]
            if r.status_code in (500, 502, 503) and attempt == 0:
                time.sleep(3)
                continue
            self._raise(r)
        raise LLMError(503, "сервис недоступен")

    def transcribe(self, audio: bytes, filename: str) -> str:
        r = self.s.post(self.API + "audio/transcriptions", timeout=180,
                        data={"model": GROQ_STT_MODEL, "language": "ru", "response_format": "json"},
                        files={"file": (filename, audio)})
        if r.status_code != 200:
            self._raise(r)
        return (r.json().get("text") or "").strip()

    def models(self) -> List[str]:
        r = self.s.get(self.API + "models", timeout=30)
        if r.status_code != 200:
            self._raise(r)
        return [m["id"] for m in r.json().get("data", [])]


class Mistral(Groq):
    """Запасной провайдер: тот же формат запросов (как у OpenAI)."""
    API = "https://api.mistral.ai/v1/"
    NAME = "Mistral"
    MAX_TOKENS_FIELD = "max_tokens"

    def __init__(self):
        super().__init__(MISTRAL_API_KEY, GROQ_PROXY)

    def prepare(self, messages: list) -> list:
        out = []
        for m in messages:
            if isinstance(m.get("content"), list):   # картинка у Mistral — строкой
                m = {**m, "content": [{"type": "image_url", "image_url": p["image_url"]["url"]}
                                      if p.get("type") == "image_url" else p for p in m["content"]]}
            out.append(m)
        return out


LLM_PRESETS = {                          # адрес → имя и особенности сервиса
    "api.openai.com": ("OpenAI", "max_completion_tokens"),
    "api.anthropic.com": ("Claude", "max_tokens"),
    "generativelanguage.googleapis.com": ("Gemini", "max_tokens"),
    "openrouter.ai": ("OpenRouter", "max_tokens"),
    "api.deepseek.com": ("DeepSeek", "max_tokens"),
    "api.mistral.ai": ("Mistral", "max_tokens"),
}


class CustomLLM(Groq):
    """Своя модель через API в формате OpenAI (chat/completions)."""

    def __init__(self, url: str, key: str, name: str = ""):
        super().__init__(key, LLM_PROXY or GROQ_PROXY)
        self.API = url.rstrip("/") + "/"
        host = re.sub(r"^https?://", "", self.API).split("/")[0]
        preset = next((v for k, v in LLM_PRESETS.items() if host.endswith(k)), (host, "max_tokens"))
        self.NAME = name or preset[0]
        self.MAX_TOKENS_FIELD = preset[1]
        self.no_temperature = False

    def chat(self, model: str, messages: list, tools: Optional[list] = None, **extra) -> dict:
        body = {"model": model, "messages": self.prepare(messages), self.MAX_TOKENS_FIELD: 2048, **extra}
        if not self.no_temperature:
            body["temperature"] = 0.4
        if tools:
            body["tools"] = tools
        for attempt in range(3):
            r = self.s.post(self.API + "chat/completions", json=body, timeout=180)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]
            if r.status_code == 400 and "temperature" in r.text and "temperature" in body:
                self.no_temperature = True          # некоторые модели принимают только температуру по умолчанию
                body.pop("temperature")
                continue
            if r.status_code in (500, 502, 503, 529) and attempt == 0:
                time.sleep(3)
                continue
            self._raise(r)
        raise LLMError(503, "сервис недоступен", self.NAME)


def make_custom_llm() -> Optional["CustomLLM"]:
    if not (LLM_API_URL and LLM_API_KEY and LLM_MODEL):
        return None
    if LLM_VISION == "1" and "deepseek" not in LLM_API_URL:
        VISION_MODELS.add(LLM_MODEL)
    return CustomLLM(LLM_API_URL, LLM_API_KEY, LLM_NAME)


RETRY_RE = re.compile(r"try again in\s+(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.I)


class Router:
    """Выбор модели: по очереди Groq-модели, затем Mistral; помнит, какая модель «на перерыве»."""

    def __init__(self, groq: Groq):
        self.groq = groq
        self.mistral = Mistral() if MISTRAL_API_KEY else None
        self.custom = make_custom_llm()      # своя (платная) модель — первой, бесплатные — запасные
        self.cool: dict = {}        # модель -> до какого времени не трогать
        self.last: Optional[tuple] = None   # (провайдер, модель, время) — кто отвечал последним
        self.errors: dict = {}              # модель -> (время, код, текст) последней ошибки

    def chain(self) -> list:
        items = [(self.custom, LLM_MODEL)] if self.custom else []
        items += [(self.groq, m) for m in GROQ_MODELS]
        if self.mistral:
            items.append((self.mistral, MISTRAL_MODEL))
        return items

    def candidates(self, vision: bool, skip: set) -> list:
        now = time.time()
        free = [(p, m) for p, m in self.chain() if self.cool.get(m, 0) <= now and m not in skip]
        if vision:   # с картинкой — сначала модели, которые видят
            free.sort(key=lambda pm: pm[1] not in VISION_MODELS)
        return free

    def status(self) -> str:
        now = time.time()
        lines = []
        for p, m in self.chain():
            left = self.cool.get(m, 0) - now
            state = "свободна" if left <= 0 else (f"перерыв ещё {int(left // 60)} мин" if left >= 60
                                                  else f"перерыв ещё {int(left)} с")
            lines.append(f"- {getattr(p, 'NAME', '')} {m}: {state}")
            if m in self.errors and now - self.errors[m][0] < 6 * 3600:
                t, code, text = self.errors[m]
                lines.append(f"    последняя ошибка {datetime.fromtimestamp(t, TZ):%H:%M}: {code} {text}")
        if not self.mistral:
            lines.append("- Mistral: не подключён (нет MISTRAL_API_KEY)")
        if not self.custom:
            lines.append("- Своя модель: не подключена (LLM_API_URL, LLM_API_KEY, LLM_MODEL)")
        last = (f"Последним отвечал: {self.last[0]} {self.last[1]} в "
                f"{datetime.fromtimestamp(self.last[2], TZ):%H:%M}") if self.last else "С запуска ещё никто не отвечал."
        return "Модели Троя (по порядку):\n" + "\n".join(lines) + "\n\n" + last + "\n\n" + pcs_status()

    def describe(self, provider, model) -> str:
        names = ", ".join(f"{getattr(p, 'NAME', '')} {m}" for p, m in self.chain())
        return (f"\n\nО себе (если спросят): ты — {BOT_NAME}, ассистент, который по очереди использует модели: {names}. "
                f"Этот ответ пишет {getattr(provider, 'NAME', '')} {model}. Если модель упирается в бесплатный лимит, "
                f"{BOT_NAME} автоматически переключается на следующую.")

    def wait_hint(self) -> int:
        now = time.time()
        waits = [max(0, self.cool.get(m, 0) - now) for _, m in self.chain()]
        return int(min(waits)) if waits else 60

    def penalize(self, model: str, e: LLMError) -> None:
        self.errors[model] = (time.time(), e.status, (e.text or "")[:140])
        if e.status == 429:
            m = RETRY_RE.search(e.text or "")
            sec = 60.0
            if m and any(m.groups()):
                sec = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0) + 2
            if "per day" in (e.text or "") or "TPD" in (e.text or "") or "RPD" in (e.text or ""):
                sec = max(sec, 600)
            self.cool[model] = time.time() + sec
            log.warning("%s на перерыве %d с: %s", model, sec, (e.text or "")[:120])
        elif e.status in (500, 502, 503):
            self.cool[model] = time.time() + 30
        elif e.status in (401, 402, 403):     # ключ не подошёл или кончились деньги — пока отвечают запасные
            self.cool[model] = time.time() + 1800
            log.warning("%s недоступна (%s) — 30 мин отвечают запасные модели", model, e.status)


THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def clean_answer(text: Optional[str]) -> str:
    return THINK_RE.sub("", text or "").strip()


def pdf_text(data: bytes) -> str:
    from io import BytesIO
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(data))
    text = "\n".join((p.extract_text() or "") for p in reader.pages[:30]).strip()
    return text[:30000]


def fn(name: str, description: str, props: Optional[dict] = None, required: Optional[list] = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}


TOOLS = [
    fn("create_reminder", "Поставить напоминание. Бот сам пришлёт сообщение в указанное время.", {
        "when": {"type": "string", "description": "Местное время срабатывания, формат ГГГГ-ММ-ДД ЧЧ:ММ"},
        "text": {"type": "string", "description": "Что напомнить, коротко, от второго лица"},
        "repeat": {"type": "string", "enum": REPEATS,
                   "description": "Повтор: none — один раз, daily, weekdays (пн–пт), weekly, monthly, yearly"}},
       ["when", "text"]),
    fn("list_reminders", "Показать активные напоминания с номерами."),
    fn("delete_reminder", "Удалить напоминание по номеру.", {"id": {"type": "integer"}}, ["id"]),
    fn("save_idea", "Записать идею или заметку в файл проекта (его потом читает Claude на рабочих сессиях).", {
        "project": {"type": "string",
                    "description": "Имя проекта латиницей через дефис (как в списке существующих) или inbox"},
        "text": {"type": "string", "description": "Идея словами пользователя, без потери деталей"}},
       ["project", "text"]),
    fn("read_ideas", "Прочитать записанные идеи проекта.", {"project": {"type": "string"}}, ["project"]),
    fn("web_search", "Поиск в интернете: цены, где купить, адреса, часы работы, расписания, новости, курсы, "
                     "погода и всё, что могло измениться.",
       {"query": {"type": "string", "description": "Подробный поисковый запрос с городом/страной, если важно"}},
       ["query"]),
]

BITRIX_TOOLS = [fn(*t) for t in bitrix.TOOLS]
PC_ACTIONS = {"shutdown": "выключить компьютер", "restart": "перезагрузить компьютер", "sleep": "отправить в сон",
              "lock": "заблокировать экран", "open_app": "открыть", "close_app": "закрыть",
              "play_pause": "пауза / продолжить", "next_track": "следующий трек", "prev_track": "предыдущий трек",
              "volume_up": "громче", "volume_down": "тише", "mute": "выключить / включить звук",
              "find_app": "найти программу"}
PC_CONFIRM = {"shutdown", "restart"}
PC_TOOL = fn("pc_command", "Управление компьютером, где запущена программа Трой: питание, открыть/закрыть программу, "
                           "музыка и громкость. shutdown и restart — ТОЛЬКО после «да» пользователя.", {
    "action": {"type": "string", "enum": [a for a in PC_ACTIONS if a != "find_app"]},
    "app": {"type": "string", "description": "для open_app/close_app: название программы как в меню Пуск "
                                             "(Telegram, Google Chrome, Блокнот, Word, Steam…)"},
    "device": {"type": "string", "description": "имя компьютера, только если пользователь назвал его явно"}},
    ["action"])
SITE_TOOL = fn("open_site", "Открыть сайт в браузере на компьютере пользователя (вход, капча, скачивание — делает сам "
                           "пользователь). mode: direct — напрямую с компьютера, server — через сервер Троя, как VPN "
                           "(сайт видит адрес сервера; помогает, если сайт не открывается). Не знаешь mode — не передавай: "
                           "Трой сам спросит или возьмёт запомненный.", {
    "url": {"type": "string", "description": "полный адрес сайта, https://…; не знаешь адрес — сначала web_search"},
    "mode": {"type": "string", "enum": ["direct", "server"]}}, ["url"])
SITE_HINT_RE = re.compile(r"сайт|страниц|https?://|\b[\w-]+\.(?:ru|com|org|net|io|me|su|рф|info|tv|online|site|to)\b|"
                          r"через сервер|напрямую|\bв браузер|зайди на|перейди на", re.I)
SITE_DIRECT_RE = re.compile(r"^(?:\[голосовое\]\s*)?(?:давай\s+)?(?:напрямую|прямо|обычно|стандартно|как обычно|сам[оа]?|"
                            r"с компьютера|без сервера)\b", re.I)
SITE_SERVER_RE = re.compile(r"^(?:\[голосовое\]\s*)?(?:давай\s+)?(?:через сервер|сервер|через впн|впн|vpn|через vpn)\b", re.I)
FILE_MAX_MB = 50
FILE_TOOL = fn("pc_file", f"Переслать файл (до {FILE_MAX_MB} МБ) с одного компьютера с Троем на другой. Файл ищется по имени "
                         "на рабочем столе, в Документах и Загрузках (или по полному пути) и приходит в «Загрузки\\Трой». "
                         "После доставки копия с сервера удаляется.", {
    "file": {"type": "string", "description": "имя файла или его часть («отчёт.xlsx», «договор»), или полный путь"},
    "source": {"type": "string", "description": "с какого компьютера взять"},
    "target": {"type": "string", "description": "на какой компьютер положить"}}, ["file"])
FILE_HINT_RE = re.compile(r"файл|документ|папк|перекин|перешл|пересл|скин[уь]|закин|забер|перетащ|переда[йт]", re.I)
# Пасхалка: Трой назван в честь Троя Маклюра из «Симпсонов» — и представляется так же
WHO_RE = re.compile(r"^\s*(?:\S+[,!]\s*)?(?:кто ты|ты кто|кто ты такой|ты кто такой|представься|представьтесь|"
                    r"как тебя зовут|расскажи о себе|who are you)\s*[?!.]*\s*$", re.I)
MCCLURE_FILMS = [
    "«Напоминание на завтра в 9:00»", "«Где купить термопасту подешевле»", "«Выключи комп, я спать»",
    "«Погода на выходные»", "«Задача в Битриксе до понедельника»", "«Следующий трек»",
    "«Идея для проекта, записанная в 3 часа ночи»", "«Открой хром и Яндекс Музыку»",
    "«Сколько сделок закрыли в октябре»", "«Заблокируй комп, я отошёл»", "«Перезагрузить? Ответь „да“»",
    "«Голосовое на ходу»", "«Пароль для приложения»", "«Выбери компьютер»",
]


def mcclure(text: str) -> Optional[str]:
    """«Кто ты?» → «Привет, я Трой! Возможно, вы помните меня по таким фильмам, как …»"""
    text = re.sub(r"^\[голосовое\]\s*", "", (text or "").strip(), flags=re.I)
    if not WHO_RE.match(text.replace("ё", "е").replace("Ё", "Е")):
        return None
    a, b = random.sample(MCCLURE_FILMS, 2)
    return (f"Привет, я {BOT_NAME}! Возможно, вы помните меня по таким фильмам, как {a} и {b}.\n\n"
            "Я твой помощник: напоминания, поиск, идеи для проектов"
            + (", Битрикс24" if BITRIX_WEBHOOK else "") + (", котёл" if BOILER_ON else "")
            + " и управление компьютерами — текстом и голосом.")


# Котёл: только чтение — из автоматики котла (/opt/boiler-auto) на этом же сервере
BOILER_DIR = os.getenv("BOILER_DIR", "/opt/boiler-auto")   # модуль для котлов DM350 с автоматикой boiler-auto
BOILER_TOOL = fn("boiler_info", "Котёл (только чтение): status — работает ли, теплоноситель, уставка, топливо в "
                                "бункере, улица, режим отопления, погодная автоматика; loads — загрузки бункера "
                                "(мешки, кг, деньги) за период.", {
    "what": {"type": "string", "enum": ["status", "loads"]},
    "date_from": {"type": "string", "description": "для loads: с какой даты, YYYY-MM-DD"},
    "date_to": {"type": "string", "description": "для loads: по какую дату включительно, YYYY-MM-DD"}},
    ["what"])
BOILER_ON = os.path.exists(os.path.join(BOILER_DIR, "boiler_auto.py"))
BOILER_HINT_RE = re.compile(r"кот[её]л|котл|бункер|мешк|топлив|теплонос|уставк|отоплен|батаре", re.I)
BOILER_RULES = """
- Котёл: boiler_info. Только смотришь — управлять котлом ты не можешь (для этого приложение «КотелТройка»).
  «Как котёл», «работает ли», «сколько топлива» — what=status. «Сколько мешков / потратили на топливо за …» —
  what=loads с датами периода. Отвечай коротко, суммы с разделителями тысяч."""
# Читаем данные котла, запуская его же код отдельным процессом. Запись в его файлы отключена,
# поэтому Трой ничего не может изменить в автоматике котла.
_BOILER_SCRIPT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import boiler_auto as b
mem = {}
def no_write(path, data):
    if path == b.STATE_FILE and isinstance(data, dict) and data.get("token"):
        mem["token"] = data["token"]
b.write_json = no_write
_load = b.load_state
b.load_state = lambda: dict(_load(), **({"token": mem["token"]} if mem.get("token") else {}))
what, frm, to = sys.argv[2], sys.argv[3] or None, sys.argv[4] or None
out = b.report_text() if what == "status" else b.loads_period(frm, to)
print(json.dumps(out, ensure_ascii=False))
"""


def boiler_info(what: str, frm: str = "", to: str = "") -> str:
    if not os.path.exists(os.path.join(BOILER_DIR, "boiler_auto.py")):
        return "Автоматика котла на сервере не найдена."
    for d in (frm, to):
        if d and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            return "Ошибка: даты в виде YYYY-MM-DD."
    try:
        r = subprocess.run(["/usr/bin/env", "python3", "-B", "-c", _BOILER_SCRIPT, BOILER_DIR, what, frm or "", to or ""],
                           cwd=BOILER_DIR, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "Котёл не ответил за минуту — сервер котла, видимо, недоступен."
    if r.returncode != 0:
        log.warning("Котёл: %s", r.stderr[-500:])
        return "Не получилось прочитать данные котла."
    data = json.loads(r.stdout.strip().splitlines()[-1])
    if what == "status":
        return data
    s = data.get("summary") or {}
    lines = [f"Загрузки бункера {frm or 'с начала учёта'} — {to or 'сегодня'}: загрузок {s.get('count', 0)}, "
             f"{s.get('bags', 0)} мешков, {s.get('kg', 0)} кг, {s.get('cost', 0)} ₽."]
    for i in (data.get("items") or [])[:15]:
        lines.append(f"- {i.get('date')}: {i.get('count')} × {i.get('weight')} кг, {i.get('cost')} ₽")
    if len(data.get("items") or []) > 15:
        lines.append(f"…и ещё {len(data['items']) - 15}")
    return "\n".join(lines)


# умный дом: «включи свет», «выключи кондиционер» — это не команды компьютеру
HOME_RE = re.compile(r"\b(?:свет\w*|ламп\w*|люстр\w*|ночник\w*|розетк\w*|кондиционер\w*|обогревател\w*|"
                     r"штор\w*|жалюзи|чайник\w*|увлажнител\w*|пылесос\w*|сценари\w*|гирлянд\w*|умн\w* дом\w*|"
                     r"телевизор\w*|телик\w*|тв|вентилятор\w*|кондей|кондер\w*|подсветк\w*|торшер\w*|бра)\b", re.I)


# прямое поручение компьютеру — модель обязана вызвать pc_command, а не просто ответить
PC_ORDER_RE = re.compile(r"\b(?:открой|запусти|включи|закрой|выключи|перезагрузи|усыпи|заблокируй|переключи|"
                         r"поставь на паузу|пауза|громче|тише|следующ\w*|предыдущ\w*)\b", re.I)
PC_HINT_RE = re.compile(r"компьют|браузер|хром|chrome|edge|\bкомп\b|\bпк\b|ноут|выключ|перезагр|усып|\bсон\b|спящ|блокир|открой|запусти|"
                        r"закрой|трек|песн|музык|громк|громч|тише|тиш[ео]|пауз|звук|плеер|прилож|программ|диспетчер", re.I)
# Простые команды компьютеру понимаем сами, без модели: надёжно, мгновенно и без расхода лимитов
_PC_OBJ = r"(?:\s+(?:мой\s+|этот\s+)?(?:комп\w*|пк|ноут\w*|экран|систему))?"
PC_FAST = [
    (re.compile(r"^(?:выключи|выключить|выруби|отключи)\s+(?:мой\s+|этот\s+)?(?:комп\w*|пк|ноут\w*)$"), "shutdown", None),
    (re.compile(r"^(?:перезагрузи|перезагрузить|перезапусти|ребутни|рестартни)" + _PC_OBJ + "$"), "restart", None),
    (re.compile(r"^(?:заблокируй|заблокировать|блокируй|блокировка|залочь)" + _PC_OBJ + "$"), "lock", None),
    (re.compile(r"^(?:усыпи|усыпить|в сон|спать|спящий режим|отправь в сон|переведи в сон|отправь" + _PC_OBJ +
                r"\s+в сон|переведи" + _PC_OBJ + r"\s+в сон)" + _PC_OBJ + "$"), "sleep", None),
    (re.compile(r"^(?:выключи|отключи|убери|вырубай|выруби)\s+звук$|^(?:без звука|заглуши(?:\s+звук)?|включи звук|"
                r"верни звук|мьют|mute)$"), "mute", None),
    (re.compile(r"^(?:следующ\w*|дальше|некст|next|скипни|пропусти)(?:\s+(?:трек|песн\w*|композици\w*|музык\w*))?$|"
                r"^(?:переключи|смени)\s+(?:трек|песн\w*|композици\w*)$"), "next_track", None),
    (re.compile(r"^(?:предыдущ\w*|прошл\w*|верни|назад)(?:\s+(?:трек|песн\w*|композици\w*))?$"), "prev_track", None),
    (re.compile(r"^(?:пауза|на паузу|поставь на паузу|продолжи(?:ть)?(?:\s+музык\w*)?|плей|play|стоп|"
                r"(?:останови|выключи|включи|поставь)\s+музык\w*|воспроизвед\w*)$"), "play_pause", None),
    (re.compile(r"^(?:громче|погромче|ещ[её] громче|прибавь(?:\s+(?:звук|громкость))?|увеличь\s+громкость|"
                r"сделай\s+(?:по)?громче)$"), "volume_up", None),
    (re.compile(r"^(?:тише|потише|ещ[её] тише|убавь(?:\s+(?:звук|громкость))?|уменьши\s+громкость|"
                r"сделай\s+(?:по)?тише)$"), "volume_down", None),
    (re.compile(r"^(?:открой|запусти|включи|открыть|запустить)\s+(?!сайт|ссылк|страниц|файл|папк)(.{2,40})$"), "open_app", 1),
    (re.compile(r"^(?:закрой|закрыть|заверши|завершить|выключи|вырубай|выруби)\s+(?!комп|пк|ноут|звук)(.{2,40})$"),
     "close_app", 1),
]


# как латинская буква может прозвучать или быть написана по-русски: «office-pc» = «офис пс» = «оффис пк»
_SOUNDS = {"a": "аэе", "b": "б", "c": "кцс", "d": "д", "e": "еэи", "f": "ф", "g": "гж", "h": "х", "i": "иайы",
           "j": "джй", "k": "к", "l": "л", "m": "м", "n": "н", "o": "оа", "p": "п", "q": "к", "r": "р", "s": "сз",
           "t": "т", "u": "уюа", "v": "в", "w": "ву", "x": "кс", "y": "йиы", "z": "зс"}
_SOUND_MULTI = {"j": ["дж"], "x": ["кс"], "i": ["ай"], "u": ["ю"]}
_SPOKEN = {"pc": ["пи\\s*си", "писи", "пэ\\s*ка", "пэ\\s*сэ"], "nb": ["нб", "ноут"]}


def _letter_rx(ch: str, prev: str) -> str:
    alts = [re.escape(ch)] + list(_SOUNDS.get(ch, "")) + _SOUND_MULTI.get(ch, [])
    rx = "(?:" + "|".join(sorted(set(alts), key=len, reverse=True)) + ")"
    return rx + "?" if ch == prev else rx          # «eez» можно сказать как «ез»


def device_rx(device: str) -> str:
    """Как имя компьютера может прозвучать в сообщении: «OFFICE-PC» = «office pc», «officepc», «офис пс», «оффис пк»."""
    parts = []
    for tok in re.findall(r"[a-zа-я]+|\d+", device.lower().replace("ё", "е")):
        if re.fullmatch(r"[a-z]+", tok):
            letters = "".join(_letter_rx(ch, tok[i - 1] if i else "") for i, ch in enumerate(tok))
            if len(tok) > 3 and tok.endswith("e"):          # немая «e» на конце: «office» = «офис»
                letters += "?"
            alts = [letters] + _SPOKEN.get(tok, [])
            parts.append("(?:" + "|".join(alts) + ")")
        else:
            parts.append(re.escape(tok))
    return r"[\s\-_]*".join(parts)


_PREFIX = r"(?:(?<![\w-])(?:на|у|для|в)\s+)?(?:(?:компьютер\w*|комп\w*|пк|ноут\w*)\s+)?"


def device_span(t: str):
    """(имя компьютера, совпадение в тексте) — если в тексте назван один из компьютеров в сети."""
    for d in sorted(PRESENCE, key=len, reverse=True):
        m = re.search(_PREFIX + r"(?<![\w-])" + device_rx(d) + r"(?![\w-])", t)
        if m:
            return d, m
    # «на зеоне», «на work»: одна отличительная часть имени, если она есть только у одного компьютера
    hits = {}
    for d in PRESENCE:
        for tok in re.findall(r"[a-z]{3,}", d.lower()):
            if tok in ("desktop", "laptop", "user", "home", "work") and len(PRESENCE) > 1 and tok != d.lower():
                continue
            m = re.search(_PREFIX + r"(?<![\w-])" + device_rx(tok) + r"\w{0,3}(?![\w-])", t)
            # часть имени — только «на офисе» или «выключи офис» (иначе «открой office studio» ушло бы не туда)
            if m and (re.match(r"\s*(?:на|у|для|в|комп|пк|ноут)", m.group(0)) or
                      len((t[:m.start()] + " " + t[m.end():]).split()) == 1):
                hits[d] = m
    if len(hits) == 1:
        return next(iter(hits.items()))
    return "", None


def find_device(text: str) -> str:
    """Имя компьютера из тех, что в сети, если оно названо в тексте."""
    return device_span(text.lower().replace("ё", "е"))[0]


def pc_fast_match(text: str):
    """«Трой, заблокируй комп!» → ("lock", ""). Не похоже на простую команду → None."""
    t = re.sub(r"^\[голосовое\]\s*", "", text.strip(), flags=re.I).lower().replace("ё", "е")
    t = re.sub(rf"^(?:{re.escape(BOT_NAME.lower())}|трой|эй|слушай)[\s,!.:—-]+", "", t)
    t = re.sub(r"\b(?:пожалуйста|плиз|please|быстро|мне)\b", " ", t)
    t = re.sub(r"[.!?,]+$", "", re.sub(r"\s+", " ", t)).strip(" ,")
    if BOILER_HINT_RE.search(t) or HOME_RE.search(t) or SITE_HINT_RE.search(t):   # котёл, умный дом, сайты — не программы
        return None
    device, m = device_span(t)              # «выключи office pc», «открой телеграм на офисном»
    if device:
        t = re.sub(r"\s+", " ", t[:m.start()] + " " + t[m.end():]).strip(" ,")
        if len(t.split()) == 1:                # остался один глагол — значит, речь о самом компьютере
            t += " комп"
    if not t or len(t.split()) > 6:
        return None
    for rx, action, group in PC_FAST:
        m = rx.match(t)
        if m:
            arg = m.group(group).strip() if group else ""
            if action in ("open_app", "close_app") and len(arg.split()) > 4:
                return None
            return action, arg, device
    return None


# Функции Битрикса (это ~2000 токенов) отправляем модели, только когда разговор про Битрикс
BITRIX_HINT_RE = re.compile(r"битрикс|bitrix|crm|црм|задач|сделк|лид[аыуов ]|лид$|воронк|клиент|контакт|компани[яию]|"
                            r"продаж|выручк|менеджер|аналитик|отч[её]т|коллег|сотрудник|чат|непрочитан|"
                            r"сообщени[ея] (?:ане|ивану|коллег)|ответь [А-ЯЁ]|напиши [А-ЯЁ]", re.I)
# «Трой, пароль для приложения: Abc123» — перехватываем до модели, пароль в Groq не уходит
PASSWORD_RE = re.compile(r"^\s*(?:\S+[,!]\s*)?(?:(?:новый|смени|поменяй|установи|задай|поставь)\s+)?пароль\s+"
                         r"(?:для|от|на|в)\s+(?:приложени\w*|программ\w*|десктоп\w*|компьютер\w*|клиент\w*)"
                         r"(?:\s*[:=—–-]\s*|\s+(?:на\s+|это\s+)?)(\S+)\s*$", re.I)
BITRIX_WRITE = {"bitrix_task_create", "bitrix_task_complete", "bitrix_task_comment", "bitrix_send_message",
                "bitrix_crm_create", "bitrix_crm_update"}
YES_WORDS = (r"да|ага|угу|ок|окей|ok|yes|давай|отправляй|отправь|отправить|подтверждаю|верно|делай|создавай|"
             r"создай|закрывай|согласен|го|поехали|выполняй|конечно|всё верно|все верно|\+")
YES_RE = re.compile(rf"^(\[голосовое\]\s*)?({YES_WORDS})(?=$|[\s,.!])", re.I)
# ответ — только «да» (без поправок): тогда подтверждённое действие выполняем сами, без нейросети
YES_ONLY_RE = re.compile(rf"^(?:\[голосовое\]\s*)?(?:трой[\s,.!]*)?(?:(?:{YES_WORDS})(?:[\s,.!]+|$))+(?:трой[\s,.!]*)?$", re.I)
NO_RE = re.compile(r"^(?:\[голосовое\]\s*)?(?:нет|не надо|не нужно|не делай|отмена|отмени|стоп)\b[\s,.!]*$", re.I)


BX_PENDING: dict = {}       # chat_id -> действие в Битриксе, которое ждёт «да» (name, args, ts, what)


def system_prompt(projects: List[str], bitrix_on: bool = False, pc_on: bool = False, boiler_on: bool = False) -> str:
    now = now_local()
    who = f" пользователя по имени {USER_NAME}" if USER_NAME else ""
    return f"""Тебя зовут {BOT_NAME}. Ты — личный помощник{who} (в Telegram и в программе на компьютере). Отвечай всегда по-русски, коротко и по делу.
Обращение по имени («{BOT_NAME}, …») — просто обращение к тебе, отвечай на суть. Представляйся этим именем, если спросят.
Пиши обычным текстом: без Markdown, звёздочек, решёток и таблиц, Telegram их не отрисует. Списки — через «-».

Сейчас {now:%Y-%m-%d %H:%M}, {WEEKDAYS[now.weekday()]}, часовой пояс {TZ_NAME}.

Правила:
- Напоминания: вызывай create_reminder. «Через 20 минут», «в пятницу», «завтра утром» переводи в точное время
  от текущего момента («утром» — 09:00, «днём» — 13:00, «вечером» — 19:00). Если непонятно, на когда, — спроси.
  После создания подтверди дату, день недели и время.
- Идеи и заметки для проектов («идея», «запиши», «для проекта …»): вызывай save_idea.
  Существующие проекты: {', '.join(projects) if projects else 'пока нет'}.
  Подбирай подходящий по смыслу. Если не ясно, к какому, — пиши в inbox.
  Новый проект заводи, только если пользователь явно назвал новый.
- Всё, что могло измениться (цены, наличие, где купить, адреса, часы работы, расписания, новости, курсы валют),
  ищи через web_search, а не отвечай по памяти. Указывай источники ссылками.
- Сообщения с пометкой [голосовое] — это расшифровка речи пользователя: отвечай на смысл, не пересказывай.
  В расшифровке бывают ошибки распознавания — понимай по смыслу.
- Фото и документы: сделай то, о чём просит подпись. Без подписи — коротко скажи, что там, и чем можешь помочь.
- Если не уверен в факте — так и скажи.{APP_RULES}{BITRIX_RULES if bitrix_on else ""}{PC_RULES if pc_on else ""}{BOILER_RULES if boiler_on else ""}"""


APP_RULES = """
- О себе (SomeTroy): настройки — в программе Трой на компьютере. Кнопка 🔌 «Подключения»: Telegram, запасная модель
  Mistral, идеи в GitHub, Битрикс24, календари, почта, умный дом""" + (""", «Сервер (VPS)» — перенести тебя
  с компьютера на свой сервер, чтобы ты работал круглосуточно (нужны только IP и пароль сервера, ввод — там же)"""
  if LOCAL_MODE else "") + """. Переписку файлом — «сохрани переписку» или /log. Ошибку разработчикам — /bug.
  Пароли, ключи и токены НИКОГДА не проси прислать в чат: их вводят только в окнах программы.
  Сам ты на серверы по SSH не ходишь и ничего на них не ставишь — это делает программа."""


PC_RULES = """
- Сайты («открой сайт X», «зайди на X.ru», «открой рутрекер в браузере») — open_site с полным адресом https://…
  (не знаешь адрес — сначала web_search). Программы на компьютере — pc_command, сайты — open_site.
- Файл с одного компьютера на другой («перекинь отчёт с ZEON на EEZ40») — pc_file; до 50 МБ.
- Компьютеры, где запущен Трой: pc_command. Выключение и перезагрузку — только после «да» (спроси «Выключаю?»).
  Остальное (сон, блокировка, открыть/закрыть программу, музыка, громкость) — сразу. Если компьютеров несколько,
  пользователю покажут кнопки выбора — просто скажи «Выбери компьютер». device передавай, только если его назвали.
  Ты МОЖЕШЬ открывать и закрывать на компьютере любые программы, включая браузер (app: "браузер", "хром"…).
  Несколько программ — одним вызовом через «и» (app: "хром и яндекс музыка").
  Каждая новая просьба про компьютер («а браузер?», «это оно», «и телеграм») — снова вызов pc_command; слова без
  вызова ничего не делают. Если программа не нашлась, а пользователь подтверждает похожую — открой похожую."""


BITRIX_RULES = """
- Битрикс24 (портал пользователя, ты действуешь от его имени): задачи, чаты, CRM, аналитика — функции bitrix_*.
  Чтение (списки, поиск, аналитика) делай сразу. Изменение (поставить или закрыть задачу, комментарий, сообщение,
  запись CRM) — тоже вызывай функцию сразу, как понял, что нужно: она сама не выполнится, а вернёт, что именно будет
  сделано, — покажи это пользователю дословно и спроси «Делаю?». После «да» Трой выполнит сам.
  НИКОГДА не пиши, что задача поставлена, сообщение отправлено или запись создана, если функция не вернула «✅».
  Исполнителя можно передать именем и фамилией — Трой найдёт его сам. Продажи и выручку за период считай с
  period_by=closed, а «сколько пришло» — с period_by=created. Для нестандартной аналитики используй bitrix_read
  и сам посчитай итоги. Суммы показывай с разделителями тысяч."""


class Assistant:
    def __init__(self):
        self.llm = Groq()
        self.router = Router(self.llm)
        self.ideas = Ideas()
        self.bx = bitrix.Bitrix(BITRIX_WEBHOOK, tz=TZ) if BITRIX_WEBHOOK else None
        self.lock = threading.Lock()     # Telegram и программа на компьютере — по очереди
        self.pc_used = False
        self.user_text = ""
        self.channel = "telegram"
        self.pc_choice: Optional[dict] = None
        self.pending_power: dict = {}    # chat_id -> (action, когда спросили) — ждём «да» на выключение

    def web_search(self, query: str) -> str:
        messages = [
            {"role": "system", "content": f"Найди в интернете актуальный ответ и изложи факты кратко: цены, адреса, "
                                          f"даты, названия, со ссылками на источники. Сегодня {now_local():%Y-%m-%d}. "
                                          f"Отвечай по-русски."},
            {"role": "user", "content": query}]
        # gpt-oss иногда выдаёт битый вызов поиска (Groq отвечает 400 «Parsing failed») — пробуем ещё раз и запасную
        attempts = [(GROQ_SEARCH_MODEL, "low"), (GROQ_SEARCH_MODEL, "medium"), ("openai/gpt-oss-20b", "low")]
        last: Optional[Exception] = None
        for model, effort in attempts:
            try:
                msg = self.llm.chat(model, messages, tools=[{"type": "browser_search"}], reasoning_effort=effort)
                text = clean_answer(msg.get("content"))
                if text:
                    return text
            except LLMError as e:
                last = e
                log.warning("Поиск (%s): %s", model, e)
                if e.status == 429:
                    break
        return ("ПОИСК НЕ УДАЛСЯ" + (f" ({last})" if last else "") +
                ". Скажи пользователю, что сейчас не получилось поискать, и предложи повторить через минуту; "
                "не выдумывай цены и наличие.")

    def execute(self, chat_id: str, name: str, args: dict) -> str:
        try:
            if name == "create_reminder":
                return add_reminder(chat_id, str(args.get("when", "")), str(args.get("text", "")),
                                    str(args.get("repeat") or "none"))
            if name == "list_reminders":
                return list_reminders(chat_id)
            if name == "delete_reminder":
                return delete_reminder(chat_id, int(args.get("id", 0)))
            if name == "save_idea":
                return self.ideas.save(str(args.get("project") or "inbox"), str(args.get("text", "")))
            if name == "read_ideas":
                return self.ideas.read(str(args.get("project") or "inbox"))
            if name == "web_search":
                return self.web_search(str(args.get("query", "")))
            if name == "boiler_info":
                return boiler_info(str(args.get("what") or "status"), str(args.get("date_from") or ""),
                                   str(args.get("date_to") or ""))
            if name == "pc_command":
                return self.pc_command(chat_id, args)
            if name == "open_site":
                self.pc_used = True
                return self.open_site(chat_id, str(args.get("url") or ""), str(args.get("mode") or ""))
            if name == "pc_file":
                self.pc_used = True
                return file_command(str(args.get("file") or ""), str(args.get("source") or ""),
                                    str(args.get("target") or ""), self.channel, chat_id, self.user_text)
            if name == "route_link":
                return connectors.route_link(self.places, args)
            if name == "taxi_link":
                return connectors.taxi_link(self.places, args, from_pc=getattr(self, "channel", "") == "desktop")
            if name == "save_place":
                return self.places.save(str(args.get("name") or ""), str(args.get("address") or ""))
            if self.conns.owner_of(name):
                if self.conns.is_write(name) and not YES_RE.match(self.user_text.strip()):
                    return ("НЕ ВЫПОЛНЕНО: сначала покажи пользователю, что именно добавишь (название, дата, время), "
                            "и спроси «Добавляю?». Вызови функцию снова только после его ответа «да».")
                return self.conns.run(name, args)
            if name.startswith("bitrix_"):
                if not self.bx:
                    return "Ошибка: Битрикс24 не подключён (нет BITRIX_WEBHOOK в настройках)."
                if name in BITRIX_WRITE:
                    # изменение — только после отдельного «да» на точное описание (не «создай задачу…» и не «да, но…»)
                    try:
                        what = bitrix.describe(self.bx, name, args)
                    except bitrix.BitrixError as e:
                        return f"НЕ ВЫПОЛНЕНО: {e}. Скажи это пользователю и спроси, как быть."
                    run_id = getattr(self, "run_id", 0)
                    pend = BX_PENDING.get(chat_id)
                    if not pend or pend.get("run") != run_id:    # новое сообщение — новый список действий
                        pend = {"items": [], "run": run_id}
                    item = {"name": name, "args": dict(args), "what": what}
                    if item not in pend["items"]:                # «две задачи разом» — ждут подтверждения обе
                        pend["items"].append(item)
                    pend["ts"] = time.time()
                    BX_PENDING[chat_id] = pend
                    return ("НЕ ВЫПОЛНЕНО — нужно подтверждение. Покажи пользователю дословно и спроси «Делаю?»:\n"
                            + what)
                return bitrix.execute(self.bx, name, args)
            return f"Ошибка: неизвестная функция {name}"
        except Exception as e:
            log.exception("Функция %s упала", name)
            return f"Ошибка: {redact(str(e))}"

    @property
    def places(self) -> "connectors.Places":
        """Места пользователя для маршрутов: дом, работа…"""
        if getattr(self, "_places", None) is None:
            self._places = connectors.Places(db)
        return self._places

    @property
    def conns(self) -> "connectors.Connections":
        """Подключения: календари, почта, умный дом, расписания (доступы — в базе Троя)."""
        if getattr(self, "_conns", None) is None:
            self._conns = connectors.Connections(db, TZ)
        return self._conns

    @staticmethod
    def strip_images(messages: list) -> list:
        """Запасная модель не видит картинки — заменяем их пометкой."""
        out = []
        for m in messages:
            if isinstance(m.get("content"), list):
                texts = [p["text"] for p in m["content"] if p.get("type") == "text"]
                if any(p.get("type") == "image_url" for p in m["content"]):
                    texts.insert(0, "[фото — эта модель его не видит, попроси прислать позже]")
                m = {**m, "content": "\n".join(texts)}
            out.append(m)
        return out

    def pc_command(self, chat_id: str, args: dict) -> str:
        action = str(args.get("action") or "")
        if action not in PC_ACTIONS:
            return f"Ошибка: неизвестное действие {action}"
        if action in PC_CONFIRM and not YES_RE.match(self.user_text.strip()):
            return "НЕ ВЫПОЛНЕНО: сначала спроси пользователя «Выключаю?» / «Перезагружаю?» и вызови снова после «да»."
        app = str(args.get("app") or "").strip()
        if action in ("open_app", "close_app") and not app:
            return "Ошибка: укажи app — какую программу открыть или закрыть."
        device = str(args.get("device") or "").strip()
        if app and find_device(app):
            self.pc_used = True
            return (f"Ошибка: «{app}» — это компьютер, а не программа. Уточни у пользователя, что сделать с ним: "
                    "выключить, перезагрузить, усыпить или заблокировать.")
        # модель любит «помнить» прошлый выбор — компьютер без кнопок, только если его назвали в ЭТОМ сообщении
        named = find_device(self.user_text) or (device if device and device.lower() in self.user_text.lower() else "")
        device = named if device and named else ""
        text, choice = pc_create(action, app, device, self.channel, chat_id)
        self.pc_used = True
        if choice:
            self.pc_choice = choice
        return text

    def open_site(self, chat_id: str, url: str, mode: str = "", _from_answer: bool = False) -> str:
        url = url.strip().strip("«»\"'")
        if not url:
            return "Ошибка: укажи url — адрес сайта."
        if not re.match(r"https?://", url, re.I):
            url = "https://" + url
        host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", url, flags=re.I).split("/")[0].lower())
        if getattr(self, "channel", "") != "desktop":    # браузер — только в программе на компьютере
            return ("Сайты Трой открывает только в программе на компьютере — попроси там. "
                    "Передай это пользователю коротко.")
        modes = kv_json("site_modes")
        local = LOCAL_MODE
        if local:
            mode = "direct"                           # Трой живёт на этом компьютере — «через сервер» не бывает
        said = SITE_SERVER_RE.search(self.user_text or "") or SITE_DIRECT_RE.search(self.user_text or "") or re.search(
            r"напрямую|через сервер|через впн|через vpn|без сервера", self.user_text or "", re.I)
        if mode and not said and not local and not _from_answer:
            mode = ""                                 # модель сама выбрала режим — нет: решает пользователь
        if mode in ("direct", "server"):
            if not local and modes.get(host) != mode:
                modes[host] = mode
                kv_json_set("site_modes", modes)
        else:
            mode = modes.get(host, "")
        if not mode:
            if not hasattr(self, "pending_site"):
                self.pending_site = {}
            self.pending_site[chat_id] = (url, time.time())
            return (f"НЕ ОТКРЫТО: спроси пользователя одной фразой — открыть {host} напрямую с компьютера или через сервер "
                    "(как VPN: сайт увидит адрес сервера, помогает, если сайт не открывается)? Ответ запомню для этого сайта.")
        device = getattr(self, "caller_device", "")      # тот компьютер, с которого попросили
        have = PC_VERSION.get(device, "") if device else ""
        if have and tuple(int(x) for x in have.split(".") if x.isdigit()) < (0, 4, 0):
            return (f"НЕ ОТКРЫТО: программа Troy на компьютере {device} старой версии ({have}) и не умеет открывать сайты. "
                    "Скажи пользователю обновить программу (версия 0.4.0 или новее).")
        text, choice = pc_create("open_site", json.dumps({"url": url, "mode": mode}, ensure_ascii=False), device,
                                 self.channel, chat_id)
        if choice:
            self.pc_choice = choice
        return text + (" Режим: через сервер." if mode == "server" else " Режим: напрямую.")

    def bx_confirm(self, chat_id: str) -> Optional[str]:
        """«Да» на «Делаю?» по Битриксу — выполняем подтверждённое сами (нейросеть могла бы его «забыть»)."""
        pend = BX_PENDING.get(chat_id)
        if not pend or not self.bx:
            return None
        text = (self.user_text or "").strip()
        newer = max([v[1] for v in (getattr(self, "pending_power", {}).get(chat_id),
                                    getattr(self, "pending_site", {}).get(chat_id)) if v] or [0])
        if time.time() - pend["ts"] > 1800 or newer > pend["ts"]:
            BX_PENDING.pop(chat_id, None)                   # устарело, или «да» относится уже к другому вопросу
            return None
        if NO_RE.match(text):
            BX_PENDING.pop(chat_id, None)
            return "Хорошо, не делаю."
        BX_PENDING.pop(chat_id, None)
        if not YES_ONLY_RE.match(text):
            return None                 # другая тема или поправка («да, но срок пятница») — спросим заново, если нужно
        out = []
        for it in pend["items"]:
            try:
                out.append(bitrix.execute(self.bx, it["name"], it["args"]))
            except Exception as e:
                log.exception("Битрикс: %s", it["name"])
                out.append(f"⚠️ Не получилось ({it['what'][:60]}): {redact(str(e))}")
        return "\n".join(out)

    def fast_pc(self, chat_id: str) -> Optional[str]:
        """Простые команды компьютеру — без модели."""
        if getattr(self, "channel", "") != "desktop" and SITE_HINT_RE.search(self.user_text or "") and re.match(
                r"^(?:\[голосовое\]\s*)?(?:трой[,\s]+)?(?:открой|зайди|перейди)\b", (self.user_text or "").strip(), re.I):
            return "🌐 Сайты я открываю в программе Трой на компьютере — попроси там, и я открою окно браузера."
        m = re.match(r"^(?:\[голосовое\]\s*)?(?:трой[,\s]+)?(?:открой|зайди на|перейди на)\s+(?:сайт\s+)?"
                     r"((?:https?://)?[\w.-]+\.(?:ru|com|org|net|io|me|su|рф|info|tv|online|site|to)(?:/\S*)?)"
                     r"(?:\s+(напрямую|через сервер))?\s*[.!]*$", (self.user_text or "").strip(), re.I)
        if m and getattr(self, "channel", "") == "desktop":       # «открой ya.ru» — адрес уже есть, нейросеть не нужна
            self.pc_used = True
            text = self.open_site(chat_id, m.group(1), "server" if (m.group(2) or "").lower() == "через сервер"
                                  else "direct" if m.group(2) else "")
            if text.startswith("НЕ ОТКРЫТО: программа"):
                return ("⚠️ " + text[len("НЕ ОТКРЫТО: "):].split(" Скажи пользователю")[0]
                        + " Обнови её — тогда сайты откроются вкладкой в окне Троя.")
            if text.startswith("НЕ ОТКРЫТО"):
                host = re.sub(r"^https?://", "", m.group(1)).split("/")[0]
                return f"Как открыть {host}: напрямую с компьютера или через сервер (как VPN — сайт увидит адрес сервера)?"
            host = re.sub(r"^https?://", "", m.group(1)).split("/")[0]
            if self.pc_choice:
                return "Выбери компьютер."
            if "отправлена" not in text:
                return text
            return f"Открываю {host} — {'через сервер' if 'через сервер' in text else 'напрямую'}."
        pend = getattr(self, "pending_site", {}).get(chat_id)
        if pend and time.time() - pend[1] < 600:     # ответ на «напрямую или через сервер?»
            mode = "server" if SITE_SERVER_RE.match(self.user_text.strip()) else \
                "direct" if SITE_DIRECT_RE.match(self.user_text.strip()) else ""
            if mode:
                self.pending_site.pop(chat_id, None)
                self.pc_used = True
                text = self.open_site(chat_id, pend[0], mode, _from_answer=True)
                if text.startswith("НЕ ОТКРЫТО: программа"):
                    return ("⚠️ " + text[len("НЕ ОТКРЫТО: "):].split(" Скажи пользователю")[0]
                            + " Обнови её — тогда сайты откроются вкладкой в окне Троя. Выбор режима я запомнил.")
                host = re.sub(r"^https?://", "", pend[0]).split("/")[0]
                return ("Выбери компьютер." if self.pc_choice else
                        f"Открываю {host} {'через сервер' if mode == 'server' else 'напрямую'}. Запомнил — "
                        "в следующий раз не спрошу (сказать иначе: «открой … напрямую» / «через сервер»).")
        ycfg = self.conns.enabled().get("yhome")
        if ycfg:                                 # «выключи ферма 2» — устройство умного дома, а не программа
            home = connectors.yiot_fast(ycfg, self.user_text, TZ)
            if home:
                return home
            down = connectors.yiot_unavailable(ycfg, self.user_text)
            if down:
                return down
            if connectors.yiot_mentions(ycfg, self.user_text):
                return None
        pending = getattr(self, "pending_power", {}).pop(chat_id, None)
        if pending and time.time() - pending[1] < 90 and YES_RE.match(self.user_text.strip()):
            text, choice = pc_create(pending[0], "", pending[2], self.channel, chat_id)
            self.pc_choice, self.pc_used = choice, True
            return text.replace(" Результат придёт отдельным сообщением.", "")
        hit = pc_fast_match(self.user_text)
        if not hit:
            return None
        action, arg, device = hit
        log.info("Команда компьютеру без модели: %s %s %s", action, arg, device)
        if action in PC_CONFIRM:
            if not hasattr(self, "pending_power"):
                self.pending_power = {}
            self.pending_power[chat_id] = (action, time.time(), device)
            self.pc_used = True
            where = device or "компьютер"
            return (f"Выключить {where}?" if action == "shutdown" else f"Перезагрузить {where}?") + " Ответь «да»."
        text, choice = pc_create(action, arg, device, self.channel, chat_id)
        self.pc_choice, self.pc_used = choice, True
        if choice:
            return "Выбери компьютер."
        if text.startswith("Команда"):
            return f"Есть: {pc_label(action, arg)}" + (f" на {device}." if device else ".")
        return text

    def run(self, chat_id: str, content, channel: str = "telegram") -> str:
        """Текст не в той раскладке («ghbdtn») сначала переводим — и говорим, как поняли."""
        self.caller_device = getattr(threading.current_thread(), "troy_device", "")   # компьютер, с которого спросили
        self.reply_quote = getattr(threading.current_thread(), "troy_reply", "")      # ответ на конкретное сообщение
        threading.current_thread().troy_reply = ""
        fixed = connectors.fix_layout(content) if isinstance(content, str) else content
        if isinstance(fixed, str) and fixed != content and secret_guard(fixed):
            content = fixed              # в ответ «Понял как…» пароль не попадёт
        answer = self._run_main(chat_id, fixed, channel)
        if fixed != content and isinstance(answer, str):
            answer = f"⌨️ Понял как: «{fixed.strip()}»\n\n{answer}"
        return answer

    def _run_main(self, chat_id: str, content, channel: str = "telegram") -> str:
        with self.lock:
            self.channel = channel
            self.pc_choice, self.pc_used = None, False
            self.user_text = content if isinstance(content, str) else " ".join(
                p.get("text", "") for p in content if p.get("type") == "text")
            if isinstance(content, str):
                secret = secret_guard(self.user_text)
                if secret:
                    self.secret_seen = True
                    return secret
                saved = log_request(self, chat_id, self.user_text)
                if saved:
                    return saved
                hello = mcclure(self.user_text)
                if hello:
                    return hello
                fast = self.bx_confirm(chat_id) or self.fast_pc(chat_id)
                if fast:
                    return fast
            return self._run(chat_id, content)

    @staticmethod
    def flatten_tools(messages: list) -> list:
        """При смене модели посреди шагов: вызовы функций → обычный текст (чтобы не выполнить их второй раз)."""
        out, done = [], []
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                continue
            if m.get("role") == "tool":
                done.append(m["content"][:1500])
                continue
            out.append(m)
        if done:
            out.append({"role": "assistant", "content": "Уже выполнено (повторно не вызывать):\n" + "\n".join(done)})
            out.append({"role": "user", "content": "Продолжай: ответь пользователю по результатам выше."})
        return out

    def bitrix_needed(self, history: list) -> bool:
        if not self.bx:
            return False
        recent = [self.user_text] + [h["content"] for h in history[-4:] if isinstance(h.get("content"), str)]
        return any(BITRIX_HINT_RE.search(t or "") for t in recent)

    def _run(self, chat_id: str, content) -> str:
        self.run_id = time.time()
        history = get_history(chat_id)
        use_bx = self.bitrix_needed(history)
        recent = ([self.user_text, getattr(self, "reply_quote", "")]
                  + [h["content"] for h in history[-2:] if isinstance(h.get("content"), str)])
        use_pc = any(PC_HINT_RE.search(t or "") for t in recent)
        use_boiler = BOILER_ON and any(BOILER_HINT_RE.search(t or "") for t in recent)
        conn_tools, conn_names = self.conns.tools_for(recent)
        use_route = any(re.search(connectors.ROUTE_HINT, t or "", re.I) for t in recent)
        if use_route:
            conn_tools = conn_tools + connectors.ROUTE_TOOLS
        use_file = any(FILE_HINT_RE.search(t or "") for t in recent)
        use_site = getattr(self, "channel", "") == "desktop" and any(SITE_HINT_RE.search(t or "") for t in recent)
        tools = (TOOLS + (BITRIX_TOOLS if use_bx else []) + ([PC_TOOL] if use_pc else [])
                 + ([FILE_TOOL] if use_file else []) + ([SITE_TOOL] if use_site else [])
                 + ([BOILER_TOOL] if use_boiler else []) + conn_tools)
        prompt = system_prompt(self.ideas.projects(), use_bx, use_pc, use_boiler)
        if use_route:
            prompt += connectors.ROUTE_RULES
        if conn_names:
            prompt += ("\n- Подключено: " + ", ".join(conn_names) + ". Для событий, писем, умного дома и расписаний "
                       "вызывай соответствующие функции, не выдумывай. Даты считай от сегодняшней.")
        quote = getattr(self, "reply_quote", "")
        if quote:                                        # «ответить» на конкретное сообщение Троя
            head = f"[Это ответ на твоё сообщение: «{quote[:1500]}»]\n"
            content = head + content if isinstance(content, str) else \
                [{"type": "text", "text": head}] + content
        messages = ([{"role": "system", "content": prompt}]
                    + history + [{"role": "user", "content": content}])
        has_image = isinstance(content, list)
        must_pc = (use_pc and bool(PC_ORDER_RE.search(self.user_text or "")) and not HOME_RE.search(self.user_text or "")
                   and not SITE_HINT_RE.search(self.user_text or ""))
        nudged = False
        skip: set = set()
        provider, model = None, None
        last_err: Optional[LLMError] = None
        for _ in range(12):
            cands = self.router.candidates(has_image, skip)
            if not cands:
                if last_err and last_err.status != 429:
                    raise last_err
                raise LLMError(429, f"все модели на перерыве, ~{max(1, self.router.wait_hint() // 60)} мин")
            if (provider, model) != cands[0]:
                if model is not None:
                    log.info("Переключаюсь на %s", cands[0][1])
                    messages = self.flatten_tools(messages)
                provider, model = cands[0]
            send = messages if model in VISION_MODELS else self.strip_images(messages)
            send = [{**send[0], "content": send[0]["content"] + self.router.describe(provider, model)}] + send[1:]
            try:
                msg = provider.chat(model, send, tools)
                self.router.last = (getattr(provider, "NAME", ""), model, time.time())
            except LLMError as e:
                if e.status in (400, 404, 413, 422, 429, 500, 502, 503, 529) or (
                        e.status in (401, 402, 403) and provider is self.router.custom):
                    self.router.penalize(model, e)
                    skip.add(model)          # в этом сообщении к ней больше не возвращаемся
                    last_err = e
                    log.warning("%s", e)
                    continue
                raise
            except requests.RequestException as e:       # до модели не достучались (прокси, DNS, тайм-аут) — следующая
                self.router.cool[model] = time.time() + 60
                skip.add(model)
                last_err = LLMError(503, human_error(e), getattr(provider, "NAME", "Groq"))
                log.warning("%s: %s", model, human_error(e))
                continue
            calls = msg.get("tool_calls") or []
            if (not calls and not self.pc_used and not nudged
                    and (must_pc or "передал команду" in (msg.get("content") or ""))):
                nudged = True
                log.info("Модель ответила без pc_command — прошу вызвать функцию")
                messages.append({"role": "assistant", "content": msg.get("content") or ""})
                messages.append({"role": "user", "content":
                                 "Команда НЕ выполнена: ты не вызвал функцию pc_command. Слова не управляют компьютером. "
                                 "Вызови pc_command сейчас (без поля device, если я не назвал компьютер)."})
                continue
            if not calls and "передал команду" in (msg.get("content") or "") and not self.pc_used:
                return "Не понял, что сделать на компьютере. Скажи прямо, например: «открой браузер»."
            if not calls:
                return clean_answer(msg.get("content")) or "Модель вернула пустой ответ. Попробуй переформулировать."
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}") or {}
                except ValueError:
                    args = {}
                log.info("Функция: %s %s", name, json.dumps(args, ensure_ascii=False)[:200])
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": self.execute(chat_id, name, args)})
        return "Запутался в шагах — попробуй сформулировать иначе."


# ───────────────────────── Бот ─────────────────────────

HELP = """Я {name}, твой помощник. Пиши текстом, голосом, присылай фото или PDF.

Например:
- «напомни завтра в 9 позвонить в управляйку»
- «каждый понедельник в 10 напоминай про отчёт»
- «идея для котла: показывать температуру на главном экране»
- «где купить термопасту и почём»
- фото ценника, квитанции, схемы — с вопросом в подписи
- «что у меня по задачам в битриксе», «сколько сделок закрыли в октябре по менеджерам»

Команды:
/reminders — мои напоминания
/ideas — проекты с идеями, /ideas имя — идеи проекта
/new — начать разговор с чистого листа
/log — сохранить переписку файлом: /log, /log сегодня, /log неделя
/password новый_пароль — пароль для программы на компьютере
/devices — где открыта программа; /devices выйти — выйти везде
/status — какие модели сейчас работают, какие на перерыве
/apps слово — какие программы с этим словом Трой видит на компьютере
/connect — код, чтобы подключить ещё один компьютер
/diag — диагностика: что работает, что нет и почему
/bug текст — сообщить об ошибке разработчикам (готовый отчёт на GitHub)"""


class Bot:
    def __init__(self, tg: Telegram, assistant: Optional["Assistant"] = None):
        self.tg = tg
        self.assistant = assistant or Assistant()

    def command(self, chat_id: str, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/start", "/help"):
            self.tg.send(chat_id, HELP.format(name=BOT_NAME))
        elif cmd == "/log":
            self.tg.send(chat_id, log_request(self.assistant, chat_id, "сохрани переписку " + arg))
            export = getattr(self.assistant, "export", None)
            if export:
                self.assistant.export = None
                self.send_file(chat_id, *export)
        elif cmd == "/new":
            clear_history(chat_id)
            self.tg.send(chat_id, "Начали с чистого листа. Напоминания и идеи на месте.")
        elif cmd == "/password":
            self.set_app_password(chat_id, arg.strip())
        elif cmd == "/connect":
            self.tg.send(chat_id, connect_text())
        elif cmd in ("/bug", "/ошибка"):
            self.tg.typing(chat_id)
            self.tg.send(chat_id, bug_text(arg, self.assistant))
        elif cmd == "/diag":
            self.tg.typing(chat_id)
            self.tg.send(chat_id, diagnostics(self.assistant))
        elif cmd == "/devices":
            if arg.strip().lower() in ("выйти", "logout", "выход"):
                n = AppAuth.logout_all()
                self.tg.send(chat_id, f"Вышел на всех компьютерах ({n}). Там снова спросят пароль.")
                return
            rows = AppAuth.devices()
            self.tg.send(chat_id, "Программа открыта:\n" + "\n".join(
                f"- {r['device']} ({r['ip']}), вход {datetime.fromtimestamp(r['created'], TZ):%d.%m %H:%M}"
                for r in rows) if rows else "Программа сейчас нигде не авторизована.")
        elif cmd == "/status":
            self.tg.send(chat_id, self.assistant.router.status())
        elif cmd == "/apps":
            if not arg.strip():
                self.tg.send(chat_id, "Напиши так: /apps яндекс — покажу, какие программы с этим словом видит Трой на компьютере.")
                return
            text, choice = pc_create("find_app", arg.strip(), "", "telegram", chat_id)
            if choice:
                self.tg.send(chat_id, f"На каком компьютере искать «{arg.strip()}»?", buttons=[
                    [{"text": f"🖥 {d}", "callback_data": f"pc:{choice['id']}:{i}"}] for i, d in enumerate(choice["devices"])])
            else:
                self.tg.send(chat_id, text.replace("Команда «найти программу", "Ищу «").split(" отправлена")[0] + "…")
        elif cmd == "/reminders":
            self.tg.send(chat_id, list_reminders(chat_id))
        elif cmd == "/ideas":
            if arg.strip():
                self.tg.send(chat_id, self.assistant.ideas.read(arg))
            else:
                self.assistant.ideas._projects_at = 0
                pr = self.assistant.ideas.projects()
                self.tg.send(chat_id, "Проекты: " + ", ".join(pr) if pr else "Идей пока нет.")
        else:
            self.tg.send(chat_id, "Не знаю такой команды. /help — что я умею.")

    def set_app_password(self, chat_id: str, password: str) -> None:
        if not password:
            self.tg.send(chat_id, "Напиши так: /password новый_пароль\n"
                                  "Не короче 6 символов, без пробелов. Сообщение с паролем я сразу удалю.")
            return
        if len(password) < 6 or " " in password:
            self.tg.send(chat_id, "Пароль должен быть не короче 6 символов и без пробелов. Попробуй ещё раз.")
            return
        n = AppAuth.set_password(password)
        self.tg.send(chat_id, "🔑 Пароль для программы на компьютере установлен. Сообщение с ним удалил."
                     + (f"\nВышел на всех компьютерах ({n}) — там спросят новый пароль." if n else ""))

    def forget_message(self, msg: dict) -> None:
        """Сообщение с паролем убираем из чата."""
        try:
            self.tg.call("deleteMessage", data={"chat_id": msg["chat"]["id"], "message_id": msg["message_id"]})
        except Exception as e:
            log.warning("Не удалил сообщение с паролем: %s", e)
            try:
                self.tg.send(str(msg["chat"]["id"]), "Не смог удалить сообщение с паролем — удали его сам.")
            except Exception:
                pass

    def fetch(self, obj: dict) -> bytes:
        if obj.get("file_size", 0) > MAX_FILE_MB * 1024 * 1024:
            raise ValueError("too_big")
        return self.tg.download(obj["file_id"])

    def build_content(self, chat_id: str, msg: dict):
        """Готовит сообщение для модели. Возвращает (content, заметка для истории) или (None, None)."""
        text = (msg.get("text") or msg.get("caption") or "").strip()
        texts, note, image = [], [], None

        voice = msg.get("voice") or msg.get("audio") or msg.get("video_note")
        if voice:
            name = voice.get("file_name") or ("note.mp4" if "video_note" in msg else "voice.ogg")
            spoken = self.assistant.llm.transcribe(self.fetch(voice), name)
            if not spoken:
                self.tg.send(chat_id, "Не расслышал, повтори, пожалуйста.")
                return None, None
            texts.append(f"[голосовое] {spoken}")
            note.append(f"[голосовое] {spoken}")

        if msg.get("photo"):
            image = ("image/jpeg", self.fetch(msg["photo"][-1]))
            note.append("[фото]")

        doc = msg.get("document")
        if doc:
            mime = doc.get("mime_type") or ""
            fname = doc.get("file_name") or "файл"
            if mime.startswith("image/"):
                image = (mime, self.fetch(doc))
                note.append(f"[картинка {fname}]")
            elif mime == "application/pdf":
                body = pdf_text(self.fetch(doc))
                if not body:
                    self.tg.send(chat_id, "В этом PDF нет текста — похоже, это скан. Пришли его как фото.")
                    return None, None
                texts.append(f"[PDF «{fname}»]\n{body}")
                note.append(f"[PDF {fname}]")
            else:
                self.tg.send(chat_id, "Из файлов понимаю картинки и PDF.")
                return None, None

        if text:
            texts.append(text)
            note.append(text)
        if not texts and not image:
            self.tg.send(chat_id, "Понимаю текст, голосовые, фото и PDF.")
            return None, None

        joined = "\n\n".join(texts)
        if not image:
            return joined, " ".join(note)
        data_url = f"data:{image[0]};base64,{base64.b64encode(image[1]).decode()}"
        content = [{"type": "text", "text": joined or "Что на картинке?"},
                   {"type": "image_url", "image_url": {"url": data_url}}]
        return content, " ".join(note)

    def handle(self, msg: dict) -> None:
        chat_id = str(msg["chat"]["id"])
        uid = str((msg.get("from") or {}).get("id", ""))
        if msg["chat"].get("type") != "private":
            return
        if not ALLOWED_USER_ID:
            self.tg.send(chat_id, f"Твой Telegram ID: {uid}\nВпиши в settings.env строку ALLOWED_USER_ID={uid} "
                                  f"и перезапусти бота. До этого я никому не отвечаю.")
            return
        if not tg_linked():                  # Telegram подключили позже: хозяин приходит по ссылке с кодом
            code = (msg.get("text") or "").strip()
            if TELEGRAM_LINK_CODE and code in (f"/start {TELEGRAM_LINK_CODE}", TELEGRAM_LINK_CODE):
                link_owner(uid)
                self.tg.send(chat_id, f"Привет{', ' + USER_NAME if USER_NAME else ''}! Это {BOT_NAME}. Теперь я на "
                                      "связи и здесь: та же переписка, напоминания и идеи, что в программе.")
            else:
                self.tg.send(chat_id, "Я пока ни к кому не привязан. Хозяин подключает меня в программе: "
                                      "🔌 «Подключения» → Telegram.")
            return
        if uid != ALLOWED_USER_ID:
            log.warning("Сообщение от чужого пользователя %s — игнорирую", uid)
            return
        m = PASSWORD_RE.match(msg.get("text") or "")
        if m:
            self.set_app_password(chat_id, m.group(1))
            self.forget_message(msg)
            return
        if (msg.get("text") or "").startswith("/"):
            if (msg.get("text") or "").lower().startswith("/password"):
                self.forget_message(msg)
            self.command(chat_id, msg["text"].strip())
            return

        note = "[сообщение]"
        rep_msg = msg.get("reply_to_message") or {}
        quote = (rep_msg.get("text") or rep_msg.get("caption") or "") if (rep_msg.get("from") or {}).get("is_bot") else ""
        threading.current_thread().troy_reply = quote
        with KeepTyping(self.tg, chat_id):
            try:
                content, note = self.build_content(chat_id, msg)
                if content is None:
                    return
                answer = self.assistant.run(chat_id, content)
            except ValueError as e:
                if str(e) != "too_big":
                    raise
                self.tg.send(chat_id, f"Файл больше {MAX_FILE_MB} МБ — Telegram не отдаёт ботам такие.")
                return
            except LLMError as e:
                log.error("%s", e)
                if e.status == 429:
                    answer = (f"Бесплатные лимиты моделей сейчас исчерпаны ({e.text}). Попробуй чуть позже."
                              if "все модели" in str(e) else "Упёрся в бесплатный лимит. Подожди минуту и повтори.")
                else:
                    answer = f"Groq вернул ошибку: {e}"
            except requests.RequestException as e:
                log.error("Сеть: %s", e)
                answer = "Не достучался до Groq, попробуй ещё раз через минуту."

        pc = getattr(self.assistant, "pc_used", False)    # команды компьютеру модели не показываем (см. get_history)
        if getattr(self.assistant, "secret_seen", False):  # пароль/ключ: в историю не пишем, из чата убираем
            self.assistant.secret_seen, note = False, "[сообщение с паролем или ключом — не сохранено]"
            self.forget_message(msg)
        if quote and isinstance(note, str):
            note = f"↩ «{re.sub(chr(10), ' ', quote)[:80]}» {note}"
        threading.current_thread().troy_reply = ""
        add_history(chat_id, "pc_user" if pc else "user", note)
        add_history(chat_id, "pc_reply" if pc else "assistant", answer)
        self.tg.send(chat_id, answer)
        export = getattr(self.assistant, "export", None)
        if export:
            self.assistant.export = None
            self.send_file(chat_id, *export)
        choice = self.assistant.pc_choice
        if choice:
            self.tg.send(chat_id, f"Где выполнить «{choice['label']}»?", buttons=[
                [{"text": f"🖥 {d}", "callback_data": f"pc:{choice['id']}:{i}"}] for i, d in enumerate(choice["devices"])])
            self.assistant.pc_choice = None

    def send_file(self, chat_id: str, name: str, data: bytes) -> None:
        try:
            self.tg.call("sendDocument", data={"chat_id": chat_id}, files={"document": (name, data, "text/plain")})
        except Exception as e:
            log.warning("Не отправил файл: %s", e)
            self.tg.send(chat_id, f"Не смог отправить файл: {human_error(e) if isinstance(e, requests.RequestException) else e}")

    def handle_callback(self, cb: dict) -> None:
        uid = str((cb.get("from") or {}).get("id", ""))
        data = cb.get("data") or ""
        msg = cb.get("message") or {}
        reply = "Неизвестная кнопка"
        if uid != ALLOWED_USER_ID:
            reply = "Нет доступа"
        elif data.startswith("pc:"):
            try:
                _, cid, idx = data.split(":")
                reply = pc_choose(int(cid), int(idx))
            except ValueError:
                reply = "Неверная кнопка"
            try:   # кнопки убираем, на их месте — что выбрано
                self.tg.call("editMessageText", data={"chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                                                      "text": reply})
            except Exception:
                pass
        try:
            self.tg.call("answerCallbackQuery", data={"callback_query_id": cb["id"], "text": reply[:180]})
        except Exception:
            pass

    def poll_forever(self, stop_when: Callable[[], bool] = lambda: False) -> None:
        offset = None
        while not stop_when():
            try:
                updates = self.tg.call("getUpdates", timeout=40, data={
                    "timeout": 30, "offset": offset, "allowed_updates": json.dumps(["message", "callback_query"])})
            except (requests.RequestException, TelegramError) as e:
                log.warning("Telegram: %s", human_error(e))
                TG_STATE.update(ok=False, error=human_error(e), at=time.time())
                time.sleep(5)
                continue
            TG_STATE.update(ok=True, error="", at=time.time())
            for u in updates:
                offset = u["update_id"] + 1
                if u.get("callback_query"):
                    try:
                        self.handle_callback(u["callback_query"])
                    except Exception:
                        log.exception("Ошибка обработки кнопки")
                    continue
                msg = u.get("message")
                if not msg:
                    continue
                try:
                    self.handle(msg)
                except Exception as e:
                    log.exception("Ошибка при обработке сообщения")
                    try:
                        self.tg.send(str(msg["chat"]["id"]), f"Что-то сломалось: {redact(str(e))}")
                    except Exception:
                        pass


# ───────────────────────── Программа на компьютере ─────────────────────────

def _ok(flag) -> str:
    return "✅" if flag is True else ("⚪" if flag is None else "❌")


def diagnostics(assistant: Optional["Assistant"] = None) -> str:
    """/diag — что работает, что нет и почему. Без ключей и паролей в ответе."""
    up = int(time.time() - STARTED)
    lines = [f"🩺 Диагностика Троя — {'на этом компьютере' if LOCAL_MODE else 'на сервере'}, "
             f"работает {up // 3600} ч {up % 3600 // 60} мин"]
    # Telegram
    if not TELEGRAM_TOKEN:
        lines.append("⚪ Telegram: не подключён (необязательно — кнопка 🔌 «Подключения» в программе)")
    elif not tg_linked():
        lines.append("⚪ Telegram: бот подключён, но хозяин ещё не нажал «Запустить» по ссылке из «Подключений»")
    elif TG_STATE["ok"] is None:
        lines.append("⚪ Telegram: ещё не подключался")
    elif TG_STATE["ok"]:
        lines.append("✅ Telegram: на связи")
    else:
        lines.append(f"❌ Telegram: {TG_STATE['error']}. Если Telegram заблокирован — нужен прокси "
                     "(TELEGRAM_PROXY в настройках) или VPN.")
    # Groq
    try:
        have = set(Groq().models())
        missing = [m for m in GROQ_MODELS if m not in have]
        lines.append("✅ Groq: ключ работает" + (f"; нет моделей: {', '.join(missing)}" if missing else ""))
    except LLMError as e:
        lines.append(f"❌ Groq: {'ключ не принят — проверь GROQ_API_KEY' if e.status == 401 else e}")
    except requests.RequestException as e:
        lines.append(f"❌ Groq: {human_error(e)}")
    if assistant is not None:
        lines.append("   " + assistant.router.status().split("\n\n")[0].replace("\n", "\n   "))
    # Mistral
    if MISTRAL_API_KEY:
        try:
            r = requests.get("https://api.mistral.ai/v1/models", headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"},
                             timeout=15)
            lines.append(f"{_ok(r.status_code == 200)} Mistral: " +
                         ("ключ работает" if r.status_code == 200 else f"ошибка {r.status_code} — проверь MISTRAL_API_KEY"))
        except requests.RequestException as e:
            lines.append(f"❌ Mistral: {human_error(e)}")
    else:
        lines.append("⚪ Mistral: не подключён (необязательно)")
    # Битрикс
    if BITRIX_WEBHOOK:
        try:
            r = requests.get(BITRIX_WEBHOOK.rstrip("/") + "/profile.json", timeout=15)
            okb = r.status_code == 200 and "result" in r.json()
            lines.append(f"{_ok(okb)} Битрикс24: " + ("вебхук работает" if okb else
                         f"ошибка {r.status_code} — проверь вебхук и его права"))
        except (requests.RequestException, ValueError) as e:
            lines.append(f"❌ Битрикс24: {human_error(e)}")
    # идеи
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}",
                             headers={"Authorization": f"Bearer {GITHUB_TOKEN}"}, timeout=15)
            lines.append(f"{_ok(r.status_code == 200)} Идеи в GitHub: " + (GITHUB_REPO if r.status_code == 200 else
                         f"ошибка {r.status_code} — проверь GITHUB_TOKEN и GITHUB_REPO"))
        except requests.RequestException as e:
            lines.append(f"❌ Идеи в GitHub: {human_error(e)}")
    else:
        lines.append("✅ Идеи: хранятся " + ("на этом компьютере" if LOCAL_MODE else "на сервере"))
    # база и напоминания
    try:
        with closing(db()) as con:
            n = con.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        size = DB_FILE.stat().st_size // 1024
        lines.append(f"✅ База: в порядке ({size} КБ), напоминаний: {n}, часовой пояс {TZ_NAME}")
    except Exception as e:
        lines.append(f"❌ База: {e}")
    if not ALLOWED_USER_ID:
        lines.append("❌ Хозяин не указан (ALLOWED_USER_ID) — Трой никому не отвечает")
    # подключения
    if assistant is not None:
        lines += assistant.conns.diag() or ["⚪ Подключений нет (календари, почта, умный дом — в программе: «Подключения»)"]
    # компьютеры
    lines.append(pcs_status())
    # последние проблемы
    recent = [x for x in RECENT.items if time.time() - x[0] < 24 * 3600][-6:]
    if recent:
        lines.append("Последние проблемы:")
        lines += [f"- {datetime.fromtimestamp(t, TZ):%d.%m %H:%M} {msg_}" for t, _lvl, msg_ in recent]
    else:
        lines.append("Проблем за последние сутки не было.")
    return "\n".join(lines)


BUG_REPO = "https://github.com/estark407-max/SomeTroy"


def scrub(text: str) -> str:
    """Для отчёта об ошибке: без ключей, адресов серверов и почты."""
    text = redact(text)
    text = re.sub(r"\b(y0_)[\w-]{10,}", r"\1•••", text)
    text = re.sub(r"troy://\S+", "troy://•••", text)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "•.•.•.•", text)
    return re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "•••@•••", text)


def bug_text(what: str, assistant: Optional["Assistant"] = None) -> str:
    """/bug в Telegram: ссылка на новую задачу в GitHub с описанием и короткой диагностикой."""
    from urllib.parse import quote
    what = (what or "").strip()
    if not what:
        return ("Напиши так: /bug что случилось — например «/bug не ставятся напоминания». Я соберу отчёт с "
                "диагностикой и дам ссылку на GitHub: проверишь и отправишь (нужен бесплатный аккаунт GitHub).")
    title = "Ошибка: " + re.sub(r"\s+", " ", what)[:80]
    diag = scrub(diagnostics(assistant))
    head = f"**Что случилось**\n{what}\n\n**Откуда:** Telegram, Трой {'на компьютере' if LOCAL_MODE else 'на сервере'}\n\n"
    url = lambda d: (f"{BUG_REPO}/issues/new?labels=bug&title={quote(title)}&body="
                     + quote(head + "<details><summary>Диагностика</summary>\n\n```\n" + d + "\n```\n</details>\n"))
    while len(url(diag)) > 3500 and len(diag) > 100:      # длинные ссылки Telegram режет — диагностику укорачиваем
        diag = diag[: int(len(diag) * 0.8)] + "\n…"
    return ("🐞 Отчёт готов. Открой ссылку, проверь текст и нажми «Submit new issue» (нужен бесплатный аккаунт GitHub). "
            "Ключи, пароли и адреса серверов я из отчёта убрал. Полная диагностика — в программе: /bug там же.\n\n" + url(diag))


def connect_code() -> Optional[str]:
    """troy://IP:ПОРТ#отпечаток — адрес сервера и отпечаток его сертификата (без паролей).
    Программа на новом компьютере скачает сертификат и сверит отпечаток: подменить сервер нельзя."""
    import ssl
    cert = TLS_DIR / "cert.pem"
    if LOCAL_MODE or not cert.exists():
        return None
    pem = cert.read_text()
    fp = base64.urlsafe_b64encode(hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).digest()).decode().rstrip("=")
    try:
        info = ssl._ssl._test_decode_cert(str(cert))              # IP сервера записан в сертификате
        ip = next(v for k, v in info.get("subjectAltName", ()) if k == "IP Address")
    except Exception:
        return None
    return f"troy://{ip}:{DESKTOP_PORT}#{fp}"


def connect_text() -> str:
    code = connect_code()
    if not code:
        return ("Я живу на компьютере (локальный режим) — к такому Трою другие компьютеры не подключаются.")
    return ("Чтобы подключить ещё один компьютер: поставь на нём программу Трой, при первом запуске нажми "
            "«У меня уже есть Трой» и вставь этот код:\n\n" + code +
            "\n\nВ коде нет паролей — войти всё равно можно только с паролем программы.")


def desktop_command(assistant: "Assistant", text: str) -> Optional[str]:
    """Команды в программе на компьютере — те же, что в Telegram (кроме пароля)."""
    cmd, _, arg = text.strip().partition(" ")
    cmd = cmd.lower()
    if cmd == "/status":
        return assistant.router.status()
    if cmd == "/apps":
        if not arg.strip():
            return "Напиши так: /apps яндекс — покажу, какие программы с этим словом видит Трой на компьютере."
        text, choice = pc_create("find_app", arg.strip(), "", "desktop", ALLOWED_USER_ID)
        return {"reply": "Выбери компьютер.", "choice": choice} if choice else "Ищу… ответ придёт отдельным сообщением."
    if cmd == "/log":
        reply = log_request(assistant, ALLOWED_USER_ID, "сохрани переписку " + arg)
        export, assistant.export = getattr(assistant, "export", None), None
        return {"reply": reply, "download": {"name": export[0], "b64": base64.b64encode(export[1]).decode()}} \
            if export else reply
    if cmd == "/reminders":
        return list_reminders(ALLOWED_USER_ID)
    if cmd == "/new":
        clear_history(ALLOWED_USER_ID)
        return "Начали с чистого листа. Напоминания и идеи на месте."
    if cmd == "/ideas":
        if arg.strip():
            return assistant.ideas.read(arg)
        assistant.ideas._projects_at = 0
        pr = assistant.ideas.projects()
        return "Проекты: " + ", ".join(pr) if pr else "Идей пока нет."
    if cmd == "/devices":
        rows = AppAuth.devices()
        return "Программа открыта:\n" + "\n".join(
            f"- {r['device']} ({r['ip']}), вход {datetime.fromtimestamp(r['created'], TZ):%d.%m %H:%M}" for r in rows)
    if cmd == "/password":
        if tg_linked():
            return "Пароль меняется только в Telegram: /password новый_пароль"
        pw = arg.strip()                      # Telegram не подключён — меняем здесь (вход уже по паролю)
        if len(pw) < 6 or " " in pw:
            return "Напиши так: /password новый_пароль — не короче 6 символов, без пробелов."
        AppAuth.set_password(pw)
        return "🔑 Пароль сменён. Программа попросит войти с новым паролем."
    if cmd == "/connect":
        return connect_text()
    if cmd == "/diag":
        return diagnostics(assistant)
    if cmd in ("/help", "/start"):
        return HELP.format(name=BOT_NAME)
    return None


def desktop_message(assistant: "Assistant", text: str, files: Optional[list]) -> str:
    """Сообщение из программы на компьютере: тот же разговор, что и в Telegram. До 3 файлов за раз."""
    chat_id = ALLOWED_USER_ID
    if text.strip().startswith("/") and not files:
        answer = desktop_command(assistant, text)
        if answer is not None:
            return answer
    texts, note, images = [], [], []
    for f in (files or [])[:3]:
        if not f or not f.get("data"):
            continue
        raw = base64.b64decode(f["data"])
        mime, name = f.get("mime") or "", f.get("name") or "файл"
        if mime.startswith("image/"):
            images.append((mime, raw))
            note.append(f"[картинка {name}]")
        elif mime == "application/pdf" or name.lower().endswith(".pdf"):
            body = pdf_text(raw)
            if not body:
                return f"В «{name}» нет текста — похоже, это скан. Пришли его как картинку."
            texts.append(f"[PDF «{name}»]\n{body}")
            note.append(f"[PDF {name}]")
        elif mime.startswith("audio/") or mime.startswith("video/"):
            spoken = assistant.llm.transcribe(raw, name)
            if not spoken:
                return "Не расслышал, повтори, пожалуйста."
            texts.append(f"[голосовое] {spoken}")
            note.append(f"[голосовое] {spoken}")
        else:
            return f"«{name}»: из файлов понимаю картинки, PDF и аудио."
    if text.strip():
        texts.append(text.strip())
        note.append(text.strip())
    if not texts and not images:
        return "Пустое сообщение."
    joined = "\n\n".join(texts)
    content = joined
    if images:
        content = [{"type": "text", "text": joined or "Что на картинках?"}] + [
            {"type": "image_url", "image_url": {"url": f"data:{m};base64,{base64.b64encode(r).decode()}"}}
            for m, r in images]
    choice = export = None
    try:
        answer = assistant.run(chat_id, content, channel="desktop")
        choice, assistant.pc_choice = assistant.pc_choice, None
        export, assistant.export = getattr(assistant, "export", None), None
    except LLMError as e:
        answer = (f"Бесплатные лимиты моделей сейчас исчерпаны ({e.text}). Попробуй чуть позже." if e.status == 429
                  else f"Ошибка модели: {e}")
    pc = getattr(assistant, "pc_used", False)
    quote = getattr(assistant, "reply_quote", "")
    if quote:
        note = [f"↩ «{re.sub(chr(10), ' ', quote)[:80]}»"] + list(note)
    if getattr(assistant, "secret_seen", False):
        assistant.secret_seen, note = False, ["[сообщение с паролем или ключом — не сохранено]"]
    add_history(chat_id, "pc_user" if pc else "user", " ".join(note))
    add_history(chat_id, "pc_reply" if pc else "assistant", answer)
    if export:
        return {"reply": answer, "download": {"name": export[0], "b64": base64.b64encode(export[1]).decode()}}
    return {"reply": answer, "choice": choice} if choice else answer


def desktop_history(after: int = 0) -> list:
    """Переписка для окна программы. after — только новее этого номера (так окна на всех компьютерах
    видят сообщения, отправленные с другого компьютера или из Telegram)."""
    with closing(db()) as con:
        rows = con.execute("SELECT id, role, text, ts FROM history WHERE chat_id=? AND id>? AND role != 'pc_result' "
                           "ORDER BY id DESC LIMIT 60",
                           (ALLOWED_USER_ID, int(after or 0))).fetchall()
    return [{"id": r["id"], "role": "user" if r["role"] in ("user", "pc_user") else "assistant", "text": r["text"],
             "ts": r["ts"]} for r in reversed(rows)]


def last_history_id() -> int:
    with closing(db()) as con:
        row = con.execute("SELECT MAX(id) FROM history WHERE chat_id=?", (ALLOWED_USER_ID,)).fetchone()
    return int(row[0] or 0)


def desktop_reply(assistant: "Assistant", text: str, files: Optional[list]) -> dict:
    """Ответ окну + номер последней записи истории: своё сообщение окно уже нарисовало, второй раз не покажет."""
    res = desktop_message(assistant, text, files)
    res = res if isinstance(res, dict) else {"reply": res}
    res["hist"] = last_history_id()
    return res


def notify_owner(text: str) -> None:
    if not tg_linked():
        push_event(text)
        return
    try:
        Telegram(TELEGRAM_TOKEN).send(ALLOWED_USER_ID, text)
    except Exception as e:
        log.warning("Не отправил уведомление: %s", e)


# ───────────────────────── Модули, которые можно подключить позже ─────────────────────────
# Telegram, Mistral и Битрикс24 включаются из программы (🔌 «Подключения») без перезапуска:
# значение проверяется у сервиса, пишется в settings.env и сразу начинает работать.

def tg_linked() -> bool:
    """Telegram подключён и хозяин известен (ALLOWED_USER_ID — числовой id, а не «local»)."""
    return bool(TELEGRAM_TOKEN) and bool(re.fullmatch(r"\d+", ALLOWED_USER_ID or ""))


SETTINGS_LOCK = threading.Lock()


def save_setting(key: str, value: str) -> None:
    """Меняет одну строку settings.env (пустое значение — убирает её) и значение в работающем Трое."""
    path = BASE / "settings.env"
    with SETTINGS_LOCK:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        out, done = [], False
        for line in lines:
            if re.match(rf"\s*{re.escape(key)}\s*=", line):
                if value and not done:
                    out.append(f"{key}={value}")
                done = True
                continue
            out.append(line)
        if value and not done:
            out.append(f"{key}={value}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    os.environ[key] = value
    globals()[key] = value


def kv_json(key: str) -> dict:
    with closing(db()) as con:
        row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    try:
        return json.loads(row[0]) if row else {}
    except ValueError:
        return {}


def kv_json_set(key: str, value: dict) -> None:
    with closing(db()) as con, con:
        con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))


def link_owner(uid: str) -> None:
    """Хозяин нажал «Запустить» по ссылке с кодом: переписка и напоминания программы переезжают на его id."""
    old = ALLOWED_USER_ID
    if old and old != uid:
        with closing(db()) as con, con:
            for table in ("history", "reminders", "pc_commands", "message_log"):
                con.execute(f"UPDATE {table} SET chat_id=? WHERE chat_id=?", (uid, old))
    save_setting("ALLOWED_USER_ID", uid)
    save_setting("TELEGRAM_LINK_CODE", "")
    log.info("Telegram: хозяин привязан")


def _tg_link_note() -> str:
    if not (TELEGRAM_TOKEN and TELEGRAM_LINK_CODE and not tg_linked()):
        return ""
    return (f"Последний шаг: открой https://t.me/{TELEGRAM_BOT}?start={TELEGRAM_LINK_CODE} и нажми «Запустить» — "
            "так я узнаю, что это ты, и буду отвечать только тебе.")


def _tg_connect(v: dict) -> tuple:
    token, proxy = v.get("token", ""), v.get("proxy", "")
    if not re.fullmatch(r"\d{5,}:[\w-]{30,}", token):
        return "Токен бота выглядит так: 123456789:AAH… — скопируй его из сообщения @BotFather целиком.", ""
    if proxy and not re.match(r"(socks5h?|https?)://\S+$", proxy):
        return "Адрес прокси выглядит так: socks5://адрес:порт или http://адрес:порт", ""
    try:
        r = make_session(proxy).get(f"https://api.telegram.org/bot{token}/getMe", timeout=20)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        return (f"Не достучался до Telegram: {human_error(e)}. Если Telegram открывается только через VPN — "
                "включи VPN или впиши прокси."), ""
    if not data.get("ok"):
        return "Telegram не принял токен. Проверь, что скопировал его целиком (/mybots → бот → API Token).", ""
    save_setting("TELEGRAM_PROXY", proxy)
    save_setting("TELEGRAM_BOT", data["result"]["username"])
    if not re.fullmatch(r"\d+", ALLOWED_USER_ID or ""):
        save_setting("TELEGRAM_LINK_CODE", secrets.token_hex(8))
    save_setting("TELEGRAM_TOKEN", token)         # последним: на него смотрит цикл Telegram
    return None, _tg_link_note() or (f"Готово: теперь я @{TELEGRAM_BOT}. Открой https://t.me/{TELEGRAM_BOT} "
                                     "и нажми «Запустить», чтобы я мог тебе писать.")


def _tg_disconnect() -> None:
    save_setting("TELEGRAM_TOKEN", "")
    save_setting("TELEGRAM_LINK_CODE", "")


def _mistral_connect(assistant: "Assistant", v: dict) -> tuple:
    key = v.get("key", "")
    try:
        r = requests.get("https://api.mistral.ai/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    except requests.RequestException as e:
        return f"Не достучался до Mistral: {human_error(e)}", ""
    if r.status_code in (401, 403):
        return "Mistral не принял ключ. Скопируй его заново на https://console.mistral.ai/api-keys", ""
    if r.status_code != 200:
        return f"Mistral ответил ошибкой {r.status_code}. Попробуй через минуту.", ""
    save_setting("MISTRAL_API_KEY", key)
    assistant.router.mistral = Mistral()
    return None, ""


def _llm_connect(assistant: "Assistant", v: dict) -> tuple:
    url, key, model = v.get("url", "").strip(), v.get("key", "").strip(), v.get("model", "").strip()
    proxy = v.get("proxy", "").strip()
    if not re.match(r"https?://\S+", url):
        return "Выбери сервис или впиши адрес API (начинается с https://).", ""
    if not key or not model:
        return "Нужны ключ и название модели.", ""
    global LLM_PROXY
    old_proxy, LLM_PROXY = LLM_PROXY, proxy
    try:
        test = CustomLLM(url, key)
        reply = clean_answer(test.chat(model, [{"role": "user", "content": "Ответь одним словом: работаю"}]).get("content"))
    except LLMError as e:
        LLM_PROXY = old_proxy
        if e.status in (401, 403):
            return f"{test.NAME} не принял ключ ({e.status}). Скопируй ключ заново из кабинета сервиса.", ""
        if e.status == 402:
            return f"{test.NAME}: на счёте нет денег (402). Пополни баланс в кабинете сервиса.", ""
        if e.status in (400, 404):
            return f"{test.NAME} не знает модель «{model}». Скопируй её название из кабинета сервиса. ({(e.text or '')[:150]})", ""
        return f"{test.NAME} ответил ошибкой {e.status}: {(e.text or '')[:150]}", ""
    except requests.RequestException as e:
        LLM_PROXY = old_proxy
        return f"Не достучался до {url}: {human_error(e)}. Если сервис закрыт для твоей страны — укажи прокси.", ""
    save_setting("LLM_API_URL", url)
    save_setting("LLM_API_KEY", key)
    save_setting("LLM_MODEL", model)
    save_setting("LLM_PROXY", proxy)
    assistant.router.custom = make_custom_llm()
    assistant.router.cool.pop(model, None)        # если была «на перерыве» из-за ключа или баланса — снимаем
    assistant.router.errors.pop(model, None)
    log.info("Своя модель подключена: %s %s (ответ: %s)", test.NAME, model, reply[:30])
    return None, ""


def _llm_disconnect(assistant: "Assistant") -> None:
    for k in ("LLM_API_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_PROXY"):
        save_setting(k, "")
    assistant.router.custom = None


def _llm_state() -> tuple:
    if not (LLM_API_URL and LLM_API_KEY and LLM_MODEL):
        return False, "", ""
    host = re.sub(r"^https?://", "", LLM_API_URL).split("/")[0]
    name = next((v[0] for k, v in LLM_PRESETS.items() if host.endswith(k)), host)
    return True, f"{name}: {LLM_MODEL}", ""


def _mistral_disconnect(assistant: "Assistant") -> None:
    save_setting("MISTRAL_API_KEY", "")
    assistant.router.mistral = None


def _bitrix_connect(assistant: "Assistant", v: dict) -> tuple:
    url = v.get("url", "").strip()
    if not re.match(r"https://\S+/rest/\d+/\S+", url):
        return "Адрес вебхука выглядит так: https://портал.bitrix24.ru/rest/1/abc123…/ — скопируй его целиком.", ""
    url = url.rstrip("/") + "/"
    try:
        r = requests.get(url + "profile.json", timeout=20)
        ok = r.status_code == 200 and "result" in r.json()
    except (requests.RequestException, ValueError) as e:
        return f"Не достучался до портала: {human_error(e)}", ""
    if not ok:
        return "Битрикс24 не принял вебхук. Проверь адрес и права (task, im, crm, user).", ""
    save_setting("BITRIX_WEBHOOK", url)
    assistant.bx = bitrix.Bitrix(url, tz=TZ)
    return None, ""


def _bitrix_disconnect(assistant: "Assistant") -> None:
    save_setting("BITRIX_WEBHOOK", "")
    assistant.bx = None


def _gh_connect(assistant: "Assistant", v: dict) -> tuple:
    """Идеи — в свой репозиторий GitHub: проверяем доступ, переносим уже записанное, кладём README."""
    m = re.search(r"(?:github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", v.get("repo", "").strip())
    token = v.get("token", "").strip()
    if not m:
        return "Репозиторий пишется так: имя/репозиторий — например ivan/troy-ideas (или ссылкой на GitHub).", ""
    repo = f"{m.group(1)}/{m.group(2)}"
    s = make_session()
    s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                      "X-GitHub-Api-Version": "2022-11-28"})
    try:
        r = s.get(f"https://api.github.com/repos/{repo}", timeout=20)
    except requests.RequestException as e:
        return f"Не достучался до GitHub: {human_error(e)}", ""
    if r.status_code == 401:
        return "GitHub не принял токен. Создай новый по инструкции и скопируй его целиком (github_pat_…).", ""
    if r.status_code == 404:
        return (f"Не вижу репозиторий {repo}: проверь имя или дай токену доступ к нему "
                "(Repository access → Only select repositories → выбери репозиторий)."), ""
    if r.status_code != 200:
        return f"GitHub ответил ошибкой {r.status_code}. Попробуй через минуту.", ""
    info = r.json()
    if not (info.get("permissions") or {}).get("push", False):
        return "У токена нет права записи. В настройках токена: Permissions → Contents → Read and write.", ""
    save_setting("GITHUB_REPO", repo)
    save_setting("GITHUB_BRANCH", info.get("default_branch") or "main")
    save_setting("GITHUB_TOKEN", token)
    local = Ideas.__new__(Ideas)                      # что уже записано на этом Трое — переносим
    local.github, local.s, local._projects, local._projects_at = False, make_session(), [], 0.0
    assistant.ideas = Ideas()
    moved = 0
    d = LOCAL_IDEAS_DIR / "projects"
    for f in sorted(d.glob("*.md")) if d.exists() else []:
        content, _ = local._get(f"projects/{f.name}")
        for line in (content or "").splitlines():
            mm = re.match(r"- \d{4}-\d{2}-\d{2} \d{2}:\d{2} — (.*)", line)
            if mm:
                remote, sha = assistant.ideas._get(f"projects/{f.name}")
                if remote is None:
                    remote = (content.splitlines()[0] if content.startswith("#") else f"# {f.stem}") + "\n\n"
                if not idea_duplicate(remote, mm.group(1)):
                    assistant.ideas._put(f"projects/{f.name}", remote.rstrip("\n") + "\n" + line + "\n", sha,
                                         f"Идея: {f.stem} (перенос)")
                    moved += 1
    readme, _ = assistant.ideas._get("README.md")
    if readme is None or len(readme.strip()) < 40:
        assistant.ideas._put("README.md", IDEAS_README, None if readme is None else assistant.ideas._get("README.md")[1],
                             "Идеи Троя: README")
    return None, (f"Готово: идеи теперь пишутся в {repo}." + (f" Перенёс уже записанные: {moved}." if moved else "")
                  + " Чтобы обсудить их с ИИ, дай ему доступ к этому репозиторию и попроси «подтяни идеи».")


def _gh_disconnect(assistant: "Assistant") -> None:
    for k in ("GITHUB_TOKEN", "GITHUB_REPO", "GITHUB_BRANCH"):
        save_setting(k, "")
    assistant.ideas = Ideas()


IDEAS_README = """# Идеи

Сюда мой ассистент **Трой** ([SomeTroy](https://github.com/estark407-max/SomeTroy)) записывает идеи по проектам —
из Telegram, из программы на компьютере, текстом или голосом.

- `projects/<проект>.md` — идеи одного проекта, по одной в строке: `- ГГГГ-ММ-ДД ЧЧ:ММ — текст`.
- `projects/inbox.md` — то, что пока не относится ни к какому проекту.

## Как обсудить идеи с ИИ

Дай ассистенту (Claude, ChatGPT и т. п.) доступ к этому репозиторию и попроси:
«Подтяни идеи из репозитория» или «что нового по проекту X». Новые записи — внизу каждого файла.
"""


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1) if m else ""


def register_modules(assistant: "Assistant") -> None:
    """Модули в окне «Подключения» — вверху списка, с той же формой и инструкцией."""
    assistant.conns.modules = {
        "telegram": {
            "title": "Telegram", "about": "Пиши Трою с телефона, получай напоминания где угодно.",
            "fields": [{"name": "token", "label": "Токен бота от @BotFather", "secret": True,
                        "placeholder": "123456789:AAH…"},
                       {"name": "proxy", "label": "Прокси (только если Telegram у тебя не открывается без него)",
                        "placeholder": "socks5://адрес:порт", "optional": True}],
            "state": lambda: (bool(TELEGRAM_TOKEN), f"@{TELEGRAM_BOT}" if TELEGRAM_BOT and TELEGRAM_TOKEN else "",
                              _tg_link_note()),
            "connect": _tg_connect, "disconnect": _tg_disconnect,
            "help": """
Трой будет отвечать и в Telegram — та же переписка, напоминания и идеи, что в программе.

1. Открой @BotFather: https://t.me/BotFather (официальный бот Telegram, с синей галочкой).
2. Нажми **Запустить** и отправь **/newbot**.
3. Придумай имя (например «Мой Трой») и адрес латиницей на **bot** в конце, например my_troy_2026_bot.
4. BotFather пришлёт токен вида **123456789:AAH…** — скопируй его целиком и вставь сюда.
5. Трой покажет ссылку — открой её и нажми **Запустить**. Так он узнает, что ты хозяин, и будет отвечать только тебе.

Потерял токен — /mybots → выбери бота → **API Token**.
"""},
        "mistral": {
            "title": "Запасная модель Mistral",
            "about": "Когда у бесплатного Groq кончится дневной лимит, Трой ответит через Mistral, а не замолчит.",
            "fields": [{"name": "key", "label": "Ключ Mistral", "secret": True}],
            "state": lambda: (bool(MISTRAL_API_KEY), "", ""),
            "connect": lambda v: _mistral_connect(assistant, v),
            "disconnect": lambda: _mistral_disconnect(assistant),
            "help": """
1. Открой https://console.mistral.ai/api-keys и войди (Google или почта).
2. Если спросят про план — выбери бесплатный **Experiment** (может понадобиться подтвердить телефон).
3. Нажми **Create new key**, имя любое, срок — без ограничения.
4. Скопируй ключ и вставь сюда.

Бесплатный Mistral может использовать переписку для улучшения своих моделей. Если это важно —
отключи в настройках Mistral (Admin → Privacy).
"""},
        "llm": {
            "title": "Своя модель (платная)",
            "about": "Подключи любую модель по API — ChatGPT, Claude, Gemini, DeepSeek или через OpenRouter. "
                     "Трой будет отвечать ею, а бесплатные модели останутся запасными.",
            "fields": [{"name": "url", "label": "Сервис", "placeholder": "https://…/v1/",
                        "choices": [["https://api.openai.com/v1/", "OpenAI (ChatGPT)"],
                                    ["https://api.anthropic.com/v1/", "Anthropic (Claude)"],
                                    ["https://generativelanguage.googleapis.com/v1beta/openai/", "Google Gemini"],
                                    ["https://openrouter.ai/api/v1/", "OpenRouter — много моделей по одному ключу"],
                                    ["https://api.deepseek.com/v1/", "DeepSeek"]]},
                       {"name": "key", "label": "Ключ API", "secret": True},
                       {"name": "model", "label": "Название модели", "placeholder": "как в кабинете сервиса"},
                       {"name": "proxy", "label": "Прокси (только если сервис недоступен из твоей страны)",
                        "placeholder": "socks5://адрес:порт", "optional": True}],
            "state": _llm_state,
            "connect": lambda v: _llm_connect(assistant, v),
            "disconnect": lambda: _llm_disconnect(assistant),
            "help": """
Это для тех, кто хочет, чтобы Трой отвечал более сильной моделью. Модели платные: деньги списываются
в кабинете выбранного сервиса за каждое сообщение (обычно копейки за ответ). Бесплатные модели Троя
никуда не деваются — если у платной кончатся деньги или она недоступна, ответит бесплатная.

1. Выбери сервис и зарегистрируйся у него:
   - OpenAI (ChatGPT): https://platform.openai.com/api-keys
   - Anthropic (Claude): https://console.anthropic.com/settings/keys
   - Google Gemini: https://aistudio.google.com/apikey
   - OpenRouter: https://openrouter.ai/keys — один ключ для моделей многих компаний
   - DeepSeek: https://platform.deepseek.com/api_keys
2. Пополни баланс (у Gemini есть и бесплатный лимит).
3. Создай ключ, скопируй и вставь сюда.
4. Название модели возьми в кабинете сервиса (раздел Models / Модели) — скопируй точно, как там написано.
5. Нажми «Проверить и подключить» — Трой отправит модели одно короткое сообщение и скажет, получилось ли.

Другой сервис с API «как у OpenAI» тоже подойдёт — выбери «Другой адрес…» и впиши адрес API.
Отключить можно в любой момент — Трой вернётся на бесплатные модели.
"""},
        "github": {
            "title": "Идеи в GitHub",
            "about": "Трой записывает идеи в твой репозиторий GitHub — оттуда их удобно обсуждать с Claude или другим ИИ.",
            "fields": [{"name": "repo", "label": "Репозиторий", "placeholder": "имя/репозиторий или ссылка"},
                       {"name": "token", "label": "Токен GitHub", "secret": True, "placeholder": "github_pat_…"}],
            "state": lambda: (bool(GITHUB_TOKEN and GITHUB_REPO), GITHUB_REPO if GITHUB_TOKEN else "", ""),
            "connect": lambda v: _gh_connect(assistant, v),
            "disconnect": lambda: _gh_disconnect(assistant),
            "help": """
Без этого идеи хранятся у самого Троя. С GitHub они лежат в твоём репозитории: их видно с любого устройства,
а ИИ-ассистент (Claude, ChatGPT…) может прочитать их и обсудить с тобой.

**1. Репозиторий (один раз)**
1. Войди на https://github.com (или зарегистрируйся — бесплатно).
2. Открой https://github.com/new, назови, например, **troy-ideas**, выбери **Private** (идеи видишь только ты) → **Create repository**.

**2. Токен — ключ только к этому репозиторию**
1. Открой https://github.com/settings/personal-access-tokens/new
2. Имя — «Трой»; срок (Expiration) — какой удобно, можно **No expiration**.
3. **Repository access → Only select repositories** → выбери свой репозиторий с идеями.
4. **Permissions → Repository permissions → Contents → Read and write**.
5. Нажми **Generate token** и скопируй его (начинается с github_pat_, показывается один раз).

**3. Вставь сюда** имя репозитория (например ivan/troy-ideas) и токен.
Трой проверит доступ, перенесёт уже записанные идеи и положит в репозиторий README с подсказкой для ИИ.

**Как потом обсуждать с ИИ:** подключи GitHub в своём ИИ-ассистенте (у Claude — в настройках подключений),
дай доступ к этому репозиторию и попроси «подтяни идеи».
"""},
        **({"vps": {
            "title": "Сервер (VPS): работать круглосуточно",
            "about": "Перенести Троя с этого компьютера на твой сервер — он будет отвечать в Telegram, даже когда "
                     "компьютер выключен. Переписка, напоминания и идеи переедут вместе с ним.",
            "fields": [{"name": "address", "label": "IP-адрес сервера", "placeholder": "203.0.113.10"},
                       {"name": "password", "label": "Пароль сервера (root)", "secret": True}],
            "state": lambda: (False, "", ""),
            "connect": lambda v: (globals().get("VPS_MOVE") or (lambda _v: ("Обнови программу Трой — в этой версии "
                                                                             "переезда на сервер ещё нет.", "")))(v),
            "disconnect": lambda: None,
            "help": """
Сейчас Трой живёт на этом компьютере и отвечает, только пока компьютер включён. На своём сервере (VPS) он
работает круглосуточно.

**Что нужно:** сервер на **Ubuntu** (22.04 или 24.04) или **Debian**, его IP-адрес и пароль root — они есть в
письме хостинга или в его панели управления. Хватит самого простого тарифа: 1 ядро и 1 ГБ памяти.

**Что сделает Трой (1–3 минуты):**
- подключится к серверу, поставит себя в /opt/sometroy и запустит как службу;
- перенесёт переписку, напоминания, идеи и подключения — пароль программы останется тем же;
- переключит программу на сервер и перезапустит её.

Пароль сервера нужен только для установки и **нигде не сохраняется**. В чат его не пиши — только сюда.

Другое имя пользователя — впиши адрес как **имя@IP**, другой порт SSH — **IP:порт**.
Если у хостинга есть файрвол в панели — после переезда открой порт, который назовёт Трой (TCP).
Удалить Троя с сервера: bash /opt/sometroy/uninstall.sh
"""}} if LOCAL_MODE else {}),
        "bitrix": {
            "title": "Битрикс24",
            "about": "Задачи, чаты, CRM и аналитика. Всё, что меняет, — только после твоего «да».",
            "fields": [{"name": "url", "label": "Адрес входящего вебхука", "secret": True,
                        "placeholder": "https://портал.bitrix24.ru/rest/1/…/"}],
            "state": lambda: (bool(BITRIX_WEBHOOK), _host(BITRIX_WEBHOOK), ""),
            "connect": lambda v: _bitrix_connect(assistant, v),
            "disconnect": lambda: _bitrix_disconnect(assistant),
            "help": """
Вебхук — это ключ, по которому Трой работает в Битриксе от твоего имени и с твоими правами.

1. В своём портале Битрикс24 открой **Приложения → Разработчикам**.
2. Вкладка **Другое → Входящий вебхук**.
3. Отметь права: **Задачи (task)**, **Чаты и уведомления (im)**, **CRM (crm)**, **Пользователи (user)**.
4. Нажми **Сохранить** и скопируй адрес вебхука целиком — вида https://портал.bitrix24.ru/rest/1/abc123…/
5. Вставь его сюда.

Если раздела «Разработчикам» нет — нужны права администратора портала, попроси администратора.
"""},
    }


class AppAuth:
    """Вход в программу на компьютере. Пароль задаёт владелец в Telegram (/password),
    в базе хранится только его хэш. После смены пароля все компьютеры выходят."""
    MAX_FAILS, WINDOW, LOCK = 5, 600, 600

    def __init__(self, notify=notify_owner):
        self.notify = notify
        self.fails: list = []
        self.locked_until = 0.0
        self.lock = threading.Lock()

    @staticmethod
    def _hash(password: str, salt: bytes) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000).hex()

    @staticmethod
    def _sha(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @classmethod
    def set_password(cls, password: str) -> int:
        salt = os.urandom(16)
        with closing(db()) as con, con:
            con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES ('app_password', ?)",
                        (salt.hex() + ":" + cls._hash(password, salt),))
            return con.execute("DELETE FROM app_sessions").rowcount

    @classmethod
    def check_password(cls, password: str) -> bool:
        with closing(db()) as con:
            row = con.execute("SELECT value FROM kv WHERE key='app_password'").fetchone()
        if not row or not password:
            return False
        salt_hex, stored = row["value"].split(":", 1)
        ok = hmac.compare_digest(cls._hash(password, bytes.fromhex(salt_hex)), stored)
        if not ok:
            time.sleep(1)
        return ok

    @staticmethod
    def has_password() -> bool:
        with closing(db()) as con:
            return con.execute("SELECT 1 FROM kv WHERE key='app_password'").fetchone() is not None

    @staticmethod
    def devices() -> list:
        with closing(db()) as con:
            return con.execute("SELECT device, ip, created, last_seen FROM app_sessions ORDER BY last_seen DESC").fetchall()

    @staticmethod
    def logout_all() -> int:
        with closing(db()) as con, con:
            return con.execute("DELETE FROM app_sessions").rowcount

    def login(self, password: str, device: str, ip: str):
        with self.lock:
            now = time.time()
            if now < self.locked_until:
                return None, f"Слишком много неверных попыток. Попробуй через {int(self.locked_until - now) // 60 + 1} мин."
            with closing(db()) as con:
                row = con.execute("SELECT value FROM kv WHERE key='app_password'").fetchone()
            if not row:
                return None, f"Пароль для приложения ещё не задан. Напиши {BOT_NAME}ю в Telegram: /password твой_пароль"
            salt_hex, stored = row["value"].split(":", 1)
            if not hmac.compare_digest(self._hash(password, bytes.fromhex(salt_hex)), stored):
                self.fails = [t for t in self.fails if now - t < self.WINDOW] + [now]
                time.sleep(1)
                if len(self.fails) >= self.MAX_FAILS:
                    self.locked_until = now + self.LOCK
                    self.fails = []
                    self.notify(f"⚠️ {self.MAX_FAILS} неверных паролей подряд в приложении (последний — с {ip}, "
                                f"{device}). Вход заблокирован на 10 минут. Если это не ты — смени пароль: /password")
                return None, "Неверный пароль"
            self.fails = []
            token = secrets.token_urlsafe(32)
            with closing(db()) as con, con:
                con.execute("INSERT INTO app_sessions VALUES (?,?,?,?,?)",
                            (self._sha(token), device, ip, int(now), int(now)))
        self.notify(f"🔐 Вход в приложение: {device} ({ip}). Если это не ты — смени пароль: /password")
        return token, ""

    def check(self, token: str) -> bool:
        if not token:
            return False
        h = self._sha(token)
        with closing(db()) as con, con:
            row = con.execute("SELECT last_seen FROM app_sessions WHERE token_hash=?", (h,)).fetchone()
            if row and time.time() - row["last_seen"] > 300:
                con.execute("UPDATE app_sessions SET last_seen=? WHERE token_hash=?", (int(time.time()), h))
        return row is not None

    def device(self, token: str) -> str:
        with closing(db()) as con:
            row = con.execute("SELECT device FROM app_sessions WHERE token_hash=?", (self._sha(token),)).fetchone()
        return row["device"] if row else ""

    def logout(self, token: str) -> None:
        with closing(db()) as con, con:
            con.execute("DELETE FROM app_sessions WHERE token_hash=?", (self._sha(token),))


# ───────────────────────── Управление компьютерами ─────────────────────────

PRESENCE: dict = {}            # компьютер -> когда последний раз спрашивал команды
PC_VERSION: dict = {}          # компьютер -> версия программы Трой на нём
PC_ONLINE_SEC = 30


def pcs_status() -> str:
    now = time.time()
    if not PRESENCE:
        return "Компьютеры: ни один ещё не выходил на связь с запуска сервера (нужна программа Трой новой версии)."
    rows = []
    for d, t in sorted(PRESENCE.items(), key=lambda x: -x[1]):
        ago = int(now - t)
        state = "🟢 в сети" if ago < PC_ONLINE_SEC else "⚪ не в сети"
        rows.append(f"- 🖥 {d}: {state}, был на связи {ago} с назад, программа {PC_VERSION.get(d, 'старая версия')}")
    return "Компьютеры:\n" + "\n".join(rows)


def online_pcs() -> list:
    now = time.time()
    return sorted(d for d, t in PRESENCE.items() if now - t < PC_ONLINE_SEC)


def pc_label(action: str, arg: str) -> str:
    if action == "open_site":
        try:
            info = json.loads(arg or "{}")
        except ValueError:
            info = {}
        return f"открыть сайт {re.sub(r'^https?://', '', info.get('url', '')).split('/')[0]}"
    if action in ("file_send", "file_get"):
        try:
            info = json.loads(arg or "{}")
        except ValueError:
            info = {}
        return f"переслать файл «{info.get('q') or info.get('name') or ''}»"
    return PC_ACTIONS.get(action, action) + (f" «{arg}»" if arg else "")


def pc_notify(channel: str, chat_id: str, text: str) -> None:
    """Результат — туда, откуда пришла команда."""
    if channel == "telegram" and tg_linked():
        try:
            Telegram(TELEGRAM_TOKEN).send(chat_id, text)
        except Exception as e:
            log.warning("Не отправил результат команды: %s", e)
    else:
        push_event(text)


def pc_create(action: str, arg: str, device_hint: str, channel: str, chat_id: str):
    online = online_pcs()
    label = pc_label(action, arg)
    if not online:
        return ("Ни один компьютер с Троем сейчас не в сети. Программа Трой должна быть запущена "
                "(можно свёрнутой в трей)."), None
    target = None
    if device_hint:
        h = re.sub(r"[^\w]", "", device_hint.lower())
        matches = [d for d in online if h and (h in re.sub(r"[^\w]", "", d.lower()) or re.sub(r"[^\w]", "", d.lower()) in h)]
        target = matches[0] if len(matches) == 1 else None
    if not target and len(online) == 1:
        target = online[0]
    now = int(time.time())
    with closing(db()) as con, con:
        if target:
            con.execute("INSERT INTO pc_commands(device, action, arg, status, channel, chat_id, created) "
                        "VALUES (?,?,?,?,?,?,?)", (target, action, arg, "pending", channel, chat_id, now))
            return f"Команда «{label}» отправлена на компьютер {target}. Результат придёт отдельным сообщением.", None
        cur = con.execute("INSERT INTO pc_commands(action, arg, choices, status, channel, chat_id, created) "
                          "VALUES (?,?,?,?,?,?,?)", (action, arg, json.dumps(online), "choose", channel, chat_id, now))
    return ("Компьютеров в сети несколько — пользователю показаны кнопки выбора. Коротко попроси выбрать компьютер.",
            {"id": cur.lastrowid, "devices": online, "label": label})


def pc_choose(cmd_id: int, choice) -> str:
    """choice — номер кнопки (Telegram) или имя компьютера (программа)."""
    with closing(db()) as con, con:
        row = con.execute("SELECT * FROM pc_commands WHERE id=?", (cmd_id,)).fetchone()
        if not row or row["status"] != "choose":
            return "Этот выбор уже неактуален."
        devices = json.loads(row["choices"] or "[]")
        device = devices[choice] if isinstance(choice, int) and 0 <= choice < len(devices) else choice
        if device not in devices:
            return "Такого компьютера нет в списке."
        if time.time() - row["created"] > 600:
            con.execute("UPDATE pc_commands SET status='expired' WHERE id=?", (cmd_id,))
            return "Выбор устарел (больше 10 минут). Повтори просьбу."
        con.execute("UPDATE pc_commands SET device=?, status='pending', created=? WHERE id=?",
                    (device, int(time.time()), cmd_id))
    return f"🖥 {device}: {pc_label(row['action'], row['arg'])} — отправлено"


def pc_poll(device: str, version: str = "") -> list:
    """Компьютер спрашивает, нет ли для него команд (заодно отмечается «в сети»)."""
    PRESENCE[device] = time.time()
    if version:
        PC_VERSION[device] = version
    expire_stale()
    with closing(db()) as con, con:
        rows = con.execute("SELECT * FROM pc_commands WHERE status='pending' AND device=? ORDER BY id",
                           (device,)).fetchall()
        for r in rows:
            con.execute("UPDATE pc_commands SET status='sent' WHERE id=?", (r["id"],))
    return [{"id": r["id"], "action": r["action"], "arg": r["arg"]} for r in rows]


STALE_LOCK = threading.Lock()


def expire_stale() -> None:
    """Команда компьютеру висит 2 минуты — говорим владельцу, что именно не так, а не молчим."""
    now = int(time.time())
    with STALE_LOCK, closing(db()) as con, con:      # замок: два потока не сообщат об одной команде дважды
        stale = con.execute("SELECT * FROM pc_commands WHERE status IN ('pending','sent','choose') AND created < ? "
                            "AND (action NOT IN ('file_send', 'file_get') OR created < ?)",   # файл до 50 МБ — даём 10 минут
                            (now - 120, now - 600)).fetchall()
        for r in stale:
            con.execute("UPDATE pc_commands SET status='expired' WHERE id=?", (r["id"],))
    for r in stale:
        if r["action"] == "file_get":
            file_forget(r["arg"])
    files_cleanup()
    for r in stale:
        label = pc_label(r["action"], r["arg"])
        if r["status"] == "choose":
            continue                                   # кнопку выбора просто не нажали — это не ошибка
        if r["status"] == "pending":
            why = (f"⚠️ «{label}» не выполнено: {r['device']} не забрал команду за 2 минуты — "
                   "программа Трой там закрыта, компьютер выключен или нет интернета.")
        else:
            why = (f"⚠️ «{label}»: {r['device']} взял команду, но не сообщил результат — возможно, программа "
                   "там зависла или пропал интернет. Подробности — в журнале программы (troy.log).")
        pc_notify(r["channel"], r["chat_id"], why)


def pc_result(device: str, cmd_id: int, ok: bool, text: str, late: bool = False) -> None:
    with closing(db()) as con, con:
        row = con.execute("SELECT * FROM pc_commands WHERE id=? AND device=?", (cmd_id, device)).fetchone()
        if late:                                           # дополнение к выполненной команде: «выключаю», «отменили»
            if not row or row["status"] != "done":
                return
            if not ok:
                con.execute("UPDATE pc_commands SET status=?, result=? WHERE id=?", ("cancelled", text[:300], cmd_id))
        else:
            if not row or row["status"] != "sent":
                return
            con.execute("UPDATE pc_commands SET status=?, result=? WHERE id=?",
                        ("done" if ok else "failed", text[:300], cmd_id))
    if late:                                               # записи в историю — уже после закрытия транзакции
        if not ok:
            POWER_WATCH.pop(cmd_id, None)
        pc_notify(row["channel"], row["chat_id"], f"{'⏻' if ok else '↩️'} {device}: {text}")
        add_history(row["chat_id"], "pc_result", f"{device}: {text}")
        return
    if row["action"] == "file_get":
        file_forget(row["arg"])                            # доставлено (или не вышло) — с сервера убираем
    if row["action"] == "file_send" and ok:                # источник отдал файл — ждём получателя, без лишних сообщений
        add_history(row["chat_id"], "pc_result", f"{device}: {text}")
        return
    if not ok and row["action"] == "open_app" and "не нашёл программу" in text and row["channel"] == "desktop":
        text += f". Если это сайт — скажи «открой сайт {row['arg']}»"
    pc_notify(row["channel"], row["chat_id"], f"{'✅' if ok else '⚠️'} {device}: {text}")
    add_history(row["chat_id"], "pc_result", f"{device}: {'выполнено' if ok else 'не получилось'} — {text}")
    if ok and row["action"] in ("shutdown", "restart", "sleep"):
        POWER_WATCH[cmd_id] = {"device": device, "action": row["action"], "channel": row["channel"],
                               "chat_id": row["chat_id"], "t0": time.time(), "gone": False}


# ───────────────────────── Файлы между компьютерами ─────────────────────────
# Компьютер-источник находит файл и отдаёт его серверу, компьютер-получатель забирает и сохраняет.
# На сервере файл лежит только до доставки (и не дольше часа, если получатель так и не забрал).

FILES_DIR = BASE / "transfer"


def _pick_pc(name: str, online: list) -> Optional[str]:
    h = re.sub(r"[^\w]", "", (find_device(name) or name or "").lower())
    hits = [d for d in online if h and (h in re.sub(r"[^\w]", "", d.lower()) or re.sub(r"[^\w]", "", d.lower()) in h)]
    return hits[0] if len(hits) == 1 else None


def file_command(file: str, source: str, target: str, channel: str, chat_id: str, user_text: str = "") -> str:
    """Задание компьютеру-источнику: найти файл и передать его на компьютер-получатель."""
    file = file.strip().strip("«»\"'")
    if not file:
        return "Ошибка: укажи file — какой файл переслать."
    online = online_pcs()
    if len(online) < 2:
        return ("Чтобы переслать файл, нужны два компьютера с запущенной программой Трой. Сейчас в сети: "
                + (", ".join(online) or "ни одного") + ".")
    src, dst = _pick_pc(source, online), _pick_pc(target, online)
    if not src and dst and len(online) == 2:
        src = next(d for d in online if d != dst)
    if not dst and src and len(online) == 2:
        dst = next(d for d in online if d != src)
    if channel == "desktop" and not dst and src:        # «забери с ZEON» из программы — сюда, если это понятно
        others = [d for d in online if d != src]
        dst = others[0] if len(others) == 1 else None
    if not src or not dst or src == dst:
        return (f"Уточни у пользователя, с какого компьютера на какой переслать файл. В сети: {', '.join(online)}.")
    with closing(db()) as con, con:
        con.execute("INSERT INTO pc_commands(device, action, arg, status, channel, chat_id, created) VALUES (?,?,?,?,?,?,?)",
                    (src, "file_send", json.dumps({"q": file, "to": dst}, ensure_ascii=False), "pending", channel,
                     chat_id, int(time.time())))
    return (f"Задание отправлено: {src} ищет «{file}» и передаёт на {dst}. Когда файл дойдёт, придёт отдельное "
            "сообщение. Коротко скажи пользователю, что пересылаешь.")


def file_upload(device: str, cmd_id: int, name: str, data: bytes) -> tuple:
    """Источник прислал файл → кладём во временную папку и даём задание получателю."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (name or "файл").strip())[:120] or "файл"
    with closing(db()) as con, con:
        row = con.execute("SELECT * FROM pc_commands WHERE id=? AND device=? AND action='file_send'",
                          (cmd_id, device)).fetchone()
        if not row or row["status"] != "sent":
            return False, "задание на пересылку не найдено или устарело"
        info = json.loads(row["arg"] or "{}")
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(16)
        (FILES_DIR / token).write_bytes(data)
        con.execute("INSERT INTO pc_commands(device, action, arg, status, channel, chat_id, created) VALUES (?,?,?,?,?,?,?)",
                    (info.get("to"), "file_get", json.dumps({"id": token, "name": name, "size": len(data), "from": device},
                                                            ensure_ascii=False), "pending", row["channel"], row["chat_id"],
                     int(time.time())))
    return True, ""


def file_download(device: str, token: str) -> Optional[tuple]:
    """Получатель забирает файл — только тот компьютер, которому он адресован."""
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return None
    with closing(db()) as con:
        rows = con.execute("SELECT arg FROM pc_commands WHERE device=? AND action='file_get' AND status IN ('sent','pending')",
                           (device,)).fetchall()
    for r in rows:
        info = json.loads(r["arg"] or "{}")
        if info.get("id") == token and (FILES_DIR / token).exists():
            return (FILES_DIR / token).read_bytes(), info.get("name") or "файл"
    return None


def file_forget(arg: str) -> None:
    """После доставки (или неудачи) — копию с сервера удаляем."""
    try:
        token = json.loads(arg or "{}").get("id", "")
        if re.fullmatch(r"[0-9a-f]{32}", token):
            (FILES_DIR / token).unlink(missing_ok=True)
    except (ValueError, OSError) as e:
        log.warning("Не удалил временный файл: %s", e)


def files_cleanup(max_age: int = 3600) -> None:
    """Подстраховка: всё, что лежит дольше часа, удаляем."""
    if not FILES_DIR.exists():
        return
    now = time.time()
    for f in FILES_DIR.iterdir():
        try:
            if now - f.stat().st_mtime > max_age:
                f.unlink()
        except OSError:
            pass


# Отчёт после выключения/перезагрузки/сна: компьютер сам уже не напишет — смотрим, когда он пропал из сети
POWER_WATCH: dict = {}
GONE_SEC = 20          # компьютер спрашивает команды каждые 4 с; 20 с тишины — значит, выключился


def power_watch_once(now: Optional[float] = None) -> None:
    now = now or time.time()
    for cmd_id, w in list(POWER_WATCH.items()):
        d, action = w["device"], w["action"]
        silent = now - PRESENCE.get(d, 0)
        say = None
        if action in ("shutdown", "sleep"):
            if silent > GONE_SEC:
                say = f"⏻ {d} выключился." if action == "shutdown" else f"🌙 {d} уснул."
            elif now - w["t0"] > 240:
                say = (f"⚠️ {d} всё ещё в сети — Windows не выключилась (возможно, мешает несохранённый документ)."
                       if action == "shutdown" else f"⚠️ {d} не уснул и всё ещё в сети.")
        else:                                            # перезагрузка: сначала пропал, потом вернулся
            if not w["gone"] and silent > GONE_SEC:
                w["gone"] = True
            elif w["gone"] and silent < 10:
                say = f"✅ {d} перезагрузился и снова в сети."
            elif not w["gone"] and now - w["t0"] > 240:
                say = f"⚠️ {d} всё ещё в сети — перезагрузка не началась (возможно, мешает несохранённый документ)."
            elif w["gone"] and now - w["t0"] > 900:
                say = (f"⚠️ {d} ушёл на перезагрузку, но за 15 минут Трой на нём не запустился. "
                       "Включи в установщике «Запускать вместе с Windows» — тогда я увижу, когда он вернётся.")
        if say:
            POWER_WATCH.pop(cmd_id, None)
            pc_notify(w["channel"], w["chat_id"], say)
            add_history(w["chat_id"], "pc_result", say)


def power_watch_loop() -> None:
    while True:
        try:
            power_watch_once()
            expire_stale()
        except Exception:
            log.exception("Слежение за питанием")
        time.sleep(5)


BACKUP_LOCK = threading.Lock()


def backup_export(password: str) -> tuple:
    """Копия памяти Троя файлом под паролем программы. (данные, ошибка)"""
    if not AppAuth.check_password(password):
        return None, "Неверный пароль программы."
    with BACKUP_LOCK:
        data = backup.make(db, ALLOWED_USER_ID, LOCAL_IDEAS_DIR, bool(GITHUB_TOKEN and GITHUB_REPO), BOT_NAME,
                           globals().get("SERVER_VERSION", ""))
        blob = backup.pack(data, password)
    log.info("Сделана копия памяти: %s", backup.summary(data))
    return blob, ""


def backup_import(password: str, blob: bytes) -> tuple:
    """Загрузить копию в этого Троя: нужен пароль программы того Троя, с которого её сняли. (получилось, текст)"""
    try:
        data = backup.unpack(blob, password)
        with BACKUP_LOCK:
            text = backup.restore(db, data, ALLOWED_USER_ID, LOCAL_IDEAS_DIR)
    except backup.BackupError as e:
        time.sleep(1)                           # перебор паролей к файлу через Троя — медленно
        return False, str(e)
    log.info("Загружена копия памяти: %s", backup.summary(data))
    extra = (" Идеи прежнего Троя лежали в GitHub — подключи тот же репозиторий в «Подключениях»."
             if data.get("ideas_in_github") and not (GITHUB_TOKEN and GITHUB_REPO) else "")
    return True, text + " Ключи моделей, Telegram и Битрикс24 в копию не входят — они настраиваются отдельно." + extra


class PcHooks:
    backup_export = staticmethod(backup_export)
    backup_import = staticmethod(backup_import)
    poll = staticmethod(pc_poll)
    result = staticmethod(pc_result)
    choose = staticmethod(pc_choose)
    file_upload = staticmethod(file_upload)
    server_version = SERVER_VERSION
    file_download = staticmethod(file_download)
    FILE_MAX = FILE_MAX_MB * 1024 * 1024


def start_desktop_api(assistant: "Assistant"):
    cert, key = TLS_DIR / "cert.pem", TLS_DIR / "key.pem"
    if not ALLOWED_USER_ID:
        log.warning("Программа на компьютере не запущена: сначала впиши ALLOWED_USER_ID")
        return None
    if LOCAL_MODE:                      # программа и сервер на одном компьютере — связь не выходит наружу
        return desktop_api.start(DESKTOP_PORT, AppAuth(), PcHooks, None, None,
                                 lambda text, files: desktop_reply(assistant, text, files),
                                 desktop_history, events_after, BOT_NAME, host="127.0.0.1", conns=assistant.conns)
    if not (cert.exists() and key.exists()):
        log.warning("Нет сертификата в %s — программа на компьютере не запущена (его создаёт install.sh)", TLS_DIR)
        return None
    return desktop_api.start(DESKTOP_PORT, AppAuth(), PcHooks, str(cert), str(key),
                             lambda text, files: desktop_reply(assistant, text, files),
                             desktop_history, events_after, BOT_NAME, conns=assistant.conns)


# ───────────────────────── Проверка и запуск ─────────────────────────

def self_check() -> bool:
    ok = True
    print("Telegram:", end=" ", flush=True)
    if not TELEGRAM_TOKEN:
        print("не подключён (необязательно)")
    else:
        try:
            me = Telegram(TELEGRAM_TOKEN).call("getMe")
            print(f"OK, бот @{me['username']}" + ("" if tg_linked() else " — хозяин ещё не привязан"))
        except Exception as e:
            ok = False
            print(f"ОШИБКА — {e}")

    print("Groq:", end=" ", flush=True)
    if not GROQ_API_KEY:
        ok = False
        print("ОШИБКА — не указан GROQ_API_KEY")
    else:
        g = Groq()
        try:
            available = set(g.models())
            for m in GROQ_MODELS + [GROQ_SEARCH_MODEL, GROQ_STT_MODEL]:
                print(f"{m} {'есть' if m in available else 'НЕТ'};", end=" ")
            print()
            if GROQ_MODELS[0] not in available:
                ok = False
            reply = clean_answer(g.chat(GROQ_MODELS[0], [{"role": "user", "content": "Ответь одним словом: работаю"}]
                                        ).get("content"))
            print(f"  тестовый ответ модели: {reply[:60]}")
        except Exception as e:
            ok = False
            print(f"ОШИБКА — {e}")

    print("Mistral (запасной):", end=" ", flush=True)
    if not MISTRAL_API_KEY:
        print("не подключён (MISTRAL_API_KEY пуст)")
    else:
        try:
            reply = clean_answer(Mistral().chat(MISTRAL_MODEL, [{"role": "user", "content": "Ответь одним словом: работаю"}]
                                                ).get("content"))
            print(f"OK, {MISTRAL_MODEL}: {reply[:40]}")
        except Exception as e:
            print(f"ОШИБКА — {e}")

    print("Идеи:", end=" ", flush=True)
    if not (GITHUB_TOKEN and GITHUB_REPO):
        print(f"GitHub не настроен — буду хранить в папке {LOCAL_IDEAS_DIR}")
    else:
        try:
            ideas = Ideas()
            r = ideas.s.get(f"https://api.github.com/repos/{GITHUB_REPO}", timeout=30)
            r.raise_for_status()
            perms = r.json().get("permissions") or {}
            print(f"GitHub OK, репозиторий {GITHUB_REPO}" + ("" if perms.get("push", True) else
                                                            " — но НЕТ прав на запись (Contents: Read and write)"))
        except Exception as e:
            ok = False
            print(f"ОШИБКА — {e}. Проверь GITHUB_REPO (владелец/имя) и токен.")

    print("Битрикс24:", end=" ", flush=True)
    if not BITRIX_WEBHOOK:
        print("не подключён (BITRIX_WEBHOOK пуст)")
    else:
        try:
            bx = bitrix.Bitrix(BITRIX_WEBHOOK)
            me = bx.me()
            scopes = bx.call("scope").get("result") or []
            need = {"task", "im", "crm", "user"}
            missing = need - set(scopes) - ({"user"} if "user_brief" in scopes or "user_basic" in scopes else set())
            print(f"OK, вхожу как {me.get('NAME', '')} {me.get('LAST_NAME', '')}"
                  + (f"; НЕ ХВАТАЕТ прав: {', '.join(sorted(missing))}" if missing else ""))
        except Exception as e:
            ok = False
            print(f"ОШИБКА — {e}. Проверь адрес вебхука.")

    try:
        init_db()
        pw = "пароль задан" if AppAuth.has_password() else "пароль ещё не задан — напиши боту /password"
    except Exception:
        pw = "?"
    print("Компьютер:", f"порт {DESKTOP_PORT}, {pw}",
          "" if (TLS_DIR / "cert.pem").exists() else "— сертификат ещё не создан")
    print("Владелец:", ALLOWED_USER_ID or "не указан — напиши боту, он пришлёт твой ID")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description="Личный ассистент в Telegram")
    ap.add_argument("--check", action="store_true", help="проверить настройки и доступ к сервисам")
    ap.add_argument("--set-password", action="store_true",
                    help="задать пароль программы на компьютере (пароль читается из stdin)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    for h in logging.getLogger().handlers:      # токены и ключи в журнал не пишем
        h.addFilter(_RedactFilter())
        h.setFormatter(_RedactFormatter("%(asctime)s %(levelname)s %(message)s"))

    if args.check:
        raise SystemExit(0 if self_check() else 1)
    if args.set_password:               # мастер настройки задаёт пароль по SSH — в командной строке его не видно
        import sys
        password = sys.stdin.readline().rstrip("\r\n")
        if len(password) < 4:
            raise SystemExit("Пароль слишком короткий")
        init_db()
        AppAuth.set_password(password)
        print("Пароль программы задан")
        return
    serve()


def serve(stop: Optional[threading.Event] = None) -> None:
    """Запуск Троя: на сервере — из main(), в локальном режиме — из программы на компьютере (в потоке)."""
    if not GROQ_API_KEY:
        raise SystemExit("Укажите GROQ_API_KEY в settings.env")

    init_db()
    with closing(db()) as con, con:   # история от прежней версии на Gemini: роль model → assistant
        con.execute("UPDATE history SET role='assistant' WHERE role='model'")
    stop = stop or threading.Event()
    assistant = Assistant()
    register_modules(assistant)
    # программа на компьютере работает, даже если Telegram пока недоступен или не подключён
    start_desktop_api(assistant)
    threading.Thread(target=reminder_loop, args=(stop,), daemon=True).start()
    threading.Thread(target=power_watch_loop, daemon=True).start()
    while not stop.is_set():                 # Telegram можно подключить, сменить или отключить на ходу
        if not TELEGRAM_TOKEN:
            stop.wait(3)
            continue
        try:
            telegram_session(assistant)
        except Exception:
            log.exception("Telegram")
            stop.wait(10)


def telegram_session(assistant: "Assistant") -> None:
    """Работаем с текущим ботом, пока токен не сменили в «Подключениях»."""
    token = TELEGRAM_TOKEN
    tg = Telegram(token)
    while True:
        if TELEGRAM_TOKEN != token:
            return
        try:
            me = tg.call("getMe")
            tg.call("deleteWebhook")
            break
        except (requests.RequestException, TelegramError) as e:
            log.warning("Telegram недоступен (%s) — пробую снова через 30 с", human_error(e))
            TG_STATE.update(ok=False, error=human_error(e), at=time.time())
            time.sleep(30)
    TG_STATE.update(ok=True, error="", at=time.time())
    tg.call("setMyCommands", data={"commands": json.dumps([
        {"command": "reminders", "description": "Мои напоминания"},
        {"command": "ideas", "description": "Проекты с идеями"},
        {"command": "new", "description": "Начать разговор заново"},
        {"command": "log", "description": "Сохранить переписку файлом (/log неделя)"},
        {"command": "status", "description": "Модели: кто отвечает, кто на перерыве"},
        {"command": "connect", "description": "Подключить ещё один компьютер"},
        {"command": "diag", "description": "Диагностика: что работает и что нет"},
        {"command": "bug", "description": "Сообщить об ошибке разработчикам"},
        {"command": "help", "description": "Что я умею"},
    ], ensure_ascii=False)})
    log.info("Ассистент @%s запущен. Модели: %s%s, идеи: %s", me["username"], ", ".join(GROQ_MODELS),
             " + Mistral" if MISTRAL_API_KEY else "",
             f"GitHub {GITHUB_REPO}" if GITHUB_TOKEN and GITHUB_REPO else "локально")
    Bot(tg, assistant).poll_forever(lambda: TELEGRAM_TOKEN != token)


if __name__ == "__main__":
    main()
