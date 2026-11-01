"""Мастер первой настройки Троя — разговор в окне программы.

Ведёт его обычный код по сценарию, а не нейросеть: ключи и пароли, которые вводит человек,
проверяются напрямую у сервисов (Groq, Telegram, Mistral, Битрикс24) и записываются только
в файл настроек — в модель они не попадают. Пароль сервера нигде не сохраняется.

Сообщение мастера: {"text", "buttons": [...], "secret": bool, "placeholder": str, "done": bool}
"""
import json
import logging
import random
import re
import socket
import time
from pathlib import Path
from typing import Callable, List, Optional

import requests

log = logging.getLogger("troy.setup")

FILMS = ["«Первая настройка за пять минут»", "«Ключ, который начинается с gsk_»", "«Бот, который ждал твоего привета»",
         "«Пароль, который знаешь только ты»", "«Сервер, который смог»"]

# часовые пояса: смещение от UTC в часах → понятное название и зона
ZONES = {2: ("Калининград", "Europe/Kaliningrad"), 3: ("Москва", "Europe/Moscow"), 4: ("Самара", "Europe/Samara"),
         5: ("Екатеринбург", "Asia/Yekaterinburg"), 6: ("Омск", "Asia/Omsk"), 7: ("Новосибирск", "Asia/Novosibirsk"),
         8: ("Иркутск", "Asia/Irkutsk"), 9: ("Якутск", "Asia/Yakutsk"), 10: ("Владивосток", "Asia/Vladivostok"),
         11: ("Магадан", "Asia/Magadan"), 12: ("Камчатка", "Asia/Kamchatka"), 1: ("Берлин", "Europe/Berlin"),
         0: ("Лондон", "Europe/London")}


def msg(text: str, buttons: Optional[List[str]] = None, secret: bool = False, placeholder: str = "",
        done: bool = False) -> dict:
    return {"text": text, "buttons": buttons or [], "secret": secret, "placeholder": placeholder, "done": done}


# ───────────────────────── проверки у сервисов ─────────────────────────

def _proxies(proxy: str) -> Optional[dict]:
    return {"http": proxy, "https": proxy} if proxy else None


def check_groq(key: str) -> Optional[str]:
    """None — ключ рабочий, иначе — понятная причина."""
    if not key.startswith("gsk_"):
        return "Ключ Groq начинается с gsk_ — похоже, скопировалось что-то другое."
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    except requests.RequestException:
        return "Не достучался до Groq — проверь интернет и попробуй ещё раз."
    if r.status_code == 401:
        return "Groq говорит, что ключ неверный. Создай новый и вставь целиком."
    if r.status_code != 200:
        return f"Groq ответил ошибкой {r.status_code}. Попробуй ещё раз через минуту."
    return None


def tg_call(token: str, method: str, proxy: str = "", **params):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=params, timeout=40, proxies=_proxies(proxy))
    data = r.json()
    if not data.get("ok"):
        raise ValueError(data.get("description") or f"HTTP {r.status_code}")
    return data["result"]


def check_telegram(token: str, proxy: str = ""):
    """(имя бота, None) или (None, причина, нужен_прокси)."""
    if not re.fullmatch(r"\d{5,12}:[\w-]{30,}", token):
        return None, "Токен выглядит так: 123456789:ABCdef… (цифры, двоеточие, буквы). Скопируй его из @BotFather целиком.", False
    try:
        me = tg_call(token, "getMe", proxy)
    except requests.RequestException:
        return None, "Не достучался до Telegram.", True
    except ValueError:
        return None, "Telegram говорит, что токен неверный. Проверь, что скопировал его из @BotFather целиком.", False
    return me.get("username"), None, False


def find_owner(token: str, proxy: str = "", offset: int = 0) -> Optional[dict]:
    """Последний, кто написал боту в личку: {"id", "name", "update"}."""
    tg_call(token, "deleteWebhook", proxy)
    updates = tg_call(token, "getUpdates", proxy, timeout=0, offset=offset, allowed_updates=json.dumps(["message"]))
    for u in reversed(updates):
        m = u.get("message") or {}
        if (m.get("chat") or {}).get("type") == "private" and m.get("from"):
            f = m["from"]
            return {"id": str(f["id"]), "name": f.get("first_name") or f.get("username") or "", "update": u["update_id"]}
    return None


def check_mistral(key: str) -> Optional[str]:
    try:
        r = requests.get("https://api.mistral.ai/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    except requests.RequestException:
        return "Не достучался до Mistral — попробуй ещё раз."
    return None if r.status_code == 200 else "Mistral не принял ключ. Проверь, что скопировал его целиком."


def check_bitrix(url: str) -> Optional[str]:
    if not re.match(r"https://[^/]+/rest/\d+/[\w]+/?$", url):
        return "Адрес вебхука выглядит так: https://портал.bitrix24.ru/rest/1/abcdef123456/"
    try:
        r = requests.get(url.rstrip("/") + "/profile.json", timeout=20)
        if r.status_code == 200 and r.json().get("result"):
            return None
    except (requests.RequestException, ValueError):
        pass
    return "Битрикс24 не ответил по этому вебхуку. Проверь адрес и права (task, im, crm, user)."


def local_zone() -> tuple:
    offset = round(-time.timezone / 3600 + (1 if time.daylight and time.localtime().tm_isdst else 0))
    if offset in ZONES:
        return ZONES[offset]
    sign = "-" if offset > 0 else "+"          # у Etc/GMT знак наоборот
    return f"UTC{offset:+d}", f"Etc/GMT{sign}{abs(offset)}"


def settings_text(template: str, values: dict) -> str:
    """Шаблон settings.env.example с подставленными значениями (комментарии остаются)."""
    out, seen = [], set()
    for line in template.splitlines():
        m = re.match(r"([A-Z_]+)=", line)
        if m and m.group(1) in values:
            out.append(f"{m.group(1)}={values[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in seen]
    return "\n".join(out) + "\n"


# ───────────────────────── сервер (VPS) по SSH ─────────────────────────

SERVER_FILES = ["bot.py", "bitrix.py", "connectors.py", "desktop_api.py", "requirements.txt", "install.sh", "uninstall.sh"]


def install_vps(ip: str, user: str, password: str, settings: str, src_dir: Path, say: Callable[[str], None]) -> dict:
    """Ставит Троя на сервер. Возвращает {"ok", "port", "cert", "error", "log"}."""
    import paramiko
    out_lines: List[str] = []
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    def run(cmd: str, stdin_text: str = "", timeout: int = 60):
        _in, o, e = cli.exec_command(cmd, timeout=timeout)
        if stdin_text:
            _in.write(stdin_text)
            _in.channel.shutdown_write()
        text = o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")
        return o.channel.recv_exit_status(), text

    try:
        cli.connect(ip, username=user, password=password, timeout=20, look_for_keys=False, allow_agent=False,
                    banner_timeout=20, auth_timeout=20)
    except paramiko.AuthenticationException:
        return {"ok": False, "error": "Сервер не принял логин или пароль."}
    except (socket.error, paramiko.SSHException) as e:
        return {"ok": False, "error": f"Не подключился к серверу {ip}: {e}"}
    try:
        code, osr = run(". /etc/os-release && echo $ID $VERSION_ID; id -u")
        if code != 0 or not re.search(r"\b(ubuntu|debian)\b", osr):
            return {"ok": False, "error": "Нужен сервер на Ubuntu или Debian."}
        if osr.strip().splitlines()[-1].strip() != "0":
            return {"ok": False, "error": "Нужен пользователь root (или с правами root)."}
        # свободный порт для программы на компьютере (переустановка — прежний)
        _, prev = run("grep -E '^DESKTOP_PORT=' /opt/sometroy/settings.env 2>/dev/null | cut -d= -f2")
        _, busy = run("ss -ltnH 2>/dev/null | awk '{print $4}'")
        used = {int(p) for p in re.findall(r":(\d+)\s", busy + "\n")}
        port = int(prev.strip()) if prev.strip().isdigit() else next(p for p in range(8443, 8500) if p not in used)
        say(f"Подключился. Ставлю в /opt/sometroy, порт для программы — {port}.")
        settings = re.sub(r"(?m)^DESKTOP_PORT=.*$", f"DESKTOP_PORT={port}", settings)
        run("mkdir -p /opt/sometroy")
        sftp = cli.open_sftp()
        for name in SERVER_FILES:          # переводы строк — как в Linux, даже если файлы прошли через Windows
            data = (src_dir / name).read_bytes().replace(b"\r\n", b"\n")
            with sftp.open(f"/opt/sometroy/{name}", "wb") as f:
                f.write(data)
        with sftp.open("/opt/sometroy/settings.env", "wb") as f:
            f.write(settings.encode("utf-8"))
        sftp.chmod("/opt/sometroy/settings.env", 0o600)
        say("Файлы на месте. Устанавливаю — это займёт 1–3 минуты…")
        _in, o, _e = cli.exec_command(f"bash /opt/sometroy/install.sh {ip} 2>&1", timeout=900)
        for raw in iter(o.readline, ""):
            line = raw.rstrip()
            if line:
                out_lines.append(line)
        code = o.channel.recv_exit_status()
        if code != 0:
            return {"ok": False, "error": "Установка на сервере не удалась.", "log": "\n".join(out_lines[-15:])}
        code, text = run("cd /opt/sometroy && venv/bin/python bot.py --set-password", stdin_text=password_holder["pw"] + "\n")
        if code != 0:
            return {"ok": False, "error": "Не получилось задать пароль программы: " + text[-300:]}
        with sftp.open("/opt/sometroy/tls/cert.pem") as f:
            cert = f.read().decode()
        return {"ok": True, "port": port, "cert": cert, "log": "\n".join(out_lines[-15:])}
    finally:
        cli.close()


password_holder = {"pw": ""}     # пароль программы на время установки (в файл не пишется)


def fetch_cert(ip: str, port: int, fingerprint: str):
    """Скачать сертификат сервера и сверить с отпечатком из кода /connect. (pem, None) или (None, причина)."""
    import base64
    import hashlib
    import ssl
    try:
        pem = ssl.get_server_certificate((ip, port), timeout=15)
    except (OSError, ssl.SSLError):
        return None, (f"Не достучался до {ip}:{port}. Проверь, что сервер включён и порт {port} открыт "
                      "(в панели хостинга, если там есть файрвол).")
    got = base64.urlsafe_b64encode(hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).digest()).decode().rstrip("=")
    if got != fingerprint:
        return None, ("⚠️ Сервер по этому адресу — не твой Трой: сертификат не совпал с кодом. "
                      "Возьми свежий код командой /connect.")
    return pem, None


def server_reachable(ip: str, port: int, cert_path: Path) -> bool:
    try:
        r = requests.get(f"https://{ip}:{port}/api/ping", timeout=10, verify=str(cert_path))
        return r.status_code in (200, 401)
    except requests.RequestException:
        return False


# ───────────────────────── сценарий ─────────────────────────

class Wizard:
    """Шаги: режим → Groq → Telegram → хозяин → имя → пароль → доп. модули → часовой пояс → установка."""

    def __init__(self, finish_local: Callable[[str, str], None], finish_vps: Callable[[dict], None],
                 template: str, src_dir: Path, data_dir: Path,
                 finish_connect: Optional[Callable[[dict], Optional[str]]] = None):
        self.finish_local, self.finish_vps, self.finish_connect = finish_local, finish_vps, finish_connect
        self.template, self.src_dir, self.data_dir = template, src_dir, data_dir
        self.v: dict = {}               # то, что пойдёт в settings.env
        self.state = "mode"
        self.mode = ""
        self.bot = ""
        self.password = ""
        self.vps: dict = {}

    # ── вход ──
    def start(self) -> List[dict]:
        a, b = random.sample(FILMS, 2)
        self.state = "mode"
        return [msg(f"Привет, я Трой! Возможно, вы помните меня по таким фильмам, как {a} и {b}.\n\n"
                    "Сейчас я помогу себя настроить — это минут пять. Всё бесплатно: понадобятся только "
                    "Telegram и бесплатный ключ Groq. Ключи и пароли, которые ты введёшь, проверяю у самих "
                    "сервисов и храню только в настройках — нейросеть их не видит."),
                msg("Где мне жить?\n\n"
                    "- **На этом компьютере** — проще всего, ничего не нужно. Но я работаю, только пока компьютер включён.\n"
                    "- **На моём сервере (VPS)** — если у тебя есть свой сервер на Ubuntu или Debian: "
                    "понадобятся его адрес и пароль.\n"
                    "- **У меня уже есть Трой** — подключить этот компьютер к Трою, который уже живёт на твоём сервере.",
                    ["На этом компьютере", "На моём сервере (VPS)", "У меня уже есть Трой"])]

    def handle(self, text: str) -> List[dict]:
        text = (text or "").strip()
        try:
            return getattr(self, "s_" + self.state)(text)
        except Exception as e:      # мастер не должен падать — объясняем и остаёмся на шаге
            log.exception("Мастер, шаг %s", self.state)
            return [msg(f"Что-то пошло не так: {e}. Попробуй ещё раз.")]

    # ── шаги ──
    def s_mode(self, t: str):
        low = t.lower()
        if "уже есть" in low or "подключ" in low:
            self.state = "connect"
            return [msg("Подключим этот компьютер. Напиши своему Трою в Telegram команду /connect — он пришлёт код "
                        "вида troy://… Скопируй его целиком сюда.\n\nВ коде нет паролей: это адрес сервера и его "
                        "«отпечаток», чтобы я точно подключился к твоему Трою, а не к подделке.",
                        ["Назад"], placeholder="troy://…")]
        if "компьютер" in low:
            self.mode = "local"
        elif "сервер" in low or "vps" in low:
            self.mode = "vps"
        else:
            return [msg("Выбери кнопкой, где мне жить.", ["На этом компьютере", "На моём сервере (VPS)", "У меня уже есть Трой"])]
        self.state = "groq"
        return [msg("Отлично. Шаг 1 из 5 — мозги.\n\n"
                    "Я думаю с помощью бесплатных моделей Groq. Нужен ключ:\n"
                    "1. Открой https://console.groq.com/keys и войди (Google или почта, карта не нужна).\n"
                    "2. Нажми **Create API Key**, назови как угодно, скопируй ключ (начинается с gsk_).\n"
                    "3. Вставь его сюда.", secret=True, placeholder="gsk_…")]

    def s_groq(self, t: str):
        err = check_groq(t)
        if err:
            return [msg(err, secret=True, placeholder="gsk_…")]
        self.v["GROQ_API_KEY"] = t
        self.state = "telegram"
        return [msg("✅ Ключ Groq работает.\n\nШаг 2 из 5 — Telegram. Мне нужен свой бот:\n"
                    "1. В Telegram открой @BotFather и отправь ему /newbot.\n"
                    "2. Придумай имя и адрес бота (адрес заканчивается на bot).\n"
                    "3. BotFather пришлёт токен вида 123456789:ABC… — скопируй его сюда.",
                    secret=True, placeholder="123456789:ABC…")]

    def s_telegram(self, t: str):
        name, err, need_proxy = check_telegram(t, self.v.get("TELEGRAM_PROXY", ""))
        if err and need_proxy:
            self.v["TELEGRAM_TOKEN"] = t
            self.state = "proxy"
            return [msg(err + " Похоже, Telegram у тебя открывается только через прокси или VPN.\n\n"
                        "Если есть прокси — вставь адрес (socks5://… или http://…). Если включил VPN — нажми «Проверить ещё раз».",
                        ["Проверить ещё раз"], placeholder="socks5://…")]
        if err:
            return [msg(err, secret=True, placeholder="123456789:ABC…")]
        self.v["TELEGRAM_TOKEN"], self.bot = t, name
        self.state = "owner"
        return [msg(f"✅ Нашёл твоего бота @{name}.\n\nШаг 3 из 5 — познакомимся. Открой https://t.me/{name}, "
                    "нажми **Запустить** (или напиши что угодно) и потом нажми кнопку ниже. "
                    "Так я узнаю, что ты — мой хозяин, и буду отвечать только тебе.", ["Написал"])]

    def s_proxy(self, t: str):
        if t.lower() != "проверить ещё раз":
            if not re.match(r"(socks5h?|https?)://\S+$", t):
                return [msg("Адрес прокси выглядит так: socks5://адрес:порт или http://адрес:порт", ["Проверить ещё раз"])]
            self.v["TELEGRAM_PROXY"] = t
        self.state = "telegram"
        return self.s_telegram(self.v.pop("TELEGRAM_TOKEN"))

    def s_owner(self, t: str):
        low = t.lower()
        if self.v.get("ALLOWED_USER_ID") and low == "да":
            return self._ask_name()
        if low.startswith("нет"):               # написал кто-то другой — забываем и ждём хозяина
            self.v.pop("ALLOWED_USER_ID", None)
            self.skip_updates = getattr(self, "last_update", 0) + 1
            return [msg(f"Понял. Напиши боту https://t.me/{self.bot} со **своего** аккаунта Telegram и нажми кнопку.",
                        ["Написал"])]
        owner = find_owner(self.v["TELEGRAM_TOKEN"], self.v.get("TELEGRAM_PROXY", ""),
                           getattr(self, "skip_updates", 0))
        if not owner:
            return [msg(f"Пока не вижу сообщений. Открой https://t.me/{self.bot}, нажми «Запустить» "
                        "или напиши что угодно — и снова нажми кнопку.", ["Написал"])]
        self.v["ALLOWED_USER_ID"] = owner["id"]
        self.owner_name, self.last_update = owner["name"], owner["update"]
        return [msg(f"Вижу сообщение от **{owner['name'] or 'без имени'}**. Это ты?", ["Да", "Нет, это не я"])]

    def _ask_name(self):
        try:
            tg_call(self.v["TELEGRAM_TOKEN"], "sendMessage", self.v.get("TELEGRAM_PROXY", ""),
                    chat_id=self.v["ALLOWED_USER_ID"], text="Привет! Это Трой. Настройка почти закончена — вернись в программу 🙂")
        except Exception:
            pass
        self.state = "name"
        name = getattr(self, "owner_name", "")
        return [msg("✅ Запомнил: отвечаю только тебе. Я написал тебе в Telegram — проверь.\n\nКак к тебе обращаться?",
                    ([name] if name else []) + ["Не важно"], placeholder="Имя")]

    def s_name(self, t: str):
        if t and t.lower() != "не важно":
            self.v["USER_NAME"] = t[:40]
        self.state = "password"
        return [msg("Шаг 4 из 5 — пароль для этой программы. Его спрошу при входе и на других компьютерах, "
                    "где поставишь Троя. Минимум 6 символов.", secret=True, placeholder="Пароль")]

    def s_password(self, t: str):
        if len(t) < 6:
            return [msg("Нужно хотя бы 6 символов.", secret=True, placeholder="Пароль")]
        self.password = t
        self.state = "password2"
        return [msg("Повтори пароль, чтобы не ошибиться.", secret=True, placeholder="Пароль ещё раз")]

    def s_password2(self, t: str):
        if t != self.password:
            self.state = "password"
            return [msg("Пароли не совпали. Давай ещё раз — придумай пароль.", secret=True, placeholder="Пароль")]
        return self._extras("✅ Пароль запомнил.\n\n")

    def _extras(self, prefix: str = ""):
        """Шаг 5: запасная модель Mistral — по желанию."""
        self.state = "extras"
        return [msg(prefix + "Шаг 5 из 5 — запас на всякий случай (по желанию).\n\n"
                    "У бесплатного Groq есть дневные лимиты. Если подключить ещё и бесплатный **Mistral**, "
                    "я не замолчу, когда лимит Groq закончится. Подключим?",
                    ["Подключить Mistral", "Пропустить"])]

    def s_extras(self, t: str):
        if "mistral" in t.lower() or t.lower().startswith("подключ"):
            self.state = "mistral"
            return [msg("Ключ Mistral: открой https://console.mistral.ai/api-keys, войди, выбери бесплатный план "
                        "(Experiment), нажми **Create new key** и вставь ключ сюда.", ["Пропустить"], secret=True)]
        return self._ask_bitrix()

    def s_mistral(self, t: str):
        if t.lower() in ("пропустить", "назад"):
            return self._ask_bitrix()
        err = check_mistral(t)
        if err:
            return [msg(err, ["Пропустить"], secret=True)]
        self.v["MISTRAL_API_KEY"] = t
        return self._ask_bitrix("✅ Mistral подключён.\n\n")

    def _ask_bitrix(self, prefix: str = ""):
        self.state = "bitrix_q"
        return [msg(prefix + "Пользуешься **Битрикс24** на работе? Могу смотреть задачи и чаты, искать в CRM "
                    "и считать аналитику — а всё, что меняет, делать только после твоего «да».",
                    ["Да, подключить", "Нет"])]

    def s_bitrix_q(self, t: str):
        if t.lower().startswith("да"):
            self.state = "bitrix"
            return [msg("Вебхук Битрикс24: в портале **Приложения → Разработчикам → Другое → Входящий вебхук**, "
                        "права: task, im, crm, user. Скопируй адрес вебхука целиком и вставь сюда.", ["Пропустить"],
                        secret=True, placeholder="https://…bitrix24.ru/rest/1/…/")]
        return self._ask_zone()

    def s_bitrix(self, t: str):
        if t.lower() in ("пропустить", "назад"):
            return self._ask_zone()
        err = check_bitrix(t)
        if err:
            return [msg(err, ["Пропустить"], secret=True, placeholder="https://…bitrix24.ru/rest/1/…/")]
        self.v["BITRIX_WEBHOOK"] = t.rstrip("/") + "/"
        return self._ask_zone("✅ Битрикс24 подключён.\n\n")

    def _ask_zone(self, prefix: str = ""):
        city, zone = local_zone()
        self.zone_guess = zone
        self.state = "zone"
        return [msg(prefix + f"Последнее: часовой пояс для напоминаний. По часам компьютера это **{city}**. Верно?",
                    [f"Да, {city}", "Другой"])]

    def s_zone(self, t: str):
        from zoneinfo import ZoneInfo
        if t.lower().startswith("да"):
            self.v["TIMEZONE"] = self.zone_guess
        elif t.lower() == "другой":
            return [msg("Напиши город из списка или зону в формате Europe/Moscow:\n" +
                        ", ".join(c for c, _ in ZONES.values()), placeholder="Город или Europe/…")]
        else:
            zone = next((z for c, z in ZONES.values() if c.lower() == t.lower()), t)
            try:
                ZoneInfo(zone)
            except Exception:
                return [msg("Не знаю такой зоны. Напиши город из списка или зону вида Europe/Moscow.")]
            self.v["TIMEZONE"] = zone
        if self.mode == "local":
            return self._finish_local()
        self.state = "vps_ip"
        return [msg("Теперь данные сервера. IP-адрес (например 203.0.113.10):",
                    placeholder="IP-адрес")]

    def _settings(self) -> str:
        return settings_text(self.template, self.v)

    def _finish_local(self):
        self.finish_local(self._settings(), self.password)
        self.state = "done"
        return [msg("🎉 Готово! Я живу на этом компьютере. Сейчас перезапущусь и войду — дальше просто пиши мне "
                    "здесь или в Telegram.\n\nПодсказка: в установщике можно включить «Запускать вместе с Windows» — "
                    "тогда я всегда на связи, пока компьютер включён.", done=True)]

    def s_vps_ip(self, t: str):
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", t):
            return [msg("Нужен IP-адрес из четырёх чисел через точку, например 203.0.113.10.", placeholder="IP-адрес")]
        self.vps["ip"] = t
        self.state = "vps_user"
        return [msg("Пользователь на сервере — обычно root.", ["root"], placeholder="Пользователь")]

    def s_vps_user(self, t: str):
        self.vps["user"] = t or "root"
        self.state = "vps_pass"
        return [msg("Пароль сервера. Он нужен только сейчас, чтобы меня установить, "
                    "и нигде не сохраняется.", secret=True, placeholder="Пароль сервера")]

    def s_vps_pass(self, t: str):
        progress: List[str] = []
        password_holder["pw"] = self.password
        try:
            res = install_vps(self.vps["ip"], self.vps["user"], t, self._settings(), self.src_dir, progress.append)
        finally:
            password_holder["pw"] = ""
        out = [msg(p) for p in progress]
        if not res.get("ok"):
            self.state = "vps_pass" if "пароль" in res.get("error", "").lower() else "vps_ip"
            tail = f"\n\nПоследние строки установки:\n{res['log']}" if res.get("log") else ""
            again = ("Попробуй пароль ещё раз." if self.state == "vps_pass"
                     else "Напиши IP-адрес ещё раз, когда исправишь.")
            return out + [msg(f"⚠️ {res['error']}{tail}\n\n{again}", secret=self.state == "vps_pass")]
        self.vps.update(port=res["port"], cert=res["cert"])
        self.finish_vps({"ip": self.vps["ip"], "port": res["port"], "cert": res["cert"], "password": self.password})
        cert_path = self.data_dir / "troy-cert.pem"
        warn = "" if server_reachable(self.vps["ip"], res["port"], cert_path) else (
            f"\n\n⚠️ Программа пока не достучалась до сервера по порту {res['port']}. Если у хостинга есть "
            f"файрвол в панели управления — открой там порт {res['port']} (TCP). Telegram работает и без этого.")
        self.state = "done"
        return out + [msg(f"🎉 Готово! Я живу на сервере {self.vps['ip']}.{warn}\n\n"
                          "Сейчас перезапущусь и войду. Удалить меня с сервера можно одной командой: "
                          "`bash /opt/sometroy/uninstall.sh`", done=True)]

    def s_connect(self, t: str):
        if t.lower() == "назад":
            return self.start()[1:]
        m = re.fullmatch(r"troy://(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})#([\w-]{40,})", t.strip())
        if not m:
            return [msg("Код выглядит так: troy://203.0.113.10:8443#… — скопируй его из Telegram целиком "
                        "(команда /connect).", ["Назад"], placeholder="troy://…")]
        ip, port, fp = m.group(1), int(m.group(2)), m.group(3)
        cert, err = fetch_cert(ip, port, fp)
        if err:
            return [msg(err, ["Назад"], placeholder="troy://…")]
        self.vps = {"ip": ip, "port": port, "cert": cert}
        self.state = "connect_pass"
        return [msg(f"✅ Нашёл твоего Троя на {ip}, сертификат совпал. Введи пароль программы — тот же, "
                    "что на первом компьютере (забыл — задай новый в Telegram: /password новый_пароль).",
                    secret=True, placeholder="Пароль")]

    def s_connect_pass(self, t: str):
        err = self.finish_connect({**self.vps, "password": t}) if self.finish_connect else "нет подключения"
        if err:
            return [msg(err, secret=True, placeholder="Пароль")]
        self.state = "done"
        return [msg("🎉 Готово! Этот компьютер подключён к твоему Трою. Сейчас перезапущусь — переписка, "
                    "напоминания и идеи те же, что и везде.", done=True)]

    def s_done(self, t: str):
        return [msg("Настройка уже закончена — перезапускаюсь.", done=True)]


# ───────────────────────── подробные инструкции к шагам ─────────────────────────
# Открываются в отдельном окне по ссылке «Подробная инструкция» под сообщением Троя.

HELP = {
    "mode": ("Где будет жить Трой", """
**На этом компьютере** — самый простой вариант.
- Ничего дополнительно не нужно.
- Трой работает, пока компьютер включён и программа запущена (можно свёрнутой в трей у часов).
- Когда компьютер выключен, Трой в Telegram не отвечает, а напоминания придут, как только он снова включится.

**На моём сервере (VPS)** — если у тебя уже есть свой сервер.
- Подойдёт Ubuntu 22.04/24.04 или Debian; Трою хватает 1 ядра и 1 ГБ памяти.
- В конце настройки Трой спросит адрес сервера, пользователя и пароль, сам подключится и всё установит.
  Пароль сервера нигде не сохраняется.

**У меня уже есть Трой** — если Трой уже живёт на твоём сервере, а это ещё один компьютер.
Понадобится код из Telegram (команда /connect) и пароль программы.

Передумать можно потом: настройку легко пройти заново.
"""),
    "groq": ("Ключ Groq — бесплатные «мозги» Троя", """
Groq — сервис, где бесплатно работают нейросети, которыми думает Трой. Карта не нужна.

1. Открой https://console.groq.com/keys
2. Войди: через Google, GitHub или по почте (придёт письмо со ссылкой).
3. Нажми **Create API Key**. Имя — любое, например «Трой».
4. Появится ключ, он начинается с **gsk_**. Нажми **Copy** — ключ показывают только один раз.
5. Вернись в Троя и вставь ключ в поле ввода (Ctrl+V), затем Enter.

**Если что-то не так**
- Сайт не открывается — попробуй включить VPN.
- Закрыл окно, не скопировав ключ, — не страшно: создай новый (Create API Key ещё раз).
- У бесплатного Groq есть лимиты в минуту и в сутки. Трой сам переключается между несколькими моделями,
  а если хочешь запас — на шаге 5 можно подключить бесплатный Mistral.

Ключ — как пароль: никому его не показывай.
"""),
    "telegram": ("Свой бот в Telegram", """
Трой разговаривает с тобой через своего Telegram-бота. Создать его — минута, бесплатно.

1. Открой @BotFather: https://t.me/BotFather (официальный бот Telegram для создания ботов, с синей галочкой).
2. Нажми **Запустить** (Start) и отправь команду **/newbot**.
3. BotFather спросит **имя** — оно видно в списке чатов, например «Мой Трой».
4. Потом **адрес** (username) — латиницей и обязательно на **bot** в конце, например my_troy_2026_bot.
   Если адрес занят, придумай другой.
5. BotFather пришлёт сообщение с токеном вида **123456789:AAH…** — скопируй его целиком.
6. Вставь токен в Троя.

**Если что-то не так**
- Потерял токен — напиши BotFather /mybots → выбери бота → **API Token**.
- Токен — ключ от бота: кто его знает, тот может писать от имени бота. Никому не показывай.
"""),
    "proxy": ("Telegram через прокси", """
Похоже, Telegram с этого компьютера открывается только через VPN или прокси.

- **Есть VPN** — включи его и нажми «Проверить ещё раз».
- **Есть прокси** — вставь его адрес в одном из видов:
  - socks5://адрес:порт
  - socks5://логин:пароль@адрес:порт
  - http://адрес:порт

Прокси будет нужен Трою постоянно, чтобы он мог переписываться с Telegram.
"""),
    "owner": ("Знакомство: кто хозяин", """
Трой должен отвечать только тебе. Для этого он один раз смотрит, кто ему написал.

1. Открой своего бота по ссылке из сообщения (или найди его в Telegram по адресу).
2. Нажми **Запустить** (Start) или напиши что угодно, например «привет».
3. Вернись в Троя и нажми **Написал**.

Трой покажет имя — нажми **Да**, если это ты. Если написал кто-то другой, нажми «Нет» и напиши боту
со своего аккаунта.

**Не видит сообщение?**
- Проверь, что пишешь именно своему новому боту (адрес — тот, что ты придумал у BotFather).
- Подожди пару секунд и нажми «Написал» ещё раз.
"""),
    "name": ("Как к тебе обращаться", """
Трой будет называть тебя этим именем. Можно нажать кнопку с именем из Telegram, написать своё
или нажать «Не важно».
"""),
    "password": ("Пароль программы", """
Этот пароль защищает программу Трой: его спросят при входе на этом и на других компьютерах.

- Минимум 6 символов. Лучше не использовать пароль от почты или банка.
- Введёшь дважды — чтобы не ошибиться.
- Забудешь — не страшно: напиши боту в Telegram /password новый_пароль, и пароль сменится
  (это сообщение бот сразу удаляет).
"""),
    "password2": ("Пароль программы", """
Повтори тот же пароль ещё раз — так Трой убедится, что опечатки нет.
Если не совпадёт, он попросит придумать пароль заново.
"""),
    "extras": ("Запасная модель Mistral", """
Mistral — ещё один бесплатный сервис с нейросетями. Он нужен как запас: когда у Groq закончился
дневной лимит, Трой ответит через Mistral, а не замолчит.

Можно пропустить и подключить потом. Ключ берётся здесь: https://console.mistral.ai/api-keys
(при входе выбери бесплатный план **Experiment**; может попросить подтвердить номер телефона).
"""),
    "bitrix_q": ("Битрикс24", """
Если ты пользуешься Битрикс24 (обычно — на работе), Трой может:
- показывать твои задачи и непрочитанные сообщения;
- искать сделки, контакты и компании в CRM;
- считать аналитику: например, сколько сделок закрыли за месяц по менеджерам;
- ставить задачи, писать в чаты и менять CRM — но только после твоего «да».

Трой работает от твоего имени и с твоими правами. Не пользуешься Битриксом — нажми «Нет», этот шаг пропустится.
"""),
    "mistral": ("Ключ Mistral", """
1. Открой https://console.mistral.ai/api-keys и войди (Google или почта).
2. Если спросят про план — выбери бесплатный **Experiment** (может понадобиться подтвердить телефон).
3. Нажми **Create new key**, имя любое, срок — без ограничения.
4. Скопируй ключ и вставь в Троя.

Бесплатный Mistral может использовать переписку для улучшения своих моделей. Если это важно —
отключи в настройках Mistral (Admin → Privacy).
"""),
    "bitrix": ("Вебхук Битрикс24", """
Вебхук — это ключ, по которому Трой работает в Битриксе от твоего имени и с твоими правами.

1. В своём портале Битрикс24 открой **Приложения → Разработчикам**.
2. Вкладка **Другое → Входящий вебхук**.
3. Отметь права: **Задачи (task)**, **Чаты и уведомления (im)**, **CRM (crm)**, **Пользователи (user)**.
4. Нажми **Сохранить** и скопируй адрес вебхука целиком — вида https://портал.bitrix24.ru/rest/1/abc123…/
5. Вставь его в Троя.

Если раздела «Разработчикам» нет — нужны права администратора портала, попроси администратора.
"""),
    "zone": ("Часовой пояс", """
Нужен, чтобы напоминания приходили вовремя. Трой определил его по часам компьютера.
Если неверно — нажми «Другой» и напиши город из списка или зону в формате Europe/Moscow.
"""),
    "vps_ip": ("IP-адрес сервера", """
IP-адрес — четыре числа через точку, например 203.0.113.10.

Где найти: в данных для доступа к серверу или в панели управления сервером (поле IP, IPv4 или «Адрес»).

Нужен сервер на **Ubuntu** (22.04 или 24.04) или **Debian**.
"""),
    "vps_user": ("Пользователь сервера", """
Почти всегда это **root** — просто нажми кнопку. Другое имя нужно, только если хостинг выдал
пользователя с правами администратора под другим именем.
"""),
    "vps_pass": ("Пароль сервера", """
Пароль пользователя root — тот, с которым ты входишь на сервер.

Что произойдёт дальше (1–3 минуты):
- Трой подключится к серверу, установит себя в папку /opt/sometroy и запустит как службу;
- сам выберет свободный порт, если стандартный занят другой программой;
- задаст пароль программы и сразу войдёт.

Пароль сервера используется только сейчас и **нигде не сохраняется**.

Если у сервера есть внешний файрвол (в панели управления) — после установки, возможно, понадобится
открыть порт, который назовёт Трой (TCP). Удалить Троя с сервера: bash /opt/sometroy/uninstall.sh
"""),
    "connect": ("Подключить ещё один компьютер", """
Так можно пользоваться одним Троем с нескольких компьютеров, если он живёт на твоём сервере.

1. Напиши своему Трою в Telegram команду **/connect** (или в программе на первом компьютере).
2. Он пришлёт код вида troy://203.0.113.10:8443#… — скопируй его целиком.
3. Вставь код сюда, затем введи пароль программы — тот же, что на первом компьютере.

В коде нет паролей: только адрес сервера и «отпечаток» его сертификата. По нему программа проверяет,
что подключается именно к твоему Трою.

Если Трой живёт на компьютере (локальный режим), подключить другие компьютеры нельзя.
"""),
    "connect_pass": ("Пароль программы", """
Это пароль, который ты придумал при первой настройке Троя.
Забыл — напиши боту в Telegram /password новый_пароль, и введи новый здесь.
"""),
}


def _attach_help(self, msgs: List[dict]) -> List[dict]:
    """К последнему сообщению — ссылка на подробную инструкцию к текущему шагу."""
    h = HELP.get(self.state)
    if msgs and h and not msgs[-1].get("done"):
        text = re.sub(r"\n {2,}(?=\S)", " ", h[1].strip())       # продолжение пункта — в ту же строку
        msgs[-1]["help"] = {"title": h[0], "text": text}
    return msgs


_start, _handle = Wizard.start, Wizard.handle
Wizard.start = lambda self: _attach_help(self, _start(self))
Wizard.handle = lambda self, text: _attach_help(self, _handle(self, text))
