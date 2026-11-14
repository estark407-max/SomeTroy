"""Сторож Троя: раз в 2 минуты проверяет, что Трой жив, и чинит, если нет.

Запускается таймером systemd (troy-watchdog.timer) — его ставит сам Трой при запуске.
- Трой упал и systemd поднял его заново — пишет в Telegram, что случилось (последняя ошибка из журнала).
- Трой завис (давно не проверял Telegram или процесс замер) — перезапускает и пишет об этом.
- Трой не поднимается — пишет, не чаще раза в час, и пробует снова.
Сам ничего не настраивает и не трогает: только перезапуск службы и сообщение.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import requests

DIR = Path(__file__).resolve().parent
SERVICE = "sometroy"
BEAT = DIR / "heartbeat.json"
STATE = DIR / "watchdog.json"
FROZEN = 3 * 60          # сам процесс не отмечался — он замер целиком
STUCK = 20 * 60          # Telegram не проверялся — даже самый долгий ответ столько не идёт
MAX_RESTARTS = 3         # перезапусков в час — дальше только сообщаем, чтобы не крутиться вхолостую


def settings() -> dict:
    out = {}
    try:
        for line in (DIR / "settings.env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def send(text: str) -> None:
    cfg = settings()
    token, chat = cfg.get("TELEGRAM_TOKEN"), cfg.get("ALLOWED_USER_ID")
    if not token or not chat:
        return
    proxy = cfg.get("TELEGRAM_PROXY")
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20,
                      data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"},
                      proxies={"http": proxy, "https": proxy} if proxy else None)
    except requests.RequestException as e:
        print(f"Не отправил сообщение в Telegram: {type(e).__name__}", file=sys.stderr)


def systemctl(*args) -> str:
    try:
        return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def prop(name: str) -> str:
    return systemctl("show", SERVICE, "-p", name, "--value")


def last_error() -> str:
    """Последняя ошибка Троя из журнала (токены туда не пишутся — их прячет сам Трой)."""
    try:
        out = subprocess.run(["journalctl", "-u", SERVICE, "-n", "80", "--no-pager", "-o", "cat"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ("Error" in ln or "ERROR" in ln or "Exception" in ln) and not ln.startswith(" "):
            return ln[-300:]
    return ""


def load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main() -> None:
    st = load()
    now = time.time()
    st["restarts"] = [t for t in st.get("restarts", []) if now - t < 3600]
    active = prop("ActiveState")
    n = prop("NRestarts")
    since = prop("ActiveEnterTimestampMonotonic")
    try:
        uptime = time.monotonic() - int(since) / 1e6 if since and since != "0" else 0
    except ValueError:
        uptime = 0

    # 1) systemd сам перезапустил упавшего Троя — рассказываем, почему упал
    if n.isdigit() and "nrestarts" in st and int(n) > int(st["nrestarts"]):
        err = last_error()
        send("⚠️ Трой упал и перезапустился сам." + (f"\nПоследняя ошибка: {err}" if err else ""))
    if n.isdigit():
        st["nrestarts"] = int(n)

    reason = ""
    if active != "active":
        if active not in ("activating", "deactivating", "reloading"):
            reason = "не запущен"
    elif uptime > 5 * 60:                        # только что запустился — даём время прийти в себя
        try:
            beat = json.loads(BEAT.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            beat = None
        if beat is None:
            reason = "не отмечается (нет отметки о работе)"
        elif now - beat.get("ts", 0) > FROZEN:
            reason = f"замер: не отмечался {int((now - beat.get('ts', 0)) // 60)} мин"
        elif beat.get("poll") and now - beat["poll"] > STUCK:     # Telegram не подключён — poll пустой
            reason = f"завис: {int((now - beat.get('poll', 0)) // 60)} мин не проверял сообщения в Telegram"

    if reason:
        if len(st["restarts"]) >= MAX_RESTARTS:
            if now - st.get("gave_up", 0) > 3600:
                st["gave_up"] = now
                send(f"❌ Трой {reason}, а перезапуски не помогают ({MAX_RESTARTS} за час). Нужен ремонт: "
                     "обнови Троя на сервере (update.bat) или посмотри журнал: journalctl -u sometroy -n 100. "
                     "Ошибку разработчикам — команда /bug.")
        else:
            err = last_error()
            systemctl("restart", SERVICE)
            st["restarts"].append(now)
            time.sleep(10)
            st["nrestarts"] = int(prop("NRestarts") or 0) if prop("NRestarts").isdigit() else st.get("nrestarts", 0)
            ok = prop("ActiveState") == "active"
            send(f"🔧 Трой {reason} — перезапустил его. " + ("Снова работает." if ok else "Пока не поднялся, проверю ещё.")
                 + (f"\nПоследняя ошибка в журнале: {err}" if err else ""))
    STATE.write_text(json.dumps(st), encoding="utf-8")


if __name__ == "__main__":
    main()
