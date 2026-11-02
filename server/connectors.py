"""Подключения Троя к другим сервисам: календари, почта, умный дом, расписания.

Каждое подключение человек включает сам в программе (раздел «Подключения»): вводит доступы,
Трой сразу проверяет их у сервиса и сохраняет в своей базе. Доступы не уходят в нейросеть —
модель видит только функции («какие события завтра») и их результаты.

Подключение:
  id, title, about       — что это и зачем
  fields                 — какие данные спросить (secret — скрывать при вводе)
  help                   — подробная инструкция (где взять ключ)
  hint                   — когда в разговоре это может понадобиться (чтобы не тратить лимит)
  test(cfg)              — проверка доступа → None или понятная причина
  tools                  — функции для модели
  run(cfg, name, args)   — выполнить функцию → текст
"""
import email
import email.header
import email.utils
import imaplib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("assistant.connectors")
UA = {"User-Agent": "SomeTroy/0.1 (+https://github.com/estark407-max/SomeTroy)"}


def fn(name: str, description: str, props: Optional[dict] = None, required: Optional[list] = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}


def _err(e: BaseException) -> str:
    if isinstance(e, requests.exceptions.Timeout):
        return "сервис долго не отвечает"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "нет соединения с сервисом — проверь адрес и интернет"
    return str(e)[:200]


# ═════════════════════════ Календарь: разбор iCalendar (ICS) ═════════════════════════

def _unfold(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_dt(value: str, params: dict, tz) -> tuple:
    """(datetime в tz, весь_день?)"""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value[:8], "%Y%m%d")
        return d.replace(tzinfo=tz), True
    if value.endswith("Z"):
        return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).astimezone(tz), False
    src = tz
    if params.get("TZID"):
        try:
            src = ZoneInfo(params["TZID"].strip('"'))
        except Exception:
            src = tz
    return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=src).astimezone(tz), False


def _unescape(v: str) -> str:
    return v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def parse_ics(text: str, tz) -> List[dict]:
    """События: summary, start, end, all_day, location, rrule, exdates, recurrence_id, uid."""
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"exdates": set()}
            continue
        if line == "END:VEVENT":
            if cur and cur.get("start"):
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *ps = head.split(";")
        params = dict(p.split("=", 1) for p in ps if "=" in p)
        name = name.upper()
        try:
            if name == "DTSTART":
                cur["start"], cur["all_day"] = _parse_dt(value, params, tz)
            elif name == "DTEND":
                cur["end"], _ = _parse_dt(value, params, tz)
            elif name == "DURATION":
                m = re.fullmatch(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
                if m:
                    w, d, h, mi, s = (int(x or 0) for x in m.groups())
                    cur["duration"] = timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)
            elif name == "SUMMARY":
                cur["summary"] = _unescape(value)
            elif name == "LOCATION":
                cur["location"] = _unescape(value)
            elif name == "RRULE":
                cur["rrule"] = dict(p.split("=", 1) for p in value.split(";") if "=" in p)
            elif name == "EXDATE":
                for v in value.split(","):
                    cur["exdates"].add(_parse_dt(v, params, tz)[0].replace(second=0))
            elif name == "RECURRENCE-ID":
                cur["recurrence_id"], _ = _parse_dt(value, params, tz)
            elif name == "UID":
                cur["uid"] = value.strip()
            elif name == "STATUS" and value.strip().upper() == "CANCELLED":
                cur["cancelled"] = True
        except ValueError:
            continue
    for e in events:
        if "end" not in e:
            e["end"] = e["start"] + e.get("duration", timedelta(days=1) if e.get("all_day") else timedelta(hours=1))
    return events


_WD = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _occurrences(ev: dict, a: datetime, b: datetime, tz) -> List[datetime]:
    """Начала повторений события в [a, b) — частые правила: DAILY/WEEKLY/MONTHLY/YEARLY, INTERVAL, COUNT, UNTIL, BYDAY."""
    start, rule = ev["start"], ev.get("rrule")
    dur = ev["end"] - start
    if not rule:
        return [start] if start < b and start + dur > a else []
    freq, interval = rule.get("FREQ", ""), max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        try:
            until = _parse_dt(rule["UNTIL"], {}, tz)[0]
        except ValueError:
            pass
    byday = [_WD[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in _WD]
    out, n, i = [], 0, 0
    cur = start
    while i < 5000:
        i += 1
        cands = [cur]
        if freq == "WEEKLY" and byday:
            monday = cur - timedelta(days=cur.weekday())
            cands = sorted(monday + timedelta(days=d) for d in byday)
            cands = [c for c in cands if c >= start]
        for c in cands:
            if (until and c > until) or (count is not None and n >= count):
                return out
            n += 1
            if c + dur > a and c < b and c.replace(second=0) not in ev["exdates"]:
                out.append(c)
        if cands and min(cands) >= b:
            return out
        if freq == "DAILY":
            cur += timedelta(days=interval)
        elif freq == "WEEKLY":
            cur += timedelta(weeks=interval)
        elif freq == "MONTHLY":
            y, m = divmod(cur.month - 1 + interval, 12)
            try:
                cur = cur.replace(year=cur.year + y, month=m + 1)
            except ValueError:
                cur = cur.replace(year=cur.year + y, month=m + 1, day=28)
        elif freq == "YEARLY":
            cur = cur.replace(year=cur.year + interval)
        else:
            return out
        if cur > b + timedelta(days=7) and not byday:
            return out
    return out


def events_between(events: List[dict], a: datetime, b: datetime, tz) -> List[dict]:
    moved = {(e.get("uid"), e["recurrence_id"].replace(second=0)) for e in events if e.get("recurrence_id")}
    out = []
    for e in events:
        if e.get("cancelled"):
            continue
        dur = e["end"] - e["start"]
        for s in _occurrences(e, a, b, tz):
            if not e.get("recurrence_id") and (e.get("uid"), s.replace(second=0)) in moved:
                continue
            out.append({**e, "start": s, "end": s + dur})
    return sorted(out, key=lambda x: x["start"])


def format_events(evs: List[dict]) -> str:
    if not evs:
        return "Событий нет."
    lines = []
    for e in evs[:60]:
        when = (f"{e['start']:%d.%m} весь день" if e.get("all_day")
                else f"{e['start']:%d.%m %H:%M}–{e['end']:%H:%M}")
        lines.append(f"- {when}: {e.get('summary') or '(без названия)'}" + (f" ({e['location']})" if e.get("location") else ""))
    return "\n".join(lines)


def _period(args: dict, tz) -> tuple:
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        a = datetime.strptime(str(args.get("date_from") or ""), "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        a = today
    try:
        b = datetime.strptime(str(args.get("date_to") or ""), "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=1)
    except ValueError:
        b = a + timedelta(days=1)
    if b <= a or (b - a).days > 62:
        b = a + timedelta(days=1)
    return a, b


PERIOD_PROPS = {"date_from": {"type": "string", "description": "с какой даты, YYYY-MM-DD (по умолчанию сегодня)"},
                "date_to": {"type": "string", "description": "по какую дату включительно, YYYY-MM-DD"}}


# ═════════════════════════ 1. Календарь по ссылке (ICS) ═════════════════════════

def ics_fetch(url: str) -> str:
    url = url.strip().replace("webcal://", "https://", 1)
    r = requests.get(url, timeout=25, headers=UA)
    r.raise_for_status()
    text = r.content.decode("utf-8", "replace")          # календари — всегда UTF-8, даже если сервер не сказал
    if "BEGIN:VCALENDAR" not in text[:2000]:
        raise ValueError("по ссылке не календарь (нужна ссылка в формате iCal, обычно оканчивается на .ics)")
    return text


def ics_test(cfg: dict) -> Optional[str]:
    try:
        ics_fetch(cfg["url"])
    except requests.HTTPError as e:
        return f"ссылка не открылась (ошибка {e.response.status_code}) — скопируй её заново"
    except (requests.RequestException, ValueError) as e:
        return _err(e) if isinstance(e, requests.RequestException) else str(e)
    return None


def ics_run(cfg: dict, name: str, args: dict, tz) -> str:
    a, b = _period(args, tz)
    evs = events_between(parse_ics(ics_fetch(cfg["url"]), tz), a, b, tz)
    return f"События {a:%d.%m}–{(b - timedelta(days=1)):%d.%m}:\n" + format_events(evs)


# ═════════════════════════ 2. Календарь CalDAV (Яндекс, iCloud, Mail.ru, Nextcloud) ═════════════════════════

CALDAV_SERVERS = {"yandex": "https://caldav.yandex.ru", "icloud": "https://caldav.icloud.com",
                  "mail.ru": "https://calendar.mail.ru", "nextcloud": ""}
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "cs": "http://calendarserver.org/ns/"}


class CalDav:
    def __init__(self, cfg: dict):
        self.base = (cfg.get("server") or CALDAV_SERVERS["yandex"]).rstrip("/")
        self.auth = (cfg["login"], cfg["password"])

    def _req(self, method: str, url: str, body: str = "", depth: str = "0", ctype="application/xml; charset=utf-8"):
        if not url.startswith("http"):
            m = re.match(r"https?://[^/]+", self.base)
            url = (m.group(0) if m else self.base) + url
        r = requests.request(method, url, data=body.encode("utf-8"), auth=self.auth, timeout=25,
                             headers={**UA, "Depth": depth, "Content-Type": ctype})
        if r.status_code == 401:
            raise PermissionError("логин или пароль приложения не подошли")
        if r.status_code >= 400:
            raise RuntimeError(f"сервер календаря ответил ошибкой {r.status_code}")
        return r

    def _href(self, xml: bytes, prop: str) -> Optional[str]:
        root = ET.fromstring(xml)
        el = root.find(f".//{prop}/d:href", NS)
        return el.text.strip() if el is not None and el.text else None

    def calendars(self) -> List[dict]:
        body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop>'
                '</d:propfind>')
        principal = self._href(self._req("PROPFIND", self.base + "/", body).content, "d:current-user-principal") or "/"
        body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                '<d:prop><c:calendar-home-set/></d:prop></d:propfind>')
        home = self._href(self._req("PROPFIND", principal, body).content, "c:calendar-home-set") or principal
        body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                '<d:prop><d:displayname/><d:resourcetype/><c:supported-calendar-component-set/></d:prop></d:propfind>')
        root = ET.fromstring(self._req("PROPFIND", home, body, depth="1").content)
        out = []
        for resp in root.findall("d:response", NS):
            if resp.find(".//d:resourcetype/c:calendar", NS) is None:
                continue
            comps = [c.get("name") for c in resp.findall(".//c:supported-calendar-component-set/c:comp", NS)]
            if comps and "VEVENT" not in comps:
                continue
            name = resp.findtext(".//d:displayname", default="", namespaces=NS) or "Календарь"
            out.append({"href": resp.findtext("d:href", namespaces=NS).strip(), "name": name})
        return out

    def events(self, a: datetime, b: datetime, tz) -> List[dict]:
        ts = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        body = ('<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                '<d:prop><c:calendar-data/></d:prop><c:filter><c:comp-filter name="VCALENDAR">'
                f'<c:comp-filter name="VEVENT"><c:time-range start="{ts(a)}" end="{ts(b)}"/></c:comp-filter>'
                '</c:comp-filter></c:filter></c:calendar-query>')
        out = []
        for cal in self.calendars():
            root = ET.fromstring(self._req("REPORT", cal["href"], body, depth="1").content)
            for data in root.iter("{urn:ietf:params:xml:ns:caldav}calendar-data"):
                if data.text:
                    out += events_between(parse_ics(data.text, tz), a, b, tz)
        return sorted(out, key=lambda x: x["start"])

    def create(self, summary: str, start: datetime, end: datetime, location: str = "") -> str:
        cals = self.calendars()
        if not cals:
            raise RuntimeError("в аккаунте не нашлось календаря")
        uid = f"{int(time.time() * 1000)}-sometroy"
        ts = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        esc = lambda s: s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
        ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//SomeTroy//RU\r\nBEGIN:VEVENT\r\n"
               f"UID:{uid}\r\nDTSTAMP:{ts(datetime.now(timezone.utc))}\r\nDTSTART:{ts(start)}\r\nDTEND:{ts(end)}\r\n"
               f"SUMMARY:{esc(summary)}\r\n" + (f"LOCATION:{esc(location)}\r\n" if location else "") +
               "END:VEVENT\r\nEND:VCALENDAR\r\n")
        href = cals[0]["href"].rstrip("/") + f"/{uid}.ics"
        self._req("PUT", href, ics, ctype="text/calendar; charset=utf-8")
        return cals[0]["name"]


def caldav_test(cfg: dict) -> Optional[str]:
    try:
        cals = CalDav(cfg).calendars()
    except PermissionError as e:
        return str(e) + ". Нужен именно пароль приложения, а не обычный пароль от аккаунта."
    except (requests.RequestException, RuntimeError, ET.ParseError) as e:
        return _err(e) if isinstance(e, requests.RequestException) else str(e)
    return None if cals else "вход удался, но календарей не нашлось"


def caldav_run(cfg: dict, name: str, args: dict, tz) -> str:
    cal = CalDav(cfg)
    if name == "caldav_events":
        a, b = _period(args, tz)
        return f"События {a:%d.%m}–{(b - timedelta(days=1)):%d.%m}:\n" + format_events(cal.events(a, b, tz))
    # caldav_create
    try:
        start = datetime.strptime(str(args.get("start", "")), "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    except ValueError:
        return "Ошибка: start нужен в формате YYYY-MM-DD HH:MM"
    minutes = int(args.get("minutes") or 60)
    where = cal.create(str(args.get("title") or "Событие"), start, start + timedelta(minutes=minutes),
                       str(args.get("location") or ""))
    return f"Готово: «{args.get('title')}» {start:%d.%m %H:%M}, {minutes} мин — в календаре «{where}»."


# ═════════════════════════ 3. Почта (IMAP, только чтение) ═════════════════════════

IMAP_HOSTS = {"yandex.ru": "imap.yandex.ru", "ya.ru": "imap.yandex.ru", "yandex.com": "imap.yandex.com",
              "mail.ru": "imap.mail.ru", "bk.ru": "imap.mail.ru", "inbox.ru": "imap.mail.ru", "list.ru": "imap.mail.ru",
              "internet.ru": "imap.mail.ru", "gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
              "outlook.com": "outlook.office365.com", "hotmail.com": "outlook.office365.com",
              "live.com": "outlook.office365.com", "icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com",
              "rambler.ru": "imap.rambler.ru"}


def _imap(cfg: dict) -> imaplib.IMAP4_SSL:
    host = (cfg.get("host") or "").strip() or IMAP_HOSTS.get(cfg["email"].split("@")[-1].lower(), "")
    if not host:
        raise ValueError("не знаю сервер этой почты — впиши его в поле «Сервер IMAP» (например imap.example.com)")
    m = imaplib.IMAP4_SSL(host, 993, timeout=25)
    try:
        m.login(cfg["email"], cfg["password"])
    except imaplib.IMAP4.error:
        raise PermissionError("почта не приняла логин или пароль. Нужен пароль приложения, "
                              "и в настройках почты должен быть включён доступ по IMAP")
    return m


def _hdr(v) -> str:
    try:
        return str(email.header.make_header(email.header.decode_header(v or "")))
    except Exception:
        return str(v or "")


def _snippet(msg) -> str:
    part = None
    for p in (msg.walk() if msg.is_multipart() else [msg]):
        if p.get_content_type() == "text/plain" and not p.get_filename():
            part = p
            break
    if part is None:
        for p in (msg.walk() if msg.is_multipart() else [msg]):
            if p.get_content_type() == "text/html":
                part = p
                break
    if part is None:
        return ""
    try:
        text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""
    if part.get_content_type() == "text/html":
        text = re.sub(r"<(script|style).*?</\1>|<[^>]+>", " ", text, flags=re.S | re.I)
    return re.sub(r"\s+", " ", text).strip()[:300]


def mail_test(cfg: dict) -> Optional[str]:
    try:
        _imap(cfg).logout()
    except (PermissionError, ValueError) as e:
        return str(e)
    except OSError as e:
        return f"не подключился к почтовому серверу: {e}"
    return None


def mail_run(cfg: dict, name: str, args: dict, tz) -> str:
    m = _imap(cfg)
    try:
        m.select("INBOX", readonly=True)                  # только чтение: письма остаются непрочитанными
        limit = max(1, min(15, int(args.get("limit") or 8)))
        if name == "mail_search":
            q = str(args.get("query") or "").strip()
            if not q:
                return "Ошибка: укажи, что искать."
            try:
                m._encoding = "utf-8"                       # поиск по-русски
                typ, data = m.search("UTF-8", "TEXT", q.encode("utf-8"))
            except imaplib.IMAP4.error:
                typ, data = m.search(None, "TEXT", f'"{q}"')
            title = f"Письма со словами «{q}»"
        else:
            typ, data = m.search(None, "UNSEEN")
            title = "Непрочитанные письма"
        ids = (data[0] or b"").split()
        if not ids:
            return title + ": нет."
        out = [f"{title}: {len(ids)}" + (f", показываю последние {limit}" if len(ids) > limit else "")]
        for i in reversed(ids[-limit:]):
            typ, d = m.fetch(i, "(BODY.PEEK[])")
            msg = email.message_from_bytes(d[0][1])
            try:
                when = email.utils.parsedate_to_datetime(msg["Date"]).astimezone(tz).strftime("%d.%m %H:%M")
            except Exception:
                when = ""
            out.append(f"- {when} от {_hdr(msg['From'])}: «{_hdr(msg['Subject'])}» — {_snippet(msg)}")
        return "\n".join(out)
    finally:
        try:
            m.logout()
        except Exception:
            pass


# ═════════════════════════ 4. Home Assistant ═════════════════════════

HA_DOMAINS = {"light": "свет", "switch": "выключатель", "fan": "вентилятор", "cover": "шторы/ворота",
              "climate": "климат", "media_player": "плеер", "scene": "сцена", "script": "сценарий",
              "sensor": "датчик", "binary_sensor": "датчик", "input_boolean": "переключатель", "vacuum": "пылесос"}
HA_SERVICES = {"turn_on", "turn_off", "toggle", "open_cover", "close_cover", "stop_cover", "set_temperature",
               "media_play", "media_pause", "volume_set", "start", "return_to_base"}
HA_FORBIDDEN = {"lock", "alarm_control_panel"}          # замки и сигнализацию Трой не трогает


def _ha(cfg: dict, method: str, path: str, body: Optional[dict] = None):
    url = cfg["url"].rstrip("/") + "/api/" + path.lstrip("/")
    r = requests.request(method, url, json=body, timeout=20,
                         headers={"Authorization": f"Bearer {cfg['token']}", **UA})
    if r.status_code == 401:
        raise PermissionError("Home Assistant не принял токен")
    r.raise_for_status()
    return r.json() if r.text else {}


def ha_test(cfg: dict) -> Optional[str]:
    try:
        _ha(cfg, "GET", "")
    except PermissionError as e:
        return str(e) + " — создай новый долгосрочный токен"
    except requests.RequestException as e:
        return _err(e) + ". Адрес выглядит так: http://192.168.1.10:8123 (если Трой не в той же сети — нужен внешний адрес)"
    return None


def ha_run(cfg: dict, name: str, args: dict, tz) -> str:
    if name == "ha_devices":
        q = str(args.get("filter") or "").lower()
        rows = []
        for s in _ha(cfg, "GET", "states"):
            dom = s["entity_id"].split(".")[0]
            if dom not in HA_DOMAINS:
                continue
            title = s.get("attributes", {}).get("friendly_name") or s["entity_id"]
            if q and q not in title.lower() and q not in s["entity_id"]:
                continue
            unit = s.get("attributes", {}).get("unit_of_measurement") or ""
            rows.append(f"- {title} [{s['entity_id']}]: {s['state']}{unit}")
        return ("Устройства:\n" + "\n".join(rows[:80])) if rows else "Подходящих устройств нет."
    entity = str(args.get("entity_id") or "")
    service = str(args.get("action") or "")
    dom = entity.split(".")[0]
    if dom in HA_FORBIDDEN:
        return "Замки и сигнализацию я не трогаю — это делается только в самом Home Assistant."
    if dom not in HA_DOMAINS or service not in HA_SERVICES:
        return f"Ошибка: не умею {service} для {entity}. Сначала посмотри устройства (ha_devices)."
    data = {"entity_id": entity}
    if service == "set_temperature" and args.get("value") is not None:
        data["temperature"] = float(args["value"])
    if service == "volume_set" and args.get("value") is not None:
        data["volume_level"] = max(0.0, min(1.0, float(args["value"]) / 100))
    _ha(cfg, "POST", f"services/{dom}/{service}", data)
    return f"Готово: {service} → {entity}"


# ═════════════════════════ 5. Маршруты: ссылка на Яндекс Карты ═════════════════════════
# Посчитать маршрут на общественном транспорте бесплатно нельзя (API маршрутов у Яндекса и Google платные),
# поэтому Трой даёт готовую ссылку: на телефоне откроется Яндекс Карты с построенным маршрутом,
# пересадками и временем в пути. Адреса переводим в точки бесплатным геокодером OpenStreetMap.

NOMINATIM = "https://nominatim.openstreetmap.org/search"
ROUTE_MODES = {"transit": ("mt", "на общественном транспорте"), "car": ("auto", "на машине"),
               "walk": ("pd", "пешком"), "bike": ("bc", "на велосипеде"), "taxi": ("taxi", "на такси")}
_geo_last = [0.0]


def geocode(query: str, near: Optional[tuple] = None) -> Optional[dict]:
    """Адрес или место → {"lat", "lon", "name"} (OpenStreetMap; не чаще раза в секунду — правило сервиса)."""
    wait = 1.1 - (time.time() - _geo_last[0])
    if wait > 0:
        time.sleep(wait)
    params = {"q": query, "format": "jsonv2", "limit": 1, "accept-language": "ru", "addressdetails": 0}
    if near:                                       # ищем сначала рядом с домом пользователя
        lat, lon = near
        params["viewbox"] = f"{lon - 0.6},{lat + 0.4},{lon + 0.6},{lat - 0.4}"
    try:
        r = requests.get(NOMINATIM, params=params, timeout=15, headers=UA)
        _geo_last[0] = time.time()
        r.raise_for_status()
        found = r.json()
        if found:
            f = found[0]
            return {"lat": float(f["lat"]), "lon": float(f["lon"]), "name": f.get("display_name", query)}
    except (requests.RequestException, ValueError) as e:
        log.warning("Геокодер OpenStreetMap: %s — пробую запасной", _err(e))
    return geocode_photon(query, near)          # запасной бесплатный геокодер (тоже на данных OpenStreetMap)


PHOTON = "https://photon.komoot.io/api/"


def geocode_photon(query: str, near: Optional[tuple] = None) -> Optional[dict]:
    params = {"q": query, "limit": 1}
    if near:
        params.update(lat=near[0], lon=near[1])
    try:
        r = requests.get(PHOTON, params=params, timeout=15, headers=UA)
        r.raise_for_status()
        feats = r.json().get("features") or []
    except (requests.RequestException, ValueError) as e:
        raise RuntimeError("сервис адресов сейчас недоступен — попробуй через минуту") from e
    if not feats:
        return None
    pr, (lon, lat) = feats[0].get("properties", {}), feats[0]["geometry"]["coordinates"][:2]
    name = ", ".join(x for x in (pr.get("name"), pr.get("street"), pr.get("housenumber"), pr.get("city")) if x)
    return {"lat": float(lat), "lon": float(lon), "name": name or query}


class Places:
    """«Дом», «работа» и другие места пользователя — чтобы говорить «от дома до работы»."""

    def __init__(self, db: Callable):
        self.db = db

    def all(self) -> dict:
        from contextlib import closing
        with closing(self.db()) as con:
            row = con.execute("SELECT value FROM kv WHERE key='places'").fetchone()
        try:
            return json.loads(row[0]) if row else {}
        except ValueError:
            return {}

    def save(self, name: str, address: str) -> str:
        from contextlib import closing
        name = name.strip().lower()
        if not name or not address.strip():
            return "Ошибка: нужно название места и адрес."
        places = self.all()
        g = geocode(address, self._home(places))
        if not g:
            return f"Не нашёл на карте «{address}». Попроси адрес точнее: город, улица, дом."
        places[name] = {"address": address.strip(), "lat": g["lat"], "lon": g["lon"]}
        with closing(self.db()) as con, con:
            con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES ('places', ?)",
                        (json.dumps(places, ensure_ascii=False),))
        return f"Запомнил «{name}»: {g['name']}"

    @staticmethod
    def _home(places: dict) -> Optional[tuple]:
        p = places.get("дом") or next(iter(places.values()), None)
        return (p["lat"], p["lon"]) if p else None

    def resolve(self, text: str) -> Optional[dict]:
        places = self.all()
        key = re.sub(r"^(?:от|до|из|в|на|с)\s+", "", text.strip().lower())
        key = {"дома": "дом", "работы": "работа", "работу": "работа", "офиса": "офис"}.get(key, key)
        if key in places:
            p = places[key]
            return {"lat": p["lat"], "lon": p["lon"], "name": f"{key} ({p['address']})"}
        return geocode(text, self._home(places))


def _safe(fn):
    def wrap(*a, **k):
        try:
            return fn(*a, **k)
        except RuntimeError as e:
            return f"Не получилось найти адрес: {e}."
    return wrap


@_safe
def route_link(places: Places, args: dict) -> str:
    mode = str(args.get("mode") or "transit")
    rtt, title = ROUTE_MODES.get(mode, ROUTE_MODES["transit"])
    to = places.resolve(str(args.get("to") or ""))
    if not to:
        return f"Не нашёл на карте «{args.get('to')}». Попроси уточнить: город, улица, название места."
    frm_q = str(args.get("from") or "").strip()
    frm = places.resolve(frm_q) if frm_q else None
    if frm_q and not frm:
        return f"Не нашёл на карте «{frm_q}». Попроси уточнить: город, улица, название места."
    pt = lambda p: f"{p['lat']:.6f},{p['lon']:.6f}"
    url = f"https://yandex.ru/maps/?rtext={pt(frm) if frm else ''}~{pt(to)}&rtt={rtt}"
    where_from = frm["name"] if frm else "текущего места (определит телефон)"
    return (f"Ссылка на маршрут {title}: {url}\n"
            f"Откуда: {where_from}\nКуда: {to['name']}\n"
            "Передай пользователю ссылку целиком, как есть: она откроет Яндекс Карты с готовым маршрутом, "
            "пересадками и временем в пути. Сам время и пересадки не придумывай.")


TAXI_TARIFFS = {"econom": "Эконом", "business": "Комфорт", "comfortplus": "Комфорт+", "minivan": "Минивэн",
                "vip": "Бизнес", "child_tariff": "Детский"}


@_safe
def taxi_link(places: Places, args: dict, from_pc: bool = False) -> str:
    """Ссылка на заказ в Яндекс Go: откроет приложение с уже заполненным маршрутом — заказ человек подтверждает сам."""
    to_q, frm_q = str(args.get("to") or "").strip(), str(args.get("from") or "").strip()
    params = []
    frm = places.resolve(frm_q) if frm_q else None
    if frm_q and not frm:
        return f"Не нашёл на карте «{frm_q}». Попроси уточнить: город, улица, дом."
    to = places.resolve(to_q) if to_q else None
    if to_q and not to:
        return f"Не нашёл на карте «{to_q}». Попроси уточнить: город, улица, дом."
    if not to and not frm:
        return "Ошибка: куда ехать? Спроси адрес."
    if frm:
        params += [f"start-lat={frm['lat']:.6f}", f"start-lon={frm['lon']:.6f}"]
    if to:
        params += [f"end-lat={to['lat']:.6f}", f"end-lon={to['lon']:.6f}"]
    tariff = str(args.get("tariff") or "econom")
    if tariff in TAXI_TARIFFS:
        params.append(f"tariffClass={tariff}")
    params.append("ref=sometroy")
    base = "https://3.redirect.appmetrica.yandex.com/route?" + "&".join(params)
    app = base + "&appmetrica_tracking_id=1178268795219780156"     # телефон: приложение Яндекс Go
    web = base + "&appmetrica_tracking_id=25395763362139037"       # компьютер: сайт Яндекс Go
    links = ([("💻 На сайте Яндекс Go (с компьютера)", web), ("📱 В приложении (с телефона)", app)] if from_pc
             else [("📱 В приложении Яндекс Go", app), ("💻 На сайте (с компьютера)", web)])
    if not frm and from_pc:
        note = " Точку подачи на сайте укажи сам — компьютер своё место не знает (или скажи «от дома»)."
    else:
        note = ""
    return (f"Заказ такси в Яндекс Go ({TAXI_TARIFFS.get(tariff, 'Эконом')}):\n" +
            "\n".join(f"{t}: {u}" for t, u in links) +
            f"\nОткуда: {frm['name'] if frm else 'текущее место'}\nКуда: {to['name'] if to else 'не указано'}\n"
            "Передай обе ссылки целиком, как есть. Скажи: маршрут уже вбит — тариф, оплату и сам заказ пользователь "
            "подтверждает в Яндекс Go. Сам ты такси не заказываешь." + note)


ROUTE_HINT = r"такси|яндекс ?go|как (?:добраться|доехать|дойти|проехать|попасть)|маршрут|доехать|добраться|дорог[ау] (?:до|от)|ехать (?:до|от|в)|сколько ехать|запомни.*(?:адрес|дом|работ)|мой (?:дом|адрес)|я живу"
ROUTE_TOOLS = [
    fn("route_link", "Ссылка на маршрут в Яндекс Картах (общественный транспорт, машина, пешком…).", {
        "from": {"type": "string", "description": "откуда: адрес, место или «дом»/«работа»; пусто — от текущего места"},
        "to": {"type": "string", "description": "куда: адрес, место или «дом»/«работа»"},
        "mode": {"type": "string", "enum": list(ROUTE_MODES), "description": "по умолчанию transit — общественный транспорт"}},
        ["to"]),
    fn("taxi_link", "Ссылка на заказ такси в Яндекс Go с заполненным маршрутом (заказ пользователь подтверждает сам).", {
        "from": {"type": "string", "description": "откуда: адрес, место или «дом»/«работа»; пусто — от текущего места"},
        "to": {"type": "string", "description": "куда: адрес, место или «дом»/«работа»"},
        "tariff": {"type": "string", "enum": list(TAXI_TARIFFS), "description": "по умолчанию econom"}}, ["to"]),
    fn("save_place", "Запомнить место пользователя (дом, работа, дача…) для маршрутов.", {
        "name": {"type": "string", "description": "дом, работа, дача, офис…"},
        "address": {"type": "string", "description": "адрес с городом"}}, ["name", "address"]),
]
ROUTE_RULES = """
- Такси: taxi_link даёт ссылку на Яндекс Go с уже вбитым маршрутом. Ты такси не заказываешь — пользователь
  нажимает ссылку и сам подтверждает заказ в приложении. Тариф «комфорт» — business, «комфорт плюс» — comfortplus.
- Маршруты: route_link даёт ссылку на Яндекс Карты (по умолчанию общественный транспорт). Сам маршрут, пересадки
  и время не выдумывай — отправь ссылку как есть. Если в адресе нет города, а дом пользователя не сохранён,
  уточни город. «Мой дом — …», «работа — …» запоминай через save_place."""


# ═════════════════════════ Список подключений ═════════════════════════

CONNECTORS: Dict[str, dict] = {
    "ics": {
        "title": "Календарь по ссылке", "about": "Google, Яндекс, Outlook — только чтение событий.",
        "fields": [{"name": "url", "label": "Ссылка на календарь (iCal, .ics)", "secret": True,
                    "placeholder": "https://calendar.google.com/calendar/ical/…/basic.ics"}],
        "hint": r"календар|событи|встреч|планы|расписани[ея] на|что у меня (?:завтра|сегодня|на)|занят",
        "tools": [fn("calendar_events", "События из календаря пользователя за период.", PERIOD_PROPS)],
        "test": ics_test, "run": ics_run,
        "help": """
Ссылка «только для чтения» на твой календарь — Трой увидит события, но менять ничего не сможет.

**Google Календарь** (с компьютера, calendar.google.com):
1. Слева наведи на нужный календарь → ⋮ → **Настройки и общий доступ**.
2. Внизу раздел **Интеграция календаря** → **Закрытый адрес в формате iCal** → скопируй.

**Яндекс Календарь** (calendar.yandex.ru):
1. Наведи на календарь слева → ⚙ → **Экспорт**.
2. Скопируй ссылку **iCal**.

**Outlook** (outlook.com): ⚙ Настройки → Календарь → **Общие календари** → Опубликовать календарь →
скопируй ссылку **ICS**.

Ссылка — как пароль: кто её знает, тот видит календарь. Никому не показывай.
"""},
    "caldav": {
        "title": "Календарь с записью (Яндекс, iCloud, Mail.ru)",
        "about": "Трой видит события и сам добавляет новые («запиши к стоматологу на пятницу в 10»).",
        "fields": [{"name": "server", "label": "Сервис", "choices": [["https://caldav.yandex.ru", "Яндекс"],
                                                                       ["https://caldav.icloud.com", "iCloud"],
                                                                       ["https://calendar.mail.ru", "Mail.ru"]],
                    "placeholder": "или свой адрес CalDAV"},
                   {"name": "login", "label": "Логин (почта)", "placeholder": "name@yandex.ru"},
                   {"name": "password", "label": "Пароль приложения", "secret": True}],
        "hint": r"календар|событи|встреч|планы|запиши (?:к|на|в)|что у меня (?:завтра|сегодня|на)|занят",
        "tools": [fn("caldav_events", "События из календаря пользователя за период.", PERIOD_PROPS),
                  fn("caldav_create", "Добавить событие в календарь пользователя. Только после его «да».", {
                      "title": {"type": "string"}, "start": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
                      "minutes": {"type": "integer", "description": "длительность, по умолчанию 60"},
                      "location": {"type": "string"}}, ["title", "start"])],
        "write": {"caldav_create"},
        "test": caldav_test, "run": caldav_run,
        "help": """
Нужен **пароль приложения** — отдельный пароль только для календаря. Обычный пароль от аккаунта не подойдёт.

**Яндекс:**
1. Открой https://id.yandex.ru/security/app-passwords
2. **Создать пароль приложения** → тип **Календарь CalDAV** → придумай название → **Далее**.
3. Скопируй пароль (показывается один раз). Логин — твоя почта на Яндексе.

**iCloud (Apple):**
1. Открой https://account.apple.com → **Вход и безопасность** → **Пароли для приложений**.
2. Создай пароль, скопируй. Логин — твой Apple ID.

**Mail.ru:**
1. Открой https://account.mail.ru/user/2-step-auth/passwords (нужна двухфакторная защита).
2. Создай пароль для внешнего приложения. Логин — почта на Mail.ru.

Пароль приложения можно в любой момент удалить там же — доступ Троя сразу пропадёт.
"""},
    "mail": {
        "title": "Почта", "about": "Яндекс, Mail.ru, Gmail, Outlook и другие — только чтение: новые письма и поиск.",
        "fields": [{"name": "email", "label": "Адрес почты", "placeholder": "name@yandex.ru"},
                   {"name": "password", "label": "Пароль приложения", "secret": True},
                   {"name": "host", "label": "Сервер IMAP (необязательно)", "placeholder": "определю сам",
                    "optional": True}],
        "hint": r"почт|письм|e-?mail|имейл|ящик|входящ",
        "tools": [fn("mail_unread", "Непрочитанные письма пользователя (кто, тема, начало текста).",
                     {"limit": {"type": "integer", "description": "сколько показать, до 15"}}),
                  fn("mail_search", "Найти письма по слову или отправителю.",
                     {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"])],
        "test": mail_test, "run": mail_run,
        "help": """
Трой только **читает** почту: показывает новые письма и ищет по ним. Письма остаются непрочитанными,
отправлять и удалять он не может.

Нужен **пароль приложения** (обычный пароль почта не пустит):

**Яндекс:** https://id.yandex.ru/security/app-passwords → **Почта IMAP** → скопируй пароль.
Проверь, что в настройках почты включено: ⚙ → **Почтовые программы** → «С сервера imap.yandex.ru по протоколу IMAP».

**Mail.ru:** https://account.mail.ru/user/2-step-auth/passwords → создай пароль для внешнего приложения.

**Gmail:** нужна двухэтапная проверка; затем https://myaccount.google.com/apppasswords → создай пароль.

**Outlook / Hotmail:** https://account.live.com/proofs/AppPassword (тоже нужна двухэтапная проверка).

Сервер IMAP для этих почт Трой определит сам. Для другой почты впиши его адрес (есть в справке почты).
"""},
    "ha": {
        "title": "Умный дом (Home Assistant)", "about": "Свет, розетки, климат, датчики: «выключи свет в зале».",
        "fields": [{"name": "url", "label": "Адрес Home Assistant", "placeholder": "http://192.168.1.10:8123"},
                   {"name": "token", "label": "Долгосрочный токен", "secret": True}],
        "hint": r"свет|ламп|розетк|умн|дом[еау]? |температур|влажн|кондиционер|обогрев|штор|пылесос|датчик|home assistant",
        "tools": [fn("ha_devices", "Устройства умного дома и их состояние (свет, датчики, климат…).",
                     {"filter": {"type": "string", "description": "слово для поиска, например «зал» или «свет»"}}),
                  fn("ha_control", "Управлять устройством умного дома. entity_id — из ha_devices.", {
                      "entity_id": {"type": "string"},
                      "action": {"type": "string", "enum": sorted(HA_SERVICES)},
                      "value": {"type": "number", "description": "для set_temperature — градусы, volume_set — 0–100"}},
                     ["entity_id", "action"])],
        "test": ha_test, "run": ha_run,
        "help": """
Если у тебя дома уже работает **Home Assistant**, Трой может управлять устройствами и смотреть датчики.
Замки и сигнализацию он не трогает.

1. Открой Home Assistant в браузере. Адрес обычно вида http://192.168.1.10:8123 — скопируй его.
2. Внизу слева нажми на своё имя (профиль) → вкладка **Безопасность**.
3. Внизу **Долгосрочные токены доступа** → **Создать токен** → назови «Трой» → скопируй
   (показывается один раз).
4. Вставь адрес и токен сюда.

Если Трой живёт на сервере, а Home Assistant — дома, серверу нужен доступ к нему из интернета
(например, через Home Assistant Cloud — Nabu Casa — или свой внешний адрес). Если Трой живёт на
домашнем компьютере — подойдёт обычный домашний адрес.
"""},
}


# ═════════════════════════ 6. Умный дом Яндекса (Алиса) ═════════════════════════

YIOT_API = "https://api.iot.yandex.net/v1.0/"
YIOT_TYPES = {"light": "свет", "socket": "розетка", "switch": "выключатель", "thermostat": "климат",
              "thermostat.ac": "кондиционер", "media_device": "медиа", "media_device.tv": "телевизор",
              "openable": "шторы/двери", "openable.curtain": "шторы", "humidifier": "увлажнитель",
              "purifier": "очиститель", "vacuum_cleaner": "пылесос", "cooking": "кухня", "cooking.kettle": "чайник",
              "washing_machine": "стиральная машина", "sensor": "датчик", "other": "устройство"}
YIOT_UNITS = {"unit.temperature.celsius": "°C", "unit.percent": "%", "unit.ppm": " ppm", "unit.illumination.lux": " лк"}


def yiot_clean_token(raw: str) -> str:
    """Токен, как его обычно копируют: целиком адрес страницы (#access_token=…), с «OAuth »/«Bearer » или пробелами."""
    t = (raw or "").strip().strip("'\"")
    m = re.search(r"access_token=([^&\s]+)", t)
    if m:
        t = m.group(1)
    return re.sub(r"^(?:oauth|bearer)\s+", "", t, flags=re.I).strip()


def yiot_clientid_hint(token: str) -> Optional[str]:
    """Частая ошибка: вместо токена вставлен ClientID приложения (32 символа 0-9 a-f)."""
    if re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return ("это ClientID приложения, а не токен. Открой ссылку "
                f"https://oauth.yandex.ru/authorize?response_type=token&client_id={token} , нажми «Разрешить» "
                "и скопируй длинный токен со страницы (обычно начинается с y0_)")
    return None


def _yiot(cfg: dict, method: str, path: str, body: Optional[dict] = None) -> dict:
    token = yiot_clean_token(cfg.get("token", ""))
    hint = yiot_clientid_hint(token)
    if hint:
        raise PermissionError(hint)
    r = requests.request(method, YIOT_API + path, json=body, timeout=20,
                         headers={"Authorization": f"Bearer {token}", **UA})
    if r.status_code == 401:
        raise PermissionError("Яндекс не принял токен: он неверный или истёк (обычно токен начинается с y0_). "
                              "Получи новый по ссылке authorize из инструкции")
    if r.status_code == 403:
        raise PermissionError("у токена нет прав на умный дом. В своём приложении на oauth.yandex.ru проверь "
                              "доступы «Умный дом Яндекса: просмотр и управление», сохрани и получи токен заново")
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "error":
        raise RuntimeError(data.get("message") or "Яндекс ответил ошибкой")
    return data


def yiot_test(cfg: dict) -> Optional[str]:
    try:
        _yiot(cfg, "GET", "user/info")
    except PermissionError as e:
        return str(e)[0].upper() + str(e)[1:] + "."
    except (requests.RequestException, RuntimeError) as e:
        return _err(e) if isinstance(e, requests.RequestException) else str(e)
    return None


def _yiot_kind(t: str) -> str:
    t = t.replace("devices.types.", "")
    return YIOT_TYPES.get(t) or YIOT_TYPES.get(t.split(".")[0], t)


def _yiot_state(d: dict) -> str:
    parts = []
    for c in d.get("capabilities", []):
        st = c.get("state") or {}
        if st.get("instance") == "on":
            parts.append("включено" if st.get("value") else "выключено")
        elif st.get("instance") in ("brightness", "temperature", "volume", "humidity", "open") and st.get("value") is not None:
            parts.append(f"{st['instance']} {st['value']}")
    for p in d.get("properties", []):
        st = p.get("state") or {}
        if st.get("value") is not None:
            unit = YIOT_UNITS.get((p.get("parameters") or {}).get("unit", ""), "")
            parts.append(f"{st.get('instance')} {st['value']}{unit}")
    return ", ".join(parts) or "состояние неизвестно"


def _yiot_find(info: dict, query: str) -> tuple:
    """(kind, obj) — устройство, группа или сценарий по id или названию (с комнатой)."""
    q = query.lower().strip()
    rooms = {r["id"]: r["name"] for r in info.get("rooms", [])}
    for kind, items in (("device", info.get("devices", [])), ("group", info.get("groups", [])),
                        ("scenario", info.get("scenarios", []))):
        for it in items:
            if it.get("id") == query:
                return kind, it
    best, best_score = None, 0
    words = [w for w in re.split(r"\W+", q) if len(w) > 2]
    for kind, items in (("device", info.get("devices", [])), ("group", info.get("groups", []))):
        for it in items:
            name = f"{it.get('name', '')} {rooms.get(it.get('room'), '')}".lower()
            score = sum(1 for w in words if w[:5] in name) + (3 if it.get("name", "").lower() in q else 0)
            if score > best_score:
                best, best_score = (kind, it), score
    return best if best else (None, None)


def yiot_run(cfg: dict, name: str, args: dict, tz) -> str:
    info = _yiot(cfg, "GET", "user/info")
    rooms = {r["id"]: r["name"] for r in info.get("rooms", [])}
    if name == "yhome_devices":
        q = str(args.get("filter") or "").lower()
        rows = []
        for d in info.get("devices", []):
            title = f"{d.get('name')} ({rooms.get(d.get('room'), 'без комнаты')})"
            if q and q not in title.lower() and q not in _yiot_kind(d.get("type", "")):
                continue
            rows.append(f"- {title}, {_yiot_kind(d.get('type', ''))}: {_yiot_state(d)} [id {d['id']}]")
        groups = [f"- группа «{g['name']}» [id {g['id']}]" for g in info.get("groups", []) if not q or q in g["name"].lower()]
        scen = [f"- сценарий «{sc['name']}»" for sc in info.get("scenarios", []) if sc.get("is_active", True)]
        out = ("Устройства:\n" + "\n".join(rows[:80])) if rows else "Подходящих устройств нет."
        if groups:
            out += "\nГруппы:\n" + "\n".join(groups[:30])
        if scen and not q:
            out += "\nСценарии:\n" + "\n".join(scen[:30])
        return out
    if name == "yhome_scenario":
        q = str(args.get("scenario") or "").lower()
        sc = next((x for x in info.get("scenarios", []) if x["id"] == q or x["name"].lower() == q), None) or \
            next((x for x in info.get("scenarios", []) if q and q in x["name"].lower()), None)
        if not sc:
            return "Такого сценария нет. Сценарии: " + ", ".join(x["name"] for x in info.get("scenarios", []))
        _yiot(cfg, "POST", f"scenarios/{sc['id']}/actions")
        return f"Готово: запустил сценарий «{sc['name']}»."
    # yhome_control
    kind, obj = _yiot_find(info, str(args.get("target") or ""))
    if not obj:
        return "Не нашёл такое устройство или группу. Посмотри список (yhome_devices)."
    if "lock" in str(obj.get("type", "")) or "security" in str(obj.get("type", "")):
        return "Замки и охранные устройства я не трогаю — только в приложении «Дом с Алисой»."
    action, value = str(args.get("action") or ""), args.get("value")
    if action in ("on", "off"):
        act = {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": action == "on"}}
    elif action in ("brightness", "temperature", "volume", "humidity") and value is not None:
        act = {"type": "devices.capabilities.range", "state": {"instance": action, "value": float(value)}}
    elif action in ("open", "close"):
        act = {"type": "devices.capabilities.range", "state": {"instance": "open", "value": 100 if action == "open" else 0}}
    else:
        return "Ошибка: action — on, off, brightness, temperature, volume, humidity, open или close (с value, где нужно)."
    if kind == "group":
        _yiot(cfg, "POST", f"groups/{obj['id']}/actions", {"actions": [act]})
    else:
        res = _yiot(cfg, "POST", "devices/actions", {"devices": [{"id": obj["id"], "actions": [act]}]})
        for d in res.get("devices", []):
            for c in d.get("capabilities", []):
                ar = (c.get("state") or {}).get("action_result") or {}
                if ar.get("status") == "ERROR":
                    return f"Устройство «{obj.get('name')}» не выполнило команду: {ar.get('error_message') or ar.get('error_code')}"
    room = rooms.get(obj.get("room"), "")
    return f"Готово: {obj.get('name')}{' (' + room + ')' if room else ''} — {action}{' ' + str(value) if value is not None else ''}."


_YCACHE: dict = {"at": 0.0, "token": "", "info": None}


def yiot_info_cached(cfg: dict, ttl: int = 300) -> Optional[dict]:
    """Список устройств — не чаще раза в 5 минут (для быстрых команд и подсказок модели)."""
    if _YCACHE["info"] is not None and _YCACHE["token"] == cfg.get("token") and time.time() - _YCACHE["at"] < ttl:
        return _YCACHE["info"]
    if _YCACHE.get("fail_at") and time.time() - _YCACHE["fail_at"] < 60 and _YCACHE.get("fail_token") == cfg.get("token"):
        return _YCACHE["info"] if _YCACHE["token"] == cfg.get("token") else None     # не долбим Яндекс каждую секунду
    try:
        info = _yiot(cfg, "GET", "user/info")
    except Exception as e:
        log.warning("Умный дом Яндекса: %s", e)
        _YCACHE.update(fail_at=time.time(), fail_token=cfg.get("token"), error=str(e))
        return _YCACHE["info"] if _YCACHE["token"] == cfg.get("token") else None
    _YCACHE.update(at=time.time(), token=cfg.get("token"), info=info, fail_at=0, error="")
    return info


_NOT_HOME = re.compile(r"^(?:комп\w*|пк|ноут\w*|звук|музык\w*|трек|песн\w*|экран|монитор\w*)\b", re.I)


def yiot_unavailable(cfg: dict, text: str) -> Optional[str]:
    """Токен есть, но умный дом недоступен, а команда похожа на «вкл/выкл устройство» — честно говорим, а не
    отправляем её компьютеру как «закрыть программу»."""
    m = _YFAST.match(_yfast_text(text))
    if not m or _NOT_HOME.match(m.group(2).strip()) or yiot_info_cached(cfg):
        return None
    why = _YCACHE.get("error") or "нет связи"
    return (f"⚠️ Не могу проверить умный дом Яндекса: {why}.\n"
            "Если это программа на компьютере — скажи «закрой …» / «открой …».")


_NUM_WORDS = {"один": "1", "одна": "1", "одну": "1", "первый": "1", "первая": "1", "первую": "1",
              "два": "2", "две": "2", "второй": "2", "вторая": "2", "вторую": "2",
              "три": "3", "третий": "3", "третья": "3", "третью": "3",
              "четыре": "4", "четвертый": "4", "четвертая": "4", "четвертую": "4",
              "пять": "5", "пятый": "5", "пятая": "5", "пятую": "5", "шесть": "6", "шестой": "6", "шестую": "6",
              "семь": "7", "седьмой": "7", "седьмую": "7", "восемь": "8", "восьмой": "8", "восьмую": "8",
              "девять": "9", "девятый": "9", "девятую": "9", "десять": "10", "десятый": "10", "десятую": "10"}


def _words(text: str) -> list:
    """Слова для сравнения названий: голосом «ферму два» = текстом «ферма 2»."""
    return [_NUM_WORDS.get(w, w) for w in re.sub(r"[^\w]+", " ", (text or "").lower().replace("ё", "е")).split()]


_FILLER = re.compile(r"\b(?:пожалуйста|плиз|please|ка|мне|нам|там|быстро|срочно|сейчас)\b", re.I)


def _yfast_text(text: str) -> str:
    """Как команду обычно говорят голосом: «Эй, Трой, выключи-ка, пожалуйста, ферму два.» → «выключи ферму два»."""
    t = re.sub(r"^\[голосовое\]\s*", "", (text or "").strip(), flags=re.I)
    t = re.sub(r"^(?:(?:эй|ну|слушай|так|трой|troy)[\s,!.:—-]+)+", "", t, flags=re.I)
    t = _FILLER.sub(" ", t.replace("-ка", " "))
    return re.sub(r"\s*[,;]\s*", " ", re.sub(r"\s+", " ", t)).strip(" .!?…")


def _word_hit(name_word: str, words: list) -> bool:
    """Слово из названия есть в тексте с учётом падежа: «ферма» ~ «ферму», «люстра» ~ «люстру», «2» = «2»."""
    if name_word.isdigit():
        return name_word in words
    if len(name_word) <= 3:                            # «зал» ~ «зале», «тв» = «тв»
        return any(w == name_word or (w.startswith(name_word) and len(w) - len(name_word) <= 2) for w in words)
    root = name_word[:max(3, len(name_word) - 2)]
    return any(w.startswith(root) and abs(len(w) - len(name_word)) <= 3 for w in words)


def _yiot_named(info: dict, text: str) -> list:
    """Устройства и группы, чьё название есть в тексте (в любом падеже; самые длинные названия — первыми)."""
    words = _words(text)
    out = []
    for kind, items in (("device", info.get("devices", [])), ("group", info.get("groups", []))):
        for it in items:
            nw = _words(it.get("name"))
            if nw and all(_word_hit(w, words) for w in nw):
                out.append((len(" ".join(nw)), kind, it))
    return [x[1:] for x in sorted(out, key=lambda x: -x[0])]


def yiot_mentions(cfg: dict, text: str) -> bool:
    info = yiot_info_cached(cfg)
    return bool(info and _yiot_named(info, text))


_YFAST = re.compile(r"^(включи|выключи|вруби|выруби|отключи|погаси|зажги|включить|выключить)\s+(.+?)[.!?…]*$", re.I)


def yiot_fast(cfg: dict, text: str, tz) -> Optional[str]:
    """«Выключи ферма 2» — если «ферма 2» есть в умном доме, выполняем сразу, без нейросети."""
    m = _YFAST.match(_yfast_text(text))
    if not m:
        return None
    info = yiot_info_cached(cfg)
    if not info:
        return None
    found = _yiot_named(info, m.group(2))
    if not found:
        return None
    top = [f for f in found if len(_words(f[1].get("name"))) == len(_words(found[0][1].get("name")))
           and _words(f[1].get("name")) == _words(found[0][1].get("name"))]
    if len(top) > 1:                                   # одинаковые названия в разных комнатах — смотрим на комнату
        rooms = {r["id"]: r["name"] for r in info.get("rooms", [])}
        words = _words(m.group(2))
        top = [f for f in top if all(_word_hit(w, words) for w in _words(rooms.get(f[1].get("room"), "")) or ["-"])]
        if len(top) != 1:
            return None                                # неясно, какое — пусть модель переспросит
    kind, obj = top[0]
    on = m.group(1).lower() in ("включи", "вруби", "зажги", "включить")
    try:
        return yiot_run(cfg, "yhome_control", {"target": obj["id"], "action": "on" if on else "off"}, tz)
    except Exception as e:
        return f"Умный дом Яндекса: не получилось — {_err(e) if isinstance(e, requests.RequestException) else e}"


YIOT_CONNECTOR = {
    "title": "Умный дом Яндекса (Алиса)",
    "about": "Свет, розетки, кондиционеры, шторы, сценарии Алисы: «выключи свет в спальне».",
    "fields": [{"name": "token", "label": "Токен умного дома Яндекса", "secret": True}],
    "hint": r"свет|ламп|розетк|умн|дом[еау]? |алис|сценари|кондиционер|кондей|обогрев|штор|пылесос|чайник|увлажнит|"
            r"температур|влажн|телевизор|телик|\bтв\b|вентилятор|подсветк|торшер|люстр|ночник|устройств",
    "tools": [fn("yhome_devices", "Устройства умного дома Яндекса, их комнаты и состояние; группы и сценарии.",
                 {"filter": {"type": "string", "description": "слово для поиска: комната или тип, например «спальня», «свет»"}}),
              fn("yhome_control", "Управлять устройством или группой умного дома Яндекса.", {
                  "target": {"type": "string", "description": "название устройства с комнатой или id из yhome_devices"},
                  "action": {"type": "string", "enum": ["on", "off", "brightness", "temperature", "volume", "humidity",
                                                        "open", "close"]},
                  "value": {"type": "number", "description": "для brightness/volume/humidity — %, temperature — °C"}},
                 ["target", "action"]),
              fn("yhome_scenario", "Запустить сценарий Алисы по названию.", {"scenario": {"type": "string"}}, ["scenario"])],
    "test": yiot_test, "run": yiot_run,
    "clean": lambda name, v: yiot_clean_token(v) if name == "token" else v,
    "help": """
Трой управляет устройствами из приложения **Дом с Алисой** через официальный API Яндекса. Нужен токен — это бесплатно,
займёт минуты три. Замки и охранные устройства Трой не трогает.

**1. Зарегистрируй «приложение» (один раз)**
1. Открой https://oauth.yandex.ru/client/new и войди тем же Яндекс ID, что и в «Доме с Алисой».
   Если спросят «Какое приложение хотите создать?» — выбери **Для авторизации пользователей**
   (вариант «Для доступа к API» к умному дому не пустит).
2. Название — любое, например «Трой».
3. Платформа — **Веб-сервисы**, в поле Redirect URI вставь: https://oauth.yandex.ru/verification_code
4. В доступах найди **Умный дом Яндекса** и отметь оба: **просмотр списка устройств** (iot:view)
   и **управление устройствами** (iot:control).
5. Нажми **Создать приложение** и скопируй **ClientID** (строка из 32 букв и цифр). Это ещё не токен!

**2. Получи токен**
1. Открой ссылку, подставив свой ClientID вместо ВАШ_CLIENT_ID:
   https://oauth.yandex.ru/authorize?response_type=token&client_id=ВАШ_CLIENT_ID
2. Нажми **Разрешить**. На странице появится токен — длинная строка, обычно начинается с **y0_**. Скопируй его.
3. Вставь токен сюда. Если по ошибке вставишь ClientID — Трой подскажет и даст готовую ссылку.

Токен действует около года. Когда истечёт, Трой скажет — получишь новый по пункту 2.
Отозвать доступ можно в любой момент: https://id.yandex.ru/security → «Доступы».
"""}


# ═════════════════════════ Хранение и использование ═════════════════════════

CONNECTORS["yhome"] = YIOT_CONNECTOR


class Connections:
    """Доступы подключений — в базе Троя (таблица kv), отдельно от настроек."""

    def __init__(self, db: Callable, tz):
        self.db, self.tz = db, tz
        # Модули Троя, которые можно включить позже (Telegram, Mistral, Битрикс24…). Их подключает сам бот:
        # id → {title, about, help, fields, state() → (вкл, сводка, подсказка), connect(values) → (ошибка, подсказка),
        #       disconnect()}
        self.modules: dict = {}
        self.note = ""          # подсказка после подключения модуля (например, ссылка «нажми Запустить»)

    def _all(self) -> dict:
        from contextlib import closing
        with closing(self.db()) as con:
            row = con.execute("SELECT value FROM kv WHERE key='connections'").fetchone()
        try:
            return json.loads(row[0]) if row else {}
        except ValueError:
            return {}

    def _save(self, data: dict) -> None:
        from contextlib import closing
        with closing(self.db()) as con, con:
            con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES ('connections', ?)",
                        (json.dumps(data, ensure_ascii=False),))

    def enabled(self) -> dict:
        return {k: v for k, v in self._all().items() if k in CONNECTORS}

    def listing(self) -> List[dict]:
        """Для окна «Подключения»: что есть и что включено. Секреты не отдаём."""
        have = self.enabled()
        out = []
        for mid, m in self.modules.items():
            on, summary, note = m["state"]()
            out.append({"id": mid, "group": "module", "title": m["title"], "about": m["about"],
                        "help": m["help"].strip(), "enabled": bool(on), "summary": summary, "note": note,
                        "fields": m["fields"]})
        for cid, c in CONNECTORS.items():
            cfg = have.get(cid) or {}
            out.append({"id": cid, "title": c["title"], "about": c["about"], "help": c["help"].strip(),
                        "enabled": bool(cfg),
                        "summary": next((cfg.get(f["name"]) for f in c["fields"]
                                         if not f.get("secret") and cfg.get(f["name"])), "") if cfg else "",
                        "fields": [{k: f[k] for k in ("name", "label", "secret", "placeholder", "optional", "choices")
                                    if k in f} for f in c["fields"]]})
        return out

    def connect(self, cid: str, values: dict) -> Optional[str]:
        self.note = ""
        if cid in self.modules:
            m = self.modules[cid]
            for f in m["fields"]:
                if not str(values.get(f["name"]) or "").strip() and not f.get("optional"):
                    return f"заполни поле «{f['label']}»"
            err, self.note = m["connect"]({k: str(v or "").strip() for k, v in values.items()})
            return err
        c = CONNECTORS.get(cid)
        if not c:
            return "нет такого подключения"
        cfg = {}
        for f in c["fields"]:
            v = str(values.get(f["name"]) or "").strip()
            if not v and not f.get("optional"):
                return f"заполни поле «{f['label']}»"
            cfg[f["name"]] = c["clean"](f["name"], v) if c.get("clean") else v
        err = c["test"](cfg)
        if err:
            return err
        data = self._all()
        data[cid] = cfg
        self._save(data)
        return None

    def disconnect(self, cid: str) -> None:
        if cid in self.modules:
            self.modules[cid]["disconnect"]()
            return
        data = self._all()
        data.pop(cid, None)
        self._save(data)

    def tools_for(self, texts: List[str]) -> tuple:
        """Функции для модели — только включённые и только когда разговор о них (экономим лимит)."""
        tools, rules = [], []
        for cid, cfg in self.enabled().items():
            c = CONNECTORS[cid]
            named = cid == "yhome" and texts and yiot_mentions(cfg, texts[0] or "")
            if named or any(re.search(c["hint"], t or "", re.I) for t in texts):
                tools += c["tools"]
                rules.append(c["title"])
        return tools, rules

    def owner_of(self, tool: str) -> Optional[str]:
        for cid, c in CONNECTORS.items():
            if any(t["function"]["name"] == tool for t in c["tools"]):
                return cid
        return None

    def is_write(self, tool: str) -> bool:
        cid = self.owner_of(tool)
        return bool(cid and tool in CONNECTORS[cid].get("write", set()))

    def run(self, tool: str, args: dict) -> str:
        cid = self.owner_of(tool)
        cfg = self.enabled().get(cid or "")
        if not cfg:
            return "Это подключение выключено. Его можно включить в программе: меню «Подключения»."
        try:
            return CONNECTORS[cid]["run"](cfg, tool, args, self.tz)
        except PermissionError as e:
            return f"Ошибка доступа ({CONNECTORS[cid]['title']}): {e}. Переподключи в программе: «Подключения»."
        except Exception as e:
            log.warning("Подключение %s: %s", cid, e)
            return f"Ошибка ({CONNECTORS[cid]['title']}): {_err(e) if isinstance(e, requests.RequestException) else e}"

    def diag(self) -> List[str]:
        lines = []
        for cid, cfg in self.enabled().items():
            c = CONNECTORS[cid]
            try:
                err = c["test"](cfg)
            except Exception as e:
                err = str(e)
            lines.append(("✅ " if not err else "❌ ") + c["title"] + ("" if not err else f": {err}"))
        return lines


# ═════════════════════════ Текст не в той раскладке ═════════════════════════
# «ghbdtn» → «привет», «jnrhjq ntktuhfv» → «открой телеграм»; и обратно — «ызщешан» → «spotify».

_EN = "`qwertyuiop[]asdfghjkl;'zxcvbnm,."
_RU = "ёйцукенгшщзхъфывапролджэячсмитьбю"
_EN2RU = {**dict(zip(_EN, _RU)), **dict(zip(_EN.upper().replace("[", "{").replace("]", "}").replace(";", ":")
                                             .replace("'", '"').replace(",", "<").replace(".", ">").replace("`", "~"),
                                             _RU.upper()))}
_RU2EN = {v: k for k, v in _EN2RU.items()}
_RU_BI = set("""ст но то на ен ов ни ра во ко ро ер ал по ре ли пр ет ол ан ле от ел ва ка не ор ос де ит ла од ти та ве ть ак
ри тр ск ом ес ой ем ат ин ло ез ид ды ым ам ие мо ми ма ую ня ег ив ая ог за ик ки ко лю ий ых ах им ию ши че че ше уж же ту
ру ду ну му лу су ку бо бы ми ди си зи ви ри сл се со сь ся чт ег ее ие ле ме не пе ре те фо он ей ев ял ям яв бе ба ги го гр
да до др ду ел ен ер ет жд жи за зд зн иг из ил ис ич кр кт ла ли ло лс мн мы на не ни но об од ок ол ом оп ор ос от оч пи пл
по пр пу ра ре ри ро ры са св се сп ст су сы та те ти то ту тя уд уп ус уч хо цы ча чи шк ща щи эт юб юч яз яз ыв ых ьн ьс ьк
ющ ящ ой ий ый ое ые ая ую юю ть ст ич ик ам ям ом ем им ым ах ях ов ев""".split())
_EN_BI = set("""th he in er an re on at en nd ti es or te of ed is it al ar st to nt ng se ha as ou io le ve co me de hi ri ro ic
ne ea ra ce li ch ll be ma si om ur ca el ta la ns di fo ho pe ec pr no ct us ac ot il tr ly nc et ut ss so rs un lo wa ge ie
wh ee wi em ad ol rt po we na ul ni ts mo ow pa im mi ai sh ir su id os iv ia am fi ci vi pl ig tu ev ld ry mp fe bl ab gh ty
op wo sa ay ex ke fr oo av ag if ap gr od bo sp rd do uc bu ei ov by rm ep tt oc fa ef cu rn sc gi da yo cr cl du ga qu ue ff
ba ey ls va um pp ua up lu go ht ru ug ds lt pi rc rr eg au ck ew mu br bi pt ak pu ui rg ib tl ny ki rk ys ob mm fu ph og ms
ye ud mb ip ub oi rl gu dr hr cc tw ft wn nu af hu nn eo vo rv nf xp gn sm fl iz ok nl my gl aw ju oa eq sy sl ps jo lf nv je
nk kn gs dy hy ze ks xt bs ik dd cy rp sk xi oe oy ws lv dl rf eu dg wr xa yi nm eb rb tm xc eh tc gy ja hn yp za""".split())
# Частые русские слова — из них добираем русские пары букв (чтобы «звук», «ферме» не сочли ошибкой раскладки)
_RU_WORDS = """привет пока спасибо пожалуйста да нет хорошо ладно как дела что где когда почему зачем сколько кто какой
открой закрой включи выключи запусти поставь сделай напомни найди покажи скажи напиши отправь позвони прочитай запиши
звук громкость тише громче музыку музыка песню трек паузу дальше назад следующий компьютер ноутбук экран свет лампу ферму
ферме ферма дом дома работа работу завтра сегодня вчера утром вечером ночью неделю месяц год время часов минут через
телеграм браузер почту письмо файл папку документ фото картинку видео погода курс доллар евро рубль такси маршрут адрес
задачи задачу сделки встречу календарь событие идея идеи проект проекты котел котла температура давление влажность
мне тебе нам вам его её их меня тебя себя мой твой наш ваш свой этот тот весь всё все очень уже еще ещё только тоже также
можно нужно надо хочу могу может будет было есть был была были буду будешь сейчас потом теперь тогда здесь там туда сюда
человек люди друг брат сестра мама папа жена муж сын дочь ребенок деньги цена купить продать заказ доставка курьер приедет
кухня спальня зал гостиная коридор ванная туалет окно дверь шторы кондиционер обогреватель чайник пылесос телевизор розетка
проводник диск диске рабочий каталог перекинь перешли скинь забери файлы второй первый третий быстро срочно спокойной ночи
доброе утро добрый день вечер""".split()
_RU_BI |= {w[i:i + 2] for w in _RU_WORDS for i in range(len(w) - 1)}
# Латинские названия, которые часто набирают русскими буквами («ызщешан» → spotify)
_EN_NAMES = set("""spotify chrome telegram steam discord youtube google word excel zoom skype whatsapp viber opera firefox
notepad yandex outlook photoshop obs aimp winamp rekordbox edge paint teams slack notion github figma vlc netflix twitch
windows office powerpoint teamviewer anydesk bitrix explorer total commander utorrent qbittorrent""".split())
_SKIP = re.compile(r"[\d/@:_\\#&=%]|^https?|www\.")


def _score(word: str, bigrams: set) -> float:
    w = word.lower()
    pairs = [w[i:i + 2] for i in range(len(w) - 1)]
    return sum(p in bigrams for p in pairs) / len(pairs) if pairs else 0.0


def _layout_word(tok: str) -> str:
    """Одно слово: если в своей раскладке оно бессмысленно, а в другой — похоже на слово, меняем."""
    core, tail = re.match(r"^(.*?)([!?.,]*)$", tok).groups()
    if len(core) < 3 or _SKIP.search(core):
        return tok
    if all(c.lower() in _EN or c.lower() in _EN.upper() or c in _EN2RU for c in core):        # латиница → русский?
        ru = "".join(_EN2RU.get(c, c) for c in core)
        en_s, ru_s = _score(core, _EN_BI), _score(ru, _RU_BI)
        no_vowel = not re.search(r"[aeiouy]", core, re.I)
        if ru_s >= 0.5 and (ru_s - en_s >= 0.3 or (no_vowel and len(core) >= 4)):
            return ru + tail
    elif re.fullmatch(r"[а-яё]+", core, re.I) and len(core) >= 3:                 # русскими буквами — латинское название
        en = "".join(_RU2EN.get(c, c) for c in core)
        if en.lower() in _EN_NAMES:
            return en + tail
    return tok


_SHIFT_RU = {"&": "?", "?": ",", "^": ":", "$": ";", "@": '"', "#": "№", "/": "."}


def fix_layout(text: str) -> str:
    """Исправить слова, набранные не в той раскладке. Команды (/…), ссылки, коды и голосовые не трогаем.
    Если почти всё сообщение набрано латиницей вместо русского — переводим и короткие слова («d» → «в»)."""
    t = text or ""
    if not t.strip() or t.lstrip().startswith(("/", "[")) or len(t) > 1000:   # команды, голосовые, файлы, длинные тексты
        return t
    toks = re.findall(r"\S+", t)
    fixed = [_layout_word(x) for x in toks]
    latin = [i for i, x in enumerate(toks) if re.search(r"[a-z]", x, re.I) and not _SKIP.search(x.rstrip("&?"))]
    changed = [i for i in latin if fixed[i] != toks[i] and re.search(r"[а-яё]", fixed[i], re.I)]
    if changed and not re.search(r"[а-яё]", t, re.I):   # всё сообщение — русский в английской раскладке
        for i in latin:
            if i in changed or not re.fullmatch(r"[a-z`\[\];',.&?^$@#/{}:\"<>~]+", toks[i], re.I):
                continue
            core = toks[i].rstrip("&?.,!")
            ru = "".join(_EN2RU.get(c, _SHIFT_RU.get(c, c)) for c in toks[i])
            if len(core) <= 2 or _score(ru.rstrip("?.,!"), _RU_BI) >= _score(core, _EN_BI) - 0.25:
                fixed[i] = ru
    it = iter(fixed)
    return re.sub(r"\S+", lambda m: next(it), t)
