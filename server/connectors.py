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


# ═════════════════════════ 5. Яндекс Расписания ═════════════════════════

RASP_API = "https://api.rasp.yandex.net/v3.0/"
RASP_SUGGEST = "https://suggests.rasp.yandex.net/all_suggests"
RASP_TYPES = {"train": "поезд", "suburban": "электричка", "bus": "автобус", "plane": "самолёт", "water": "водный транспорт"}


def rasp_suggest(query: str) -> List[dict]:
    """Код станции или города по названию (подсказки Яндекс Расписаний)."""
    r = requests.get(RASP_SUGGEST,
                     params={"part": query, "format": "old", "lang": "ru"}, timeout=15, headers=UA)
    r.raise_for_status()
    data = r.json()
    items = data[1] if isinstance(data, list) and len(data) > 1 else data.get("suggests", [])
    out = []
    for it in items:
        if isinstance(it, list) and len(it) >= 2:
            out.append({"code": it[0], "title": it[1], "region": it[2] if len(it) > 2 else ""})
        elif isinstance(it, dict) and it.get("point_key"):
            out.append({"code": it["point_key"], "title": it.get("title", ""), "region": it.get("region_title", "")})
    return out


def _rasp_code(q: str) -> tuple:
    q = q.strip()
    if re.fullmatch(r"[sc]\d+", q):
        return q, q
    found = rasp_suggest(q)
    if not found:
        raise ValueError(f"не нашёл станцию или город «{q}»")
    return found[0]["code"], found[0]["title"]


def rasp_test(cfg: dict) -> Optional[str]:
    try:
        r = requests.get(RASP_API + "nearest_settlement/", timeout=15, headers=UA,
                         params={"apikey": cfg["apikey"], "lat": 55.75, "lng": 37.62, "format": "json"})
    except requests.RequestException as e:
        return _err(e)
    if r.status_code in (401, 403):
        return "Яндекс не принял ключ. Проверь, что это ключ именно для «API Яндекс Расписаний»."
    return None if r.status_code == 200 else f"Яндекс ответил ошибкой {r.status_code}"


def rasp_run(cfg: dict, name: str, args: dict, tz) -> str:
    try:
        frm, frm_t = _rasp_code(str(args.get("from") or cfg.get("home") or ""))
        to, to_t = _rasp_code(str(args.get("to") or ""))
    except ValueError as e:
        return f"Ошибка: {e}."
    day = str(args.get("date") or datetime.now(tz).strftime("%Y-%m-%d"))
    params = {"apikey": cfg["apikey"], "from": frm, "to": to, "date": day, "format": "json", "lang": "ru_RU",
              "transfers": "false", "limit": 200}
    if args.get("transport") in RASP_TYPES:
        params["transport_types"] = args["transport"]
    r = requests.get(RASP_API + "search/", params=params, timeout=20, headers=UA)
    if r.status_code != 200:
        return f"Яндекс Расписания ответили ошибкой {r.status_code}."
    now = datetime.now(tz)
    rows = []
    for s in r.json().get("segments", []):
        try:
            dep = datetime.fromisoformat(s["departure"]).astimezone(tz)
            arr = datetime.fromisoformat(s["arrival"]).astimezone(tz)
        except (KeyError, ValueError):
            continue
        if day == now.strftime("%Y-%m-%d") and dep < now - timedelta(minutes=1):
            continue
        th = s.get("thread", {})
        kind = RASP_TYPES.get(th.get("transport_type", ""), th.get("transport_type", ""))
        extra = th.get("express_type") and " (экспресс)" or ""
        rows.append(f"- {dep:%H:%M} → {arr:%H:%M}: {kind}{extra} {th.get('title', '')}".rstrip())
        if len(rows) >= int(args.get("limit") or 6):
            break
    head = f"{frm_t} → {to_t}, {datetime.strptime(day, '%Y-%m-%d'):%d.%m}"
    return head + ":\n" + ("\n".join(rows) if rows else "рейсов больше нет") + \
        "\n(Данные: Яндекс Расписания, rasp.yandex.ru)"


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
    "rasp": {
        "title": "Электрички и автобусы (Яндекс Расписания)",
        "about": "«Когда ближайшая электричка до города?» — расписание поездов, электричек, автобусов.",
        "fields": [{"name": "apikey", "label": "Ключ API Яндекс Расписаний", "secret": True},
                   {"name": "home", "label": "Твоя станция по умолчанию (необязательно)",
                    "placeholder": "например «Подольск»", "optional": True}],
        "hint": r"электричк|поезд|автобус|расписани|рейс|станци|вокзал|ласточк|когда (?:ближайш|следующ)",
        "tools": [fn("rasp_search", "Расписание транспорта между станциями/городами на дату (ближайшие рейсы).", {
            "from": {"type": "string", "description": "откуда — станция или город; пусто — станция пользователя"},
            "to": {"type": "string", "description": "куда — станция или город"},
            "date": {"type": "string", "description": "YYYY-MM-DD, по умолчанию сегодня"},
            "transport": {"type": "string", "enum": list(RASP_TYPES)},
            "limit": {"type": "integer"}}, ["to"])],
        "test": rasp_test, "run": rasp_run,
        "help": """
Ключ бесплатный — для личного некоммерческого использования.

1. Открой https://developer.tech.yandex.ru/services и войди с Яндекс ID.
2. **Подключить API** → выбери **API Яндекс Расписаний** → заполни короткую анкету (цель — личный помощник).
3. На странице сервиса появится ключ — скопируй его.
4. Вставь ключ сюда. По желанию укажи свою станцию — тогда можно спрашивать просто «когда электричка до Москвы».

Ключ начинает работать не сразу — иногда через 15–30 минут после выдачи.
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


def _yiot(cfg: dict, method: str, path: str, body: Optional[dict] = None) -> dict:
    r = requests.request(method, YIOT_API + path, json=body, timeout=20,
                         headers={"Authorization": f"Bearer {cfg['token']}", **UA})
    if r.status_code in (401, 403):
        raise PermissionError("Яндекс не принял токен (он мог истечь — токен живёт около года)")
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "error":
        raise RuntimeError(data.get("message") or "Яндекс ответил ошибкой")
    return data


def yiot_test(cfg: dict) -> Optional[str]:
    try:
        _yiot(cfg, "GET", "user/info")
    except PermissionError as e:
        return str(e) + ". Получи новый токен по инструкции."
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


YIOT_CONNECTOR = {
    "title": "Умный дом Яндекса (Алиса)",
    "about": "Свет, розетки, кондиционеры, шторы, сценарии Алисы: «выключи свет в спальне».",
    "fields": [{"name": "token", "label": "Токен умного дома Яндекса", "secret": True}],
    "hint": r"свет|ламп|розетк|умн|дом[еау]? |алис|сценари|кондиционер|обогрев|штор|пылесос|чайник|увлажнит|температур|влажн",
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
    "help": """
Трой управляет устройствами из приложения **Дом с Алисой** через официальный API Яндекса. Нужен токен — это бесплатно,
займёт минуты три. Замки и охранные устройства Трой не трогает.

**1. Зарегистрируй «приложение» (один раз)**
1. Открой https://oauth.yandex.ru/client/new и войди тем же Яндекс ID, что и в «Доме с Алисой».
2. Название — любое, например «Трой».
3. Платформа — **Веб-сервисы**, в поле Redirect URI вставь: https://oauth.yandex.ru/verification_code
4. В доступах найди **Умный дом Яндекса** и отметь оба: **просмотр списка устройств** (iot:view)
   и **управление устройствами** (iot:control).
5. Нажми **Создать приложение** и скопируй **ClientID** (строка из букв и цифр).

**2. Получи токен**
1. Открой ссылку, подставив свой ClientID вместо ВАШ_CLIENT_ID:
   https://oauth.yandex.ru/authorize?response_type=token&client_id=ВАШ_CLIENT_ID
2. Нажми **Разрешить**. На странице появится токен (длинная строка) — скопируй его.
3. Вставь токен сюда.

Токен действует около года. Когда истечёт, Трой скажет — получишь новый по пункту 2.
Отозвать доступ можно в любой момент: https://id.yandex.ru/security → «Доступы».
"""}


# ═════════════════════════ Хранение и использование ═════════════════════════

CONNECTORS["yhome"] = YIOT_CONNECTOR


class Connections:
    """Доступы подключений — в базе Троя (таблица kv), отдельно от настроек."""

    def __init__(self, db: Callable, tz):
        self.db, self.tz = db, tz

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
        c = CONNECTORS.get(cid)
        if not c:
            return "нет такого подключения"
        cfg = {}
        for f in c["fields"]:
            v = str(values.get(f["name"]) or "").strip()
            if not v and not f.get("optional"):
                return f"заполни поле «{f['label']}»"
            cfg[f["name"]] = v
        err = c["test"](cfg)
        if err:
            return err
        data = self._all()
        data[cid] = cfg
        self._save(data)
        return None

    def disconnect(self, cid: str) -> None:
        data = self._all()
        data.pop(cid, None)
        self._save(data)

    def tools_for(self, texts: List[str]) -> tuple:
        """Функции для модели — только включённые и только когда разговор о них (экономим лимит)."""
        tools, rules = [], []
        for cid, cfg in self.enabled().items():
            c = CONNECTORS[cid]
            if any(re.search(c["hint"], t or "", re.I) for t in texts):
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
