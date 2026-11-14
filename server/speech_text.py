"""Текст для озвучки: те же правила, что в программе на компьютере (desktop/voice.py — clean,
desktop/ui.html — speechParts). Сервер по ним заранее готовит голос первой фразы ответа,
пока ответ ещё летит до компьютера, — так Трой начинает говорить почти сразу."""
import re

EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿️‍]")


def clean(text: str, limit: int = 900) -> str:            # копия desktop/voice.py: clean — менять вместе
    t = re.sub(r"https?://\S+|magnet:\S+", " ссылка ", text or "")
    t = EMOJI.sub("", t)
    t = re.sub(r"[*_`#>|]+", "", t)
    t = t.replace("№", "номер ").replace("₽", " рублей").replace("°", " градусов").replace("%", " процентов")
    t = re.sub(r"(?m)^\s*[-•]\s*", "", t)
    t = re.sub(r"\s*\n+\s*", ". ", t)
    t = re.sub(r"\.\s*\.", ".", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    if len(t) > limit:
        cut = t[:limit]
        t = cut[:cut.rfind(".") + 1] if "." in cut[200:] else cut
        t += " Остальное — на экране."
    return t


def parts(text: str) -> list:                              # копия desktop/ui.html: speechParts — менять вместе
    t = re.sub(r"\s+", " ", re.sub(r"https?://\S+|magnet:\S+", " ", text or "")).strip()
    if len(t) > 1400:
        cut = t[:1400]
        t = cut[:max(cut.rfind(". "), 600) + 1] + " Остальное — на экране."
    sents = re.findall(r"[^.!?…\n]+[.!?…]*\s*", t) or [t]
    out, cur = [], ""
    for s in sents:
        if cur and (len(cur) + len(s) > 260 or (not out and len(cur) > 25)):
            out.append(cur.strip())
            cur = ""
        cur += s
    if cur.strip():
        out.append(cur.strip())
    return out
