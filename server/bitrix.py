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


class Bitrix:
    def __init__(self, webhook: str, proxy: str = ""):
        self.base = webhook.rstrip("/") + "/"
        self.s = requests.Session()
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self._me: Optional[dict] = None
        self._users: dict = {}
        self._statuses: Optional[dict] = None

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

    def status_names(self) -> dict:
        if self._statuses is None:
            self._statuses = {s["STATUS_ID"]: s["NAME"] for s in self.list_all("crm.status.list", {})}
        return self._statuses

    # ── задачи ──
    def tasks(self, scope: str = "my", query: str = "", limit: int = 20) -> str:
        me = self.me()["ID"]
        flt = {"!STATUS": [5, 7]}
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
            flt = {"RESPONSIBLE_ID": me, "STATUS": 5}
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

    def task_create(self, title: str, description: str = "", deadline: str = "", responsible: str = "") -> str:
        fields = {"TITLE": title, "DESCRIPTION": description, "RESPONSIBLE_ID": responsible or self.me()["ID"]}
        if deadline:
            fields["DEADLINE"] = deadline.replace(" ", "T")
        res = self.call("tasks.task.add", {"fields": fields})["result"]
        tid = res.get("task", {}).get("id")
        return f"Задача #{tid} создана: {title}"

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
        st = self.status_names()
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
    def stats(self, entity: str, date_from: str = "", date_to: str = "", group_by: str = "stage") -> str:
        """Сводка по сделкам, лидам или задачам за период: количество и суммы с разбивкой."""
        if entity == "task":
            flt: dict = {}
            if date_from:
                flt[">=CREATED_DATE"] = date_from
            if date_to:
                flt["<=CREATED_DATE"] = date_to + " 23:59:59"
            items = self.list_all("tasks.task.list", {"filter": flt, "select": [
                "ID", "STATUS", "RESPONSIBLE_ID", "DEADLINE", "CLOSED_DATE"]}, "tasks", 5000)
            now = datetime.now().isoformat()
            by_status, by_user, overdue = defaultdict(int), defaultdict(int), 0
            for t in items:
                by_status[TASK_STATUS.get(str(t.get("status")), "?")] += 1
                by_user[self.user_name(t.get("responsibleId"))] += 1
                if t.get("deadline") and t["deadline"] < now and str(t.get("status")) not in ("5", "7"):
                    overdue += 1
            return (f"Задач: {len(items)}, просрочено: {overdue}\n"
                    f"По статусам: {dict(by_status)}\nПо исполнителям: {dict(by_user)}")

        if entity not in ("deal", "lead"):
            return "Ошибка: аналитика есть для deal, lead и task."
        flt = {}
        if date_from:
            flt[">=DATE_CREATE"] = date_from
        if date_to:
            flt["<=DATE_CREATE"] = date_to + "T23:59:59"
        stage_f = "STAGE_ID" if entity == "deal" else "STATUS_ID"
        select = ["ID", stage_f, "OPPORTUNITY", "CURRENCY_ID", "ASSIGNED_BY_ID", "SOURCE_ID", "DATE_CREATE"]
        if entity == "deal":
            select += ["CLOSED", "STAGE_SEMANTIC_ID", "CATEGORY_ID"]
        items = self.list_all(f"crm.{entity}.list", {"filter": flt, "select": select}, limit=5000)
        if not items:
            return "За период ничего нет."
        st = self.status_names()
        key_f = {"stage": stage_f, "manager": "ASSIGNED_BY_ID", "source": "SOURCE_ID",
                 "month": "DATE_CREATE"}.get(group_by, stage_f)
        groups: dict = defaultdict(lambda: [0, 0.0])
        total = 0.0
        won = lost = 0
        for i in items:
            k = i.get(key_f) or "—"
            if key_f == "ASSIGNED_BY_ID":
                k = self.user_name(k)
            elif key_f == "DATE_CREATE":
                k = k[:7]
            else:
                k = st.get(k, k)
            amount = float(i.get("OPPORTUNITY") or 0)
            groups[k][0] += 1
            groups[k][1] += amount
            total += amount
            sem = i.get("STAGE_SEMANTIC_ID")
            won += sem == "S"
            lost += sem == "F"
        lines = [f"{CRM_ENTITIES[entity].capitalize()}: {len(items)}, сумма {money(total)}"]
        if entity == "deal":
            closed = won + lost
            conv = f", конверсия в успех {won / closed:.0%}" if closed else ""
            lines.append(f"Выиграно {won}, проиграно {lost}, в работе {len(items) - closed}{conv}")
        for k, (n, s) in sorted(groups.items(), key=lambda x: -x[1][0]):
            lines.append(f"- {k}: {n} шт., {money(s)}")
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
    ("bitrix_task_create", "Битрикс24: создать задачу. Если исполнитель не я — сначала найди его через "
                           "bitrix_find_user. ТОЛЬКО после подтверждения пользователя.",
     {"title": {"type": "string"}, "description": {"type": "string"},
      "deadline": {"type": "string", "description": "ГГГГ-ММ-ДД ЧЧ:ММ, необязательно"},
      "responsible_id": {"type": "string", "description": "ID исполнителя, пусто — я сам"}}, ["title"]),
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
                     "либо сводка по задачам. group_by: stage, manager, source, month.",
     {"entity": {"type": "string", "enum": ["deal", "lead", "task"]},
      "date_from": {"type": "string", "description": "ГГГГ-ММ-ДД"},
      "date_to": {"type": "string", "description": "ГГГГ-ММ-ДД"},
      "group_by": {"type": "string", "enum": ["stage", "manager", "source", "month"]}}, ["entity"]),
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
        return bx.stats(a["entity"], a.get("date_from") or "", a.get("date_to") or "", a.get("group_by") or "stage")
    if name == "bitrix_read":
        return bx.read_any(a["method"], dict(a.get("params") or {}))
    return f"Ошибка: неизвестная функция {name}"
