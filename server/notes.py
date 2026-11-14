"""Долгая память Троя: заметки «запомни…» и поиск по всей переписке за год.

Заметки — короткие факты от пользователя («фильтр котла менял 3 ноября», «код от калитки 4512»).
Самые свежие Трой держит в голове всегда (они идут в подсказку модели), остальные находит поиском.
Поиск — по корням слов, без внешних библиотек: «котла», «котлом», «котёл» находят друг друга.
"""
import re
import time
from contextlib import closing
from datetime import datetime
from typing import Callable, List

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    text    TEXT NOT NULL,
    ts      INTEGER NOT NULL
);
"""
PROMPT_NOTES = 40            # сколько последних заметок Трой помнит без поиска
PROMPT_CHARS = 2500
STOP = set("""и в во на не что это как так а но да нет то же ли бы мне меня мой моя мои я ты он она мы вы они
у к о об от до по за из с со для при про над под был была было были есть будет где когда какой какая какие
который которая там тут здесь все всё еще ещё уже или ну вот тоже очень""".split())


def init(db: Callable) -> None:
    with closing(db()) as con, con:
        con.executescript(SCHEMA)


def norm(t: str) -> str:
    return (t or "").lower().replace("ё", "е")


def stems(text: str) -> List[str]:
    """Корни слов запроса: «котла» → «котл», «фильтр» → «филь», «4512» → «4512»."""
    out = []
    for w in re.findall(r"[a-zа-я0-9]+", norm(text)):
        if w in STOP or len(w) < 2:
            continue
        if w.isdigit() or len(w) <= 3:
            out.append(w)
        else:
            out.append(w[:max(3, min(len(w) - 2, 6))] if not w.isascii() else w[:max(3, min(len(w) - 1, 7))])
    return list(dict.fromkeys(out))


def add(db: Callable, chat_id: str, text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return "Ошибка: нечего запоминать."
    with closing(db()) as con, con:
        same = con.execute("SELECT id FROM notes WHERE chat_id=? AND text=?", (str(chat_id), text)).fetchone()
        if same:
            return f"Это я уже помню (заметка №{same[0]})."
        cur = con.execute("INSERT INTO notes(chat_id, text, ts) VALUES (?,?,?)", (str(chat_id), text, int(time.time())))
    return f"Запомнил (заметка №{cur.lastrowid}): {text}"


def delete(db: Callable, chat_id: str, nid: int = 0, query: str = "") -> str:
    with closing(db()) as con, con:
        if nid:
            row = con.execute("SELECT id, text FROM notes WHERE chat_id=? AND id=?", (str(chat_id), nid)).fetchone()
        else:
            found = _search_notes(con, chat_id, query, 2)
            if len(found) > 1:
                return "Под это подходит несколько заметок — какую забыть?\n" + "\n".join(
                    f"№{r['id']}: {r['text']}" for r in found)
            row = found[0] if found else None
        if not row:
            return "Такой заметки нет."
        con.execute("DELETE FROM notes WHERE id=?", (row["id"],))
    return f"Забыл: {row['text']}"


def prompt_block(db: Callable, chat_id: str) -> str:
    """То, что Трой помнит всегда: последние заметки — для подсказки модели."""
    with closing(db()) as con:
        rows = con.execute("SELECT id, text, ts FROM notes WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                           (str(chat_id), PROMPT_NOTES)).fetchall()
    if not rows:
        return ""
    lines, size = [], 0
    for r in rows:
        line = f"- №{r['id']} ({datetime.fromtimestamp(r['ts']):%d.%m.%Y}): {r['text']}"
        size += len(line)
        if size > PROMPT_CHARS:
            break
        lines.append(line)
    return "\n\nЧто пользователь просил запомнить (используй, когда к месту; не пересказывай без повода):\n" + "\n".join(lines)


def _score(text: str, keys: List[str]) -> int:
    t = norm(text)
    return sum(k in t for k in keys)


def _need(keys: List[str]) -> int:
    return 1 if len(keys) <= 2 else max(2, (len(keys) + 1) // 2)


def _search_notes(con, chat_id: str, query: str, limit: int) -> list:
    keys = stems(query)
    if not keys:
        return []
    rows = con.execute("SELECT id, text, ts FROM notes WHERE chat_id=? ORDER BY id DESC", (str(chat_id),)).fetchall()
    scored = [(s, r) for r in rows for s in [_score(r["text"], keys)] if s >= _need(keys)]
    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    return [r for _, r in scored[:limit]]


def search(db: Callable, chat_id: str, query: str, tz, date_from: str = "", date_to: str = "", limit: int = 12) -> str:
    """Поиск по заметкам и переписке. Даты — YYYY-MM-DD (необязательно)."""
    keys = stems(query)
    lo = hi = None
    try:
        if date_from:
            lo = int(datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=tz).timestamp())
        if date_to:
            hi = int(datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=tz).timestamp()) + 86400
    except ValueError:
        return "Ошибка: даты в виде YYYY-MM-DD."
    if not keys and lo is None:
        return "Ошибка: о чём искать? Нужны слова (или период)."
    out = []
    with closing(db()) as con:
        if keys:
            for r in _search_notes(con, chat_id, query, 5):
                out.append(f"[заметка №{r['id']}, {datetime.fromtimestamp(r['ts'], tz):%d.%m.%Y}] {r['text']}")
        sql = "SELECT role, text, ts FROM message_log WHERE chat_id=?"
        args: list = [str(chat_id)]
        if lo is not None:
            sql += " AND ts>=?"
            args.append(lo)
        if hi is not None:
            sql += " AND ts<?"
            args.append(hi)
        rows = con.execute(sql + " ORDER BY id DESC LIMIT 30000", args).fetchall()
    need = _need(keys) if keys else 0
    hits = []
    for r in rows:
        if r["role"] not in ("user", "assistant", "pc_user"):
            continue
        s = _score(r["text"], keys) if keys else 1
        if s >= need:
            hits.append((s, r))
    recent_q = time.time() - 120                  # сам этот вопрос (и ответ на него) не находим
    hits = [h for h in hits if h[1]["ts"] < recent_q]
    hits.sort(key=lambda x: (-x[0], -x[1]["ts"]))
    for s, r in hits[:limit]:
        who = "Пользователь" if r["role"] in ("user", "pc_user") else "Трой"
        text = re.sub(r"\s+", " ", r["text"])
        if len(text) > 400 and keys:              # длинное — показываем кусок вокруг найденного слова
            i = min((norm(text).find(k) for k in keys if k in norm(text)), default=0)
            text = ("…" if i > 150 else "") + text[max(0, i - 150):i + 250] + "…"
        out.append(f"[{datetime.fromtimestamp(r['ts'], tz):%d.%m.%Y %H:%M}] {who}: {text}")
    if not out:
        return "Ничего не нашёл ни в заметках, ни в переписке за год."
    more = f"\n(найдено сообщений: {len(hits)}, показаны самые подходящие)" if len(hits) > limit else ""
    return "\n".join(out) + more
