"""Битрикс24 для Троя: задачи, чаты, CRM и аналитика через входящий вебхук владельца.

Вебхук действует от имени того, кто его создал, с его правами.
"""
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

import requests

log = logging.getLogger("assistant.bitrix")

def money(x: float) -> str:
    return f"{x:_.0f}".replace("_", " ")


TASK_STATUS = {"1": "новая", "2": "ждёт выполнения", "3": "выполняется", "4": "ждёт контроля",
               "5": "завершена", "6": "отложена", "7": "отклонена"}
CRM_ENTITIES = {"deal": "сделки", "lead": "лиды", "contact": "контакты", "company": "компании"}
# Методы, которые можно вызывать без подтверждения: только чтение
READ_METHOD_RE = re.compile(r"^[a-z0-9_.]+\.(list|get|fields|search|current|count|getlist|items)$|^(user\.current|profile)$",
                            re.I)


class BitrixError(RuntimeError):
    pass


DATE_RE = re.compile(r"^\s*(?:(\d{4})-(\d{1,2})-(\d{1,2})|(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?)"
                     r"(?:[ T]+(\d{1,2})[:.](\d{2})(?::\d{2})?)?")


def parse_when(text: str, tz=None, default_time: str = "") -> Optional[datetime]:
    """«2026-11-03 18:00», «2026-11-03», «03.11.2026 18:00», «03.11» → дата-время (в часовом поясе Троя)."""
    m = DATE_RE.match(text or "")
    if not m:
        return None
    now = datetime.now(tz)
    if m.group(1):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        d, mo = int(m.group(4)), int(m.group(5))
        y = int(m.group(6)) if m.group(6) else now.year
        y += 2000 if y < 100 else 0
    hh, mm = (int(m.group(7)), int(m.group(8))) if m.group(7) else (
        tuple(int(x) for x in default_time.split(":")) if default_time else (0, 0))
    try:
        return datetime(y, mo, d, hh, mm, tzinfo=tz)
    except ValueError:
        return None


def norm_date(text: str) -> str:
    """Дата для фильтров аналитики → ГГГГ-ММ-ДД (пусто — если не понял)."""
    dt = parse_when(text)
    return dt.strftime("%Y-%m-%d") if dt else ""


class Bitrix:
    def __init__(self, webhook: str, proxy: str = "", tz=None):
        self.base = webhook.rstrip("/") + "/"
        self.tz = tz
        self.s = requests.Session()
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self._me: Optional[dict] = None
        self._users: dict = {}
        self._statuses: Optional[list] = None

    # ── транспорт ──
    def call(self, method: str, params: Optional[dict] = None) -> dict:
        for attempt in range(3):
            r = self.s.post(self.base + method + ".json", json=params or {}, timeout=60)
            try:
                data = r.json()
            except ValueError:
                raise BitrixError(f"{method}: HTTP {r.status_code}")
            if data.get("error") == "QUERY_LIMIT_EXCEEDED" and attempt < 2:
                time.sleep(1.5)
                continue
            if "error" in data:
                raise BitrixError(f"{method}: {data.get('error_description') or data['error']}")
            return data
        raise BitrixError(f"{method}: превышен лимит запросов")

    def list_all(self, method: str, params: dict, key: Optional[str] = None, limit: int = 2000) -> list:
        """Постраничная выборка (Битрикс отдаёт по 50 записей)."""
        out, start = [], 0
        while len(out) < limit:
            data = self.call(method, {**params, "start": start})
            res = data.get("result")
            items = (res.get(key) if key and isinstance(res, dict) else res) or []
            out.extend(items)
            if "next" not in data:
                break
            start = data["next"]
        return out[:limit]

    # ── справочники ──
    def me(self) -> dict:
        if not self._me:
            self._me = self.call("user.current")["result"]
        return self._me

    def user_name(self, uid) -> str:
        uid = str(uid or "")
        if not uid:
            return "—"
        if uid not in self._users:
            try:
                u = (self.call("user.get", {"ID": uid}).get("result") or [{}])[0]
                self._users[uid] = f"{u.get('NAME', '')} {u.get('LAST_NAME', '')}".strip() or f"id{uid}"
            except BitrixError:
                self._users[uid] = f"id{uid}"
        return self._users[uid]

    def find_users(self, query: str) -> list:
        res = self.call("user.search", {"FIND": query}).get("result") or []
        return [{"id": u["ID"], "name": f"{u.get('NAME', '')} {u.get('LAST_NAME', '')}".strip(),
                 "position": u.get("WORK_POSITION") or ""} for u in res[:10]]

    def status_names(self, kind: str = "deal") -> dict:
        """Названия стадий. У сделок, лидов и источников коды совпадают («NEW»), поэтому — отдельно для каждого."""
        if self._statuses is None:
            self._statuses = self.list_all("crm.status.list", {})
        want = {"deal": lambda e: e.startswith("DEAL_STAGE"), "lead": lambda e: e == "STATUS",
                "source": lambda e: e == "SOURCE"}.get(kind, lambda e: True)
        return {s["STATUS_ID"]: s["NAME"] for s in self._statuses if want(str(s.get("ENTITY_ID") or ""))}

    def resolve_user(self, who: str) -> tuple:
        """Исполнитель: ID или имя/фамилия → (ID, имя). Несколько похожих — просим уточнить."""
        who = str(who or "").strip()
        if not who:
            me = self.me()
            return str(me["ID"]), f"{me.get('NAME', '')} {me.get('LAST_NAME', '')}".strip() or "я"
        if who.isdigit():
            return who, self.user_name(who)
        found = self.find_users(who)
        if not found and " " in who:
            found = self.find_users(who.split()[-1]) or self.find_users(who.split()[0])
        if len(found) == 1:
            return str(found[0]["id"]), found[0]["name"]
        if not found:
            raise BitrixError(f"не нашёл сотрудника «{who}» в Битриксе")
        names = "; ".join(f"{u['name']} (ID {u['id']}{', ' + u['position'] if u['position'] else ''})" for u in found)
        raise BitrixError(f"под «{who}» подходят несколько сотрудников: {names}. Уточни, кого именно")

    # ── задачи ──
    def tasks(self, scope: str = "my", query: str = "", limit: int = 20) -> str:
        me = self.me()["ID"]
        flt: dict = {"<REAL_STATUS": 5}                      # 1–4: новые, ждут, в работе, ждут контроля
        if scope == "my":
            flt["RESPONSIBLE_ID"] = me
        elif scope == "created_by_me":
            flt["CREATED_BY"] = me
        elif scope == "overdue":
            flt["RESPONSIBLE_ID"] = me
            flt["<DEADLINE"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        elif scope == "today":
            flt["RESPONSIBLE_ID"] = me
            flt["<=DEADLINE"] = datetime.now().strftime("%Y-%m-%dT23:59:59")
        elif scope == "done":
            flt = {"RESPONSIBLE_ID": me, "REAL_STATUS": 5}
        if query:
            flt["%TITLE"] = query
        items = self.list_all("tasks.task.list", {
            "filter": flt, "order": {"DEADLINE": "asc"},
            "select": ["ID", "TITLE", "DEADLINE", "STATUS", "RESPONSIBLE_ID", "CREATED_BY"]}, "tasks", limit)
        if not items:
            return "Задач не найдено."
        lines = []
        for t in items:
            dl = (t.get("deadline") or "")[:16].replace("T", " ") or "без срока"
            who = self.user_name(t.get("responsibleId"))
            lines.append(f"#{t['id']} {t['title']} — до {dl}, {TASK_STATUS.get(str(t.get('status')), '?')}, "
                         f"исполнитель {who}")
        return "\n".join(lines)

    def task_fields(self, title: str, description: str = "", deadline: str = "", responsible: str = "") -> tuple:
        """Поля задачи и её описание словами (для подтверждения). Ошибки — сразу, до «Делаю?»."""
        if not (title or "").strip():
            raise BitrixError("у задачи нет названия")
        uid, uname = self.resolve_user(responsible)
        fields = {"TITLE": title.strip(), "RESPONSIBLE_ID": uid}
        if description:
            fields["DESCRIPTION"] = description
        when = ""
        if deadline:
            dt = parse_when(deadline, self.tz, "18:00")
            if not dt:
                raise BitrixError(f"не понял срок «{deadline}» — нужен вид ГГГГ-ММ-ДД ЧЧ:ММ")
            fields["DEADLINE"] = dt.isoformat(timespec="seconds")
            when = dt.strftime("%d.%m.%Y %H:%M")
        text = f"«{fields['TITLE']}», исполнитель {uname}" + (f", срок {when}" if when else ", без срока")
        if description:
            text += f", описание: {description[:200]}"
        return fields, text

    def task_create(self, title: str, description: str = "", deadline: str = "", responsible: str = "") -> str:
        fields, text = self.task_fields(title, description, deadline, responsible)
        res = self.call("tasks.task.add", {"fields": fields})["result"]
        tid = (res.get("task") or {}).get("id") if isinstance(res, dict) else None
        if not tid:
            raise BitrixError("Битрикс не вернул номер задачи — задача, скорее всего, не создана")
        return f"✅ Задача #{tid} создана: {text}."

    def task_complete(self, task_id: int) -> str:
        self.call("tasks.task.complete", {"taskId": task_id})
        return f"Задача #{task_id} завершена."

    def task_comment(self, task_id: int, text: str) -> str:
        self.call("task.commentitem.add", {"TASKID": task_id, "FIELDS": {"POST_MESSAGE": text}})
        return f"Комментарий к задаче #{task_id} добавлен."

    # ── чаты ──
    def chats(self, unread_only: bool = True, limit: int = 15) -> str:
        items = (self.call("im.recent.list", {"SKIP_OPENLINES": "Y"}).get("result") or {}).get("items") or []
        if unread_only:
            items = [i for i in items if int(i.get("counter") or 0) > 0]
        if not items:
            return "Непрочитанных сообщений нет." if unread_only else "Чатов нет."
        lines = []
        for i in items[:limit]:
            msg = (i.get("message") or {}).get("text") or ""
            lines.append(f"[{i.get('id')}] {i.get('title')}: непрочитано {i.get('counter', 0)}; "
                         f"последнее: {msg[:150]}")
        return "\n".join(lines)

    def chat_messages(self, dialog_id: str, limit: int = 20) -> str:
        res = self.call("im.dialog.messages.get", {"DIALOG_ID": dialog_id, "LIMIT": min(limit, 50)})["result"]
        users = {str(u["id"]): u.get("name") for u in res.get("users") or []}
        msgs = list(reversed(res.get("messages") or []))
        if not msgs:
            return "Сообщений нет."
        return "\n".join(f"{(m.get('date') or '')[:16].replace('T', ' ')} "
                         f"{users.get(str(m.get('author_id')), 'система')}: {m.get('text', '')}" for m in msgs)

    def send_message(self, dialog_id: str, text: str) -> str:
        mid = self.call("im.message.add", {"DIALOG_ID": dialog_id, "MESSAGE": text})["result"]
        return f"Сообщение отправлено (№{mid})."

    # ── CRM ──
    def crm_find(self, entity: str, query: str = "", limit: int = 20) -> str:
        if entity not in CRM_ENTITIES:
            return "Ошибка: entity должен быть deal, lead, contact или company."
        flt: dict = {}
        if query:
            if query.isdigit():
                flt["ID"] = query
            elif entity in ("deal", "lead", "company"):
                flt["%TITLE"] = query
            else:
                flt["%LAST_NAME"] = query
        select = {"deal": ["ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "CURRENCY_ID", "ASSIGNED_BY_ID", "DATE_CREATE"],
                  "lead": ["ID", "TITLE", "STATUS_ID", "OPPORTUNITY", "CURRENCY_ID", "NAME", "LAST_NAME", "PHONE",
                           "ASSIGNED_BY_ID", "DATE_CREATE"],
                  "contact": ["ID", "NAME", "LAST_NAME", "PHONE", "EMAIL", "COMPANY_ID", "ASSIGNED_BY_ID"],
                  "company": ["ID", "TITLE", "PHONE", "EMAIL", "ASSIGNED_BY_ID"]}[entity]
        items = self.list_all(f"crm.{entity}.list", {"filter": flt, "select": select,
                                                     "order": {"DATE_CREATE": "DESC"}}, limit=limit)
        if not items and entity == "contact" and query and not query.isdigit():
            items = self.list_all("crm.contact.list", {"filter": {"%NAME": query}, "select": select}, limit=limit)
        if not items:
            return "Ничего не найдено."
        st = self.status_names(entity)
        lines = []
        for i in items:
            name = i.get("TITLE") or f"{i.get('NAME', '')} {i.get('LAST_NAME', '')}".strip()
            parts = [f"#{i['ID']} {name}"]
            if i.get("STAGE_ID") or i.get("STATUS_ID"):
                sid = i.get("STAGE_ID") or i.get("STATUS_ID")
                parts.append(f"стадия: {st.get(sid, sid)}")
            if i.get("OPPORTUNITY"):
                parts.append(f"сумма: {money(float(i['OPPORTUNITY']))} {i.get('CURRENCY_ID', '')}")
            for f in ("PHONE", "EMAIL"):
                if i.get(f):
                    parts.append(", ".join(x.get("VALUE", "") for x in i[f]))
            parts.append(f"ответственный: {self.user_name(i.get('ASSIGNED_BY_ID'))}")
            lines.append("; ".join(parts))
        return "\n".join(lines)

    def crm_create(self, entity: str, fields: dict) -> str:
        if entity not in ("deal", "lead", "contact", "company"):
            return "Ошибка: entity должен быть deal, lead, contact или company."
        for f in ("PHONE", "EMAIL"):
            if isinstance(fields.get(f), str):
                fields[f] = [{"VALUE": fields[f], "VALUE_TYPE": "WORK"}]
        new_id = self.call(f"crm.{entity}.add", {"fields": fields})["result"]
        return f"Создано: {CRM_ENTITIES[entity]} #{new_id}."

    def crm_update(self, entity: str, item_id: int, fields: dict) -> str:
        self.call(f"crm.{entity}.update", {"id": item_id, "fields": fields})
        return f"Обновлено: {entity} #{item_id}."

    # ── аналитика ──
    def stats(self, entity: str, date_from: str = "", date_to: str = "", group_by: str = "stage",
              period_by: str = "created") -> str:
        """Сводка по сделкам, лидам или задачам за период: количество и суммы с разбивкой.
        period_by: created — по дате создания; closed — по дате закрытия (продажи, выручка, выполненные задачи)."""
        d1, d2 = norm_date(date_from), norm_date(date_to)
        if (date_from and not d1) or (date_to and not d2):
            return "Ошибка: даты нужны в виде ГГГГ-ММ-ДД."
        limit = 5000
        period = (f"с {d1} " if d1 else "") + (f"по {d2}" if d2 else "") or "за всё время"
        closed = period_by == "closed"

        if entity == "task":
            f = "CLOSED_DATE" if closed else "CREATED_DATE"
            flt: dict = {"REAL_STATUS": 5} if closed else {}
            if d1:
                flt[f">={f}"] = d1 + "T00:00:00"
            if d2:
                flt[f"<={f}"] = d2 + "T23:59:59"
            items = self.list_all("tasks.task.list", {"filter": flt, "select": [
                "ID", "STATUS", "RESPONSIBLE_ID", "DEADLINE", "CLOSED_DATE"]}, "tasks", limit)
            now = datetime.now(self.tz).isoformat() if self.tz else datetime.now().isoformat()
            by_status, by_user, overdue = defaultdict(int), defaultdict(int), 0
            for t in items:
                st = str(t.get("status"))
                by_status[TASK_STATUS.get(st, st)] += 1
                by_user[self.user_name(t.get("responsibleId"))] += 1
                if t.get("deadline") and t["deadline"] < now and st not in ("5", "7"):
                    overdue += 1
            head = f"Задачи {'закрытые' if closed else 'созданные'} {period}: {len(items)}"
            if not closed:
                head += f", из них просрочено сейчас: {overdue}"
            lines = [head, "По статусам: " + ", ".join(f"{k} — {v}" for k, v in sorted(by_status.items(), key=lambda x: -x[1])),
                     "По исполнителям: " + ", ".join(f"{k} — {v}" for k, v in sorted(by_user.items(), key=lambda x: -x[1]))]
            if len(items) >= limit:
                lines.append(f"(посчитаны первые {limit} задач — сузь период)")
            return "\n".join(lines)

        if entity not in ("deal", "lead"):
            return "Ошибка: аналитика есть для deal, lead и task."
        stage_f = "STAGE_ID" if entity == "deal" else "STATUS_ID"
        sem_f = "STAGE_SEMANTIC_ID" if entity == "deal" else "STATUS_SEMANTIC_ID"
        date_f = "CLOSEDATE" if (closed and entity == "deal") else ("DATE_CLOSED" if closed else "DATE_CREATE")
        flt = {}
        if closed and entity == "deal":
            flt["CLOSED"] = "Y"
        if d1:
            flt[f">={date_f}"] = d1 + ("" if date_f == "CLOSEDATE" else "T00:00:00")
        if d2:
            flt[f"<={date_f}"] = d2 + ("" if date_f == "CLOSEDATE" else "T23:59:59")
        select = ["ID", stage_f, sem_f, "OPPORTUNITY", "CURRENCY_ID", "ASSIGNED_BY_ID", "SOURCE_ID", "DATE_CREATE"]
        if entity == "deal":
            select += ["CLOSED", "CLOSEDATE", "CATEGORY_ID"]
        items = self.list_all(f"crm.{entity}.list", {"filter": flt, "select": select}, limit=limit)
        if not items:
            return f"{CRM_ENTITIES[entity].capitalize()} {period}: ничего нет."
        names = self.status_names(entity)
        sources = self.status_names("source")
        key_f = {"stage": stage_f, "manager": "ASSIGNED_BY_ID", "source": "SOURCE_ID",
                 "month": date_f if closed else "DATE_CREATE"}.get(group_by, stage_f)
        groups: dict = defaultdict(lambda: [0, defaultdict(float)])
        totals: dict = defaultdict(float)
        won_sum: dict = defaultdict(float)
        won = lost = 0
        for i in items:
            k = i.get(key_f) or "—"
            if key_f == "ASSIGNED_BY_ID":
                k = self.user_name(k)
            elif key_f in ("DATE_CREATE", "CLOSEDATE", "DATE_CLOSED"):
                k = str(k)[:7]
            elif key_f == "SOURCE_ID":
                k = sources.get(k, k)
            else:
                k = names.get(k, k)
            cur = i.get("CURRENCY_ID") or ""
            amount = float(i.get("OPPORTUNITY") or 0)
            groups[k][0] += 1
            groups[k][1][cur] += amount
            totals[cur] += amount
            sem = i.get(sem_f)
            if sem == "S":
                won += 1
                won_sum[cur] += amount
            elif sem == "F":
                lost += 1

        def sums(d: dict) -> str:
            return " + ".join(f"{money(v)} {c}".strip() for c, v in d.items() if v) or "0"

        what = CRM_ENTITIES[entity].capitalize()
        lines = [f"{what} {'закрытые' if closed else 'созданные'} {period}: {len(items)}, сумма {sums(totals)}"]
        done = won + lost
        if entity == "deal":
            lines.append(f"Успешных {won} на {sums(won_sum)}, провальных {lost}, в работе {len(items) - done}"
                         + (f"; конверсия в успех {won / done:.0%} от закрытых" if done else ""))
        else:
            lines.append(f"Сконвертировано {won}, в брак {lost}, в работе {len(items) - done}"
                         + (f"; конверсия {won / len(items):.0%}" if items else ""))
        for k, (n, s_) in sorted(groups.items(), key=lambda x: -x[1][0]):
            lines.append(f"- {k}: {n} шт., {sums(s_)}")
        if len(items) >= limit:
            lines.append(f"(посчитаны первые {limit} записей — сузь период)")
        return "\n".join(lines)

    def read_any(self, method: str, params: dict) -> str:
        if not READ_METHOD_RE.match(method):
            return "Ошибка: так можно вызывать только методы чтения (*.list, *.get, *.fields и т.п.)."
        data = self.call(method, params)
        text = json.dumps(data.get("result"), ensure_ascii=False)
        extra = f"\n(есть ещё записи, next={data['next']}, всего {data.get('total')})" if "next" in data else ""
        return text[:12000] + extra


TOOLS = [
    ("bitrix_tasks", "Битрикс24: список задач. scope: my (мои активные), today (на сегодня и просроченные), "
                     "overdue, created_by_me (поставленные мной), done (завершённые).",
     {"scope": {"type": "string", "enum": ["my", "today", "overdue", "created_by_me", "done"]},
      "query": {"type": "string", "description": "Слово из названия, необязательно"}}, []),
    ("bitrix_task_create", "Битрикс24: поставить задачу. Вызывай сразу, как понял задачу: Трой сам покажет "
                           "пользователю, что получится, и спросит «Делаю?».",
     {"title": {"type": "string"}, "description": {"type": "string"},
      "deadline": {"type": "string", "description": "ГГГГ-ММ-ДД ЧЧ:ММ, необязательно"},
      "responsible_id": {"type": "string", "description": "ID или имя и фамилия исполнителя; пусто — я сам"}},
     ["title"]),
    ("bitrix_task_complete", "Битрикс24: завершить задачу. ТОЛЬКО после подтверждения пользователя.",
     {"task_id": {"type": "integer"}}, ["task_id"]),
    ("bitrix_task_comment", "Битрикс24: комментарий к задаче. ТОЛЬКО после подтверждения пользователя.",
     {"task_id": {"type": "integer"}, "text": {"type": "string"}}, ["task_id", "text"]),
    ("bitrix_find_user", "Битрикс24: найти сотрудника по имени или фамилии (нужно для задач и сообщений).",
     {"query": {"type": "string"}}, ["query"]),
    ("bitrix_chats", "Битрикс24: список чатов, по умолчанию только с непрочитанными. В квадратных скобках — DIALOG_ID.",
     {"unread_only": {"type": "boolean"}}, []),
    ("bitrix_chat_messages", "Битрикс24: последние сообщения диалога.",
     {"dialog_id": {"type": "string", "description": "ID сотрудника или chatNNN"},
      "limit": {"type": "integer"}}, ["dialog_id"]),
    ("bitrix_send_message", "Битрикс24: отправить сообщение от имени пользователя. ТОЛЬКО после того, как "
                            "пользователь увидел точный текст и получателя и подтвердил.",
     {"dialog_id": {"type": "string", "description": "ID сотрудника или chatNNN"},
      "text": {"type": "string"}}, ["dialog_id", "text"]),
    ("bitrix_crm_find", "Битрикс24 CRM: найти сделки, лиды, контакты или компании по названию, фамилии или ID. "
                        "Без query — последние.",
     {"entity": {"type": "string", "enum": ["deal", "lead", "contact", "company"]},
      "query": {"type": "string"}}, ["entity"]),
    ("bitrix_crm_create", "Битрикс24 CRM: создать сделку, лид, контакт или компанию. Поля в формате Битрикса "
                          "(TITLE, NAME, LAST_NAME, PHONE, EMAIL, OPPORTUNITY, COMMENTS…). "
                          "ТОЛЬКО после подтверждения пользователя.",
     {"entity": {"type": "string", "enum": ["deal", "lead", "contact", "company"]},
      "fields": {"type": "object"}}, ["entity", "fields"]),
    ("bitrix_crm_update", "Битрикс24 CRM: изменить поля записи (например STAGE_ID, OPPORTUNITY). "
                          "ТОЛЬКО после подтверждения пользователя.",
     {"entity": {"type": "string", "enum": ["deal", "lead", "contact", "company"]},
      "id": {"type": "integer"}, "fields": {"type": "object"}}, ["entity", "id", "fields"]),
    ("bitrix_stats", "Битрикс24: аналитика за период — количество и суммы сделок или лидов, конверсия, "
                     "либо сводка по задачам. group_by: stage, manager, source, month. period_by: created — по дате "
                     "создания (сколько пришло); closed — по дате закрытия (продажи, выручка, сколько закрыто/выполнено).",
     {"entity": {"type": "string", "enum": ["deal", "lead", "task"]},
      "date_from": {"type": "string", "description": "ГГГГ-ММ-ДД"},
      "date_to": {"type": "string", "description": "ГГГГ-ММ-ДД"},
      "group_by": {"type": "string", "enum": ["stage", "manager", "source", "month"]},
      "period_by": {"type": "string", "enum": ["created", "closed"]}}, ["entity"]),
    ("bitrix_read", "Битрикс24: произвольный метод REST API только для чтения (*.list, *.get, *.fields) — "
                    "для нестандартных вопросов и аналитики, которых нет в других функциях.",
     {"method": {"type": "string", "description": "например crm.deal.list, crm.activity.list, crm.status.list"},
      "params": {"type": "object", "description": "filter, select, order и т.п."}}, ["method"]),
]


def execute(bx: Bitrix, name: str, a: dict) -> str:
    if name == "bitrix_tasks":
        return bx.tasks(a.get("scope") or "my", a.get("query") or "")
    if name == "bitrix_task_create":
        return bx.task_create(a["title"], a.get("description") or "", a.get("deadline") or "",
                              str(a.get("responsible_id") or ""))
    if name == "bitrix_task_complete":
        return bx.task_complete(int(a["task_id"]))
    if name == "bitrix_task_comment":
        return bx.task_comment(int(a["task_id"]), a["text"])
    if name == "bitrix_find_user":
        return json.dumps(bx.find_users(a["query"]), ensure_ascii=False) or "Никого не нашёл."
    if name == "bitrix_chats":
        return bx.chats(a.get("unread_only", True))
    if name == "bitrix_chat_messages":
        return bx.chat_messages(str(a["dialog_id"]), int(a.get("limit") or 20))
    if name == "bitrix_send_message":
        return bx.send_message(str(a["dialog_id"]), a["text"])
    if name == "bitrix_crm_find":
        return bx.crm_find(a["entity"], str(a.get("query") or ""))
    if name == "bitrix_crm_create":
        return bx.crm_create(a["entity"], dict(a.get("fields") or {}))
    if name == "bitrix_crm_update":
        return bx.crm_update(a["entity"], int(a["id"]), dict(a.get("fields") or {}))
    if name == "bitrix_stats":
        return bx.stats(a["entity"], a.get("date_from") or "", a.get("date_to") or "", a.get("group_by") or "stage",
                        a.get("period_by") or "created")
    if name == "bitrix_read":
        return bx.read_any(a["method"], dict(a.get("params") or {}))
    return f"Ошибка: неизвестная функция {name}"


def describe(bx: Bitrix, name: str, a: dict) -> str:
    """Что именно сделает функция-изменение — словами, чтобы пользователь подтвердил (ошибки — сразу)."""
    if name == "bitrix_task_create":
        _, text = bx.task_fields(a.get("title") or "", a.get("description") or "", a.get("deadline") or "",
                                 str(a.get("responsible_id") or ""))
        return "Поставить задачу " + text
    if name == "bitrix_task_complete":
        return f"Завершить задачу #{a.get('task_id')}"
    if name == "bitrix_task_comment":
        return f"Комментарий к задаче #{a.get('task_id')}: «{a.get('text', '')}»"
    if name == "bitrix_send_message":
        to = str(a.get("dialog_id") or "")
        who = bx.user_name(to) if to.isdigit() else to
        return f"Сообщение в Битрикс (кому: {who}): «{a.get('text', '')}»"
    if name == "bitrix_crm_create":
        return f"Создать в CRM ({CRM_ENTITIES.get(a.get('entity'), a.get('entity'))}): {json.dumps(a.get('fields') or {}, ensure_ascii=False)}"
    if name == "bitrix_crm_update":
        return f"Изменить {a.get('entity')} #{a.get('id')}: {json.dumps(a.get('fields') or {}, ensure_ascii=False)}"
    return name
