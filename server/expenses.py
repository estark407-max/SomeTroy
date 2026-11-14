"""Учёт трат: фото чека или «потратил 1500 на бензин» → запись; «сколько ушло на топливо за месяц» → итог.

Чек читает сама модель (она видит картинки) и вызывает add_expense — по одному разу на каждую категорию чека.
Отчёт — текстом или таблицей (CSV открывается в Excel).
"""
import csv
import io
import time
from contextlib import closing
from datetime import datetime, timedelta
from typing import Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  TEXT NOT NULL,
    day      TEXT NOT NULL,
    amount   REAL NOT NULL,
    category TEXT NOT NULL,
    shop     TEXT NOT NULL DEFAULT '',
    note     TEXT NOT NULL DEFAULT '',
    ts       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS expenses_day ON expenses(chat_id, day);
"""
CATEGORIES = ["продукты", "топливо для котла", "запчасти и ремонт", "авто и бензин", "дом и хозяйство", "кафе и доставка",
              "здоровье", "одежда", "связь и подписки", "техника", "развлечения", "транспорт", "подарки", "другое"]


def init(db: Callable) -> None:
    with closing(db()) as con, con:
        con.executescript(SCHEMA)


def money(x: float) -> str:
    s = f"{x:,.2f}".replace(",", " ").replace(".00", "")
    return s.replace(".", ",") + " ₽"


def _day(text: str, tz) -> str:
    text = (text or "").strip()
    if not text:
        return datetime.now(tz).strftime("%Y-%m-%d")
    for f in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text[:10], f).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError("дата в виде YYYY-MM-DD")


def add(db: Callable, chat_id: str, a: dict, tz) -> str:
    try:
        amount = round(float(str(a.get("amount", "")).replace(" ", "").replace(",", ".")), 2)
        day = _day(str(a.get("date") or ""), tz)
    except ValueError as e:
        return f"Ошибка: {e if 'дата' in str(e) else 'сумма числом'}."
    if amount <= 0 or amount > 10_000_000:
        return "Ошибка: странная сумма — переспроси пользователя."
    cat = str(a.get("category") or "другое").strip().lower()
    if cat not in CATEGORIES:
        cat = next((c for c in CATEGORIES if cat[:5] and cat[:5] in c), "другое")
    shop, note = str(a.get("shop") or "").strip()[:80], str(a.get("note") or "").strip()[:200]
    with closing(db()) as con, con:
        twin = con.execute("SELECT id FROM expenses WHERE chat_id=? AND day=? AND amount=? AND shop=? AND category=?",
                           (str(chat_id), day, amount, shop, cat)).fetchone()
        if twin and not a.get("force"):
            return (f"НЕ ЗАПИСАНО: такая трата уже есть (№{twin[0]}: {money(amount)}, {day}, {shop or cat}). "
                    "Спроси пользователя, это повтор или ещё одна такая же покупка (тогда вызови с force=true).")
        cur = con.execute("INSERT INTO expenses(chat_id, day, amount, category, shop, note, ts) VALUES (?,?,?,?,?,?,?)",
                          (str(chat_id), day, amount, cat, shop, note, int(time.time())))
    return f"✅ Записал трату №{cur.lastrowid}: {money(amount)} — {cat}" + (f", {shop}" if shop else "") + \
        (f" ({note})" if note else "") + f", {datetime.strptime(day, '%Y-%m-%d'):%d.%m.%Y}."


def delete(db: Callable, chat_id: str, eid: int) -> str:
    with closing(db()) as con, con:
        row = con.execute("SELECT * FROM expenses WHERE chat_id=? AND id=?", (str(chat_id), eid)).fetchone()
        if not row:
            return f"Траты №{eid} нет."
        con.execute("DELETE FROM expenses WHERE id=?", (eid,))
    return f"Удалил трату №{eid}: {money(row['amount'])}, {row['category']}, {row['day']}."


def _period(a: dict, tz) -> tuple:
    now = datetime.now(tz)
    lo = _day(str(a.get("date_from") or ""), tz) if a.get("date_from") else now.replace(day=1).strftime("%Y-%m-%d")
    hi = _day(str(a.get("date_to") or ""), tz) if a.get("date_to") else now.strftime("%Y-%m-%d")
    return lo, hi


def report(db: Callable, chat_id: str, a: dict, tz) -> tuple:
    """(текст, файл или None). by: category | shop | day | month | list."""
    try:
        lo, hi = _period(a, tz)
    except ValueError as e:
        return f"Ошибка: {e}.", None
    by = a.get("by") or "category"
    sql = "SELECT * FROM expenses WHERE chat_id=? AND day>=? AND day<=?"
    args: list = [str(chat_id), lo, hi]
    cat = str(a.get("category") or "").strip().lower()
    if cat:
        sql += " AND (category LIKE ? OR shop LIKE ? OR note LIKE ?)"
        args += [f"%{cat[:5]}%"] * 3
    with closing(db()) as con:
        rows = con.execute(sql + " ORDER BY day, id", args).fetchall()
    span = f"{datetime.strptime(lo, '%Y-%m-%d'):%d.%m.%Y} — {datetime.strptime(hi, '%Y-%m-%d'):%d.%m.%Y}"
    if not rows:
        return f"Трат за {span}{' по «' + cat + '»' if cat else ''} не записано.", None
    total = sum(r["amount"] for r in rows)
    head = f"Траты за {span}{' по «' + cat + '»' if cat else ''}: {money(total)} ({len(rows)} записей)."
    if by == "list":
        body = [f"№{r['id']} {r['day'][8:]}.{r['day'][5:7]} — {money(r['amount'])}, {r['category']}"
                + (f", {r['shop']}" if r["shop"] else "") + (f" ({r['note']})" if r["note"] else "") for r in rows[-40:]]
        if len(rows) > 40:
            body.insert(0, f"(последние 40 из {len(rows)})")
    else:
        key = {"shop": lambda r: r["shop"] or "без магазина", "day": lambda r: r["day"],
               "month": lambda r: r["day"][:7]}.get(by, lambda r: r["category"])
        sums: dict = {}
        for r in rows:
            sums[key(r)] = sums.get(key(r), 0) + r["amount"]
        order = sorted(sums.items()) if by in ("day", "month") else sorted(sums.items(), key=lambda x: -x[1])
        body = [f"- {k}: {money(v)}" + (f" ({v / total * 100:.0f}%)" if by in ("category", "shop") else "")
                for k, v in order]
    text = head + "\n" + "\n".join(body)
    if not a.get("file"):
        return text, None
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["№", "Дата", "Сумма", "Категория", "Магазин", "Комментарий"])
    for r in rows:
        w.writerow([r["id"], datetime.strptime(r["day"], "%Y-%m-%d").strftime("%d.%m.%Y"),
                    f"{r['amount']:.2f}".replace(".", ","), r["category"], r["shop"], r["note"]])
    w.writerow([])
    w.writerow(["", "Итого", f"{total:.2f}".replace(".", ","), "", "", ""])
    name = f"Траты {lo} — {hi}.csv"
    return text + "\n\nТаблица — в файле (открывается в Excel).", (name, ("﻿" + buf.getvalue()).encode("utf-8"))


def month_line(db: Callable, chat_id: str, tz) -> str:
    """Для утренней сводки: сколько потрачено с начала месяца (если что-то записано)."""
    now = datetime.now(tz)
    lo = now.replace(day=1).strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    with closing(db()) as con:
        m = con.execute("SELECT SUM(amount), COUNT(*) FROM expenses WHERE chat_id=? AND day>=?", (str(chat_id), lo)).fetchone()
        y = con.execute("SELECT SUM(amount) FROM expenses WHERE chat_id=? AND day=?", (str(chat_id), yesterday)).fetchone()
    if not m[1]:
        return ""
    return f"💸 Траты с начала месяца: {money(m[0])}" + (f", вчера — {money(y[0])}" if y[0] else "") + "."
