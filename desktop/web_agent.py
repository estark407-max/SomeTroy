"""Трой делает дела на сайтах: открывает вкладку в своём окне и шаг за шагом нажимает и заполняет, как человек.

Как устроено:
- программа читает страницу (текст и пронумерованные кнопки, ссылки, поля) и отправляет на сервер;
- модель на сервере отвечает одним действием: нажать [5], ввести текст в [7], открыть адрес, прокрутить…;
- программа выполняет его во вкладке, и так до результата (не больше 30 шагов).
Ты видишь всё во вкладке и можешь вмешаться.

Защита:
- прежде чем отправить заявку, записаться, заказать, оплатить или написать кому-то, Трой останавливается
  и спрашивает «да?» в чате; продолжит после твоего ответа;
- пароли, данные карт и коды из СМС Трой не вводит никогда — их вводишь ты сам во вкладке.
"""
import json
import logging
import re
import threading
import time

log = logging.getLogger("troy.agent")
MAX_STEPS = 30
STATE: dict = {"run": None}                      # текущее дело: goal, tid, history, waiting
# Жёсткая защита (не зависит от модели и от того, что написано на сайте): кнопки, после которых уходят деньги,
# заявка или сообщение, Трой нажимает только после твоего «да» в чате.
RISKY = (r"оплат|плат[её]ж|купить|куплю|заказать|оформить|оформл|подтверд|отправить|записаться|забронир|"
         r"перевести|перевод|удалить|отменить заказ|подписаться|согласен|принять|"
         r"\bpay\b|checkout|\bbuy\b|purchase|place order|confirm|submit|\bsend\b|\bbook\b|delete|subscribe")
RISKY_RE = re.compile(RISKY, re.I)
YES_RE = re.compile(r"^\s*(да|ага|ок|окей|давай|подтверждаю|продолжай|жми|yes|ok)\b", re.I)
LABEL_JS = """(() => { const e = document.querySelector('[data-troy="%d"]'); if (!e) return '';
  return (e.innerText || e.value || e.getAttribute('aria-label') || e.title || '').slice(0, 200); })()"""

SNAPSHOT_JS = r"""
(() => {
  document.querySelectorAll('[data-troy]').forEach(e => e.removeAttribute('data-troy'));
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=checkbox],[role=radio],' +
              '[role=tab],[role=menuitem],[role=option],[role=combobox],[onclick],[contenteditable=true],[tabindex]:not([tabindex="-1"])';
  const out = []; let n = 0;
  for (const e of document.querySelectorAll(sel)) {
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = getComputedStyle(e);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    if (e.tagName === 'INPUT' && e.type === 'hidden') continue;
    if (r.bottom < -300 || r.top > innerHeight * 3) continue;
    const id = ++n; e.setAttribute('data-troy', id);
    let kind = e.tagName.toLowerCase();
    if (kind === 'input') kind += ':' + (e.type || 'text');
    const lab = e.labels && e.labels[0] ? e.labels[0].innerText : '';
    let t = (e.innerText || e.value || e.getAttribute('aria-label') || lab || e.placeholder || e.title || e.alt || e.name || '')
      .trim().replace(/\s+/g, ' ').slice(0, 90);
    if (e.tagName === 'INPUT' && lab && e.value) t = lab.trim().slice(0, 40) + ' = ' + e.value.slice(0, 40);
    let extra = '';
    if (e.tagName === 'SELECT') extra = ' варианты: ' + [...e.options].slice(0, 20).map(o => (o.selected ? '•' : '') + o.text.trim()).join(' | ');
    const secret = e.type === 'password' || /cc-|card|cvc|cvv|sms|otp|one-time/i.test((e.autocomplete || '') + ' ' + (e.name || '') + ' ' + (e.id || ''));
    const view = r.top >= 0 && r.bottom <= innerHeight ? '' : ' (за экраном)';
    out.push(`[${id}] ${kind}${e.disabled ? ' недоступно' : ''}${e.checked ? ' ✓' : ''}${secret ? ' СЕКРЕТНОЕ' : ''}: ${t}${extra}${view}`);
    if (n >= 160) break;
  }
  const text = (document.body ? document.body.innerText : '').replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n').slice(0, 4000);
  const h = document.documentElement.scrollHeight || 1;
  return JSON.stringify({ url: location.href, title: document.title, text, els: out.join('\n'),
                          scroll: Math.round((scrollY + innerHeight) / h * 100) });
})()
"""

ACT_JS = r"""
((a) => { try {
  const e = a.id ? document.querySelector('[data-troy="' + a.id + '"]') : null;
  if (a.id && !e) return 'нет элемента [' + a.id + '] — страница изменилась';
  const secret = e && (e.type === 'password' || /cc-|card|cvc|cvv|sms|otp|one-time/i.test((e.autocomplete || '') + ' ' + (e.name || '') + ' ' + (e.id || '')));
  if (a.do === 'click') {
    if (secret) return 'ЗАПРЕЩЕНО: это поле для пароля, карты или кода — его трогает только пользователь';
    e.scrollIntoView({ block: 'center' });
    for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup']) e.dispatchEvent(new MouseEvent(t, { bubbles: true }));
    e.click(); return 'нажал';
  }
  if (a.do === 'type') {
    if (secret) return 'ЗАПРЕЩЕНО: это поле для пароля, карты или кода — его вводит сам пользователь';
    if (!e.isContentEditable && !['INPUT', 'TEXTAREA'].includes(e.tagName)) return 'это не поле ввода — выбери поле';
    e.scrollIntoView({ block: 'center' }); e.focus();
    if (e.isContentEditable) { e.textContent = a.text; }
    else {
      const proto = e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(e, a.text);
    }
    e.dispatchEvent(new Event('input', { bubbles: true })); e.dispatchEvent(new Event('change', { bubbles: true }));
    if (a.enter) {
      const fields = e.form ? e.form.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]),textarea,select').length : 0;
      if (fields > 1 && !a.ok) return 'ввёл, но форму НЕ отправил: отправку формы с данными подтверждает пользователь — вызови ask_user';
      for (const t of ['keydown', 'keypress', 'keyup']) e.dispatchEvent(new KeyboardEvent(t, { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
      if (e.form) { e.form.requestSubmit ? e.form.requestSubmit() : e.form.submit(); }
    }
    return 'ввёл';
  }
  if (a.do === 'select') {
    if (e.tagName !== 'SELECT') return 'это не выпадающий список';
    const want = (a.text || '').toLowerCase();
    const o = [...e.options].find(o => o.text.trim().toLowerCase() === want) || [...e.options].find(o => o.text.toLowerCase().includes(want));
    if (!o) return 'нет такого варианта';
    e.value = o.value; e.dispatchEvent(new Event('change', { bubbles: true })); return 'выбрал ' + o.text.trim();
  }
  if (a.do === 'scroll') { window.scrollBy(0, (a.text === 'up' ? -0.8 : 0.8) * innerHeight); return 'прокрутил'; }
  if (a.do === 'back') { history.back(); return 'назад'; }
  return 'не знаю действие';
} catch (err) { return 'ошибка: ' + err.message; } })(%s)
"""


def _snapshot(sh, tid) -> dict:
    raw = sh.tab_eval(tid, SNAPSHOT_JS)
    return json.loads(json.loads(raw))


def _settle(sh, tid, wait: float = 1.2) -> None:
    time.sleep(wait)
    end = time.time() + 12
    while time.time() < end:
        try:
            if json.loads(sh.tab_eval(tid, "document.readyState", 5)) == "complete":
                break
        except Exception:
            pass
        time.sleep(0.4)
    time.sleep(0.6)


def start(remote, sh, open_tab, arg: str, report) -> tuple:
    """Команда с сервера: {"goal", "url"} — новое дело; {"answer"} — ответ на вопрос «да?» для текущего дела."""
    info = json.loads(arg or "{}")
    cur = STATE["run"]
    if info.get("stop"):
        if cur:
            cur["active"] = cur["waiting"] = False
            STATE["run"] = None
            return True, "остановил дело на сайте"
        return True, "сейчас никаких дел на сайтах не делаю"
    if info.get("answer") is not None:
        if not cur or not cur.get("waiting"):
            return False, "сейчас нет дела, которое ждёт ответа"
        cur["history"].append(f"Пользователь ответил: {info['answer']}")
        cur["waiting"] = False
        cur["ok_once"] = bool(YES_RE.match(str(info["answer"])))      # «да» разрешает одно важное нажатие
    else:
        if cur and cur.get("active"):
            return False, "уже делаю другое дело на сайте — дождись или скажи «стоп»"
        url = info.get("url") or "https://ya.ru/"
        ok, text = open_tab(url)
        if not ok:
            return False, text
        tid = sh.last_tab()
        if tid is None or not sh.tab_ready(tid):
            return False, "вкладка не открылась"
        cur = STATE["run"] = {"goal": info.get("goal", ""), "tid": tid, "history": [], "waiting": False}
    cur["active"] = True
    threading.Thread(target=_loop, args=(remote, sh, cur, report), daemon=True).start()
    return True, "делаю во вкладке Троя — смотри, как идёт; итог пришлю"


def _loop(remote, sh, cur: dict, report) -> None:
    t0 = time.time()
    try:
        for step in range(MAX_STEPS):
            if not cur.get("active") or time.time() - t0 > 8 * 60:
                report(False, "остановил дело на сайте: слишком долго" if cur.get("active") else "остановлено")
                return
            _settle(sh, cur["tid"], 0.3 if step == 0 else 1.0)
            page = _snapshot(sh, cur["tid"])
            code, body = remote.forward("POST", "/api/agent/step", json.dumps(
                {"goal": cur["goal"], "page": page, "history": cur["history"][-14:], "step": step},
                ensure_ascii=False).encode("utf-8"))
            if code != 200:
                report(False, f"сервер не дал следующего шага (код {code})")
                return
            a = json.loads(body)
            do = a.get("do")
            log.info("Дело на сайте, шаг %d: %s", step + 1, do)
            if do == "done":
                report(True, a.get("text") or "готово")
                break
            if do == "ask":                          # перед важным действием — спросить пользователя
                cur["waiting"], cur["active"] = True, False
                cur["history"].append(f"Спросил пользователя: {a.get('text')}")
                report(True, "❓ " + (a.get("text") or "Продолжить?") + "\nОтветь «да» — продолжу, или скажи, что поменять.")
                return
            if do == "open":
                sh.tab_navigate(cur["tid"], a.get("url") or "")
                res = "открыл " + (a.get("url") or "")
            elif do == "wait":
                time.sleep(3)
                res = "подождал"
            else:
                if do == "click":                    # важная кнопка — только после «да» пользователя
                    try:
                        label = json.loads(sh.tab_eval(cur["tid"], LABEL_JS % int(a.get("id") or 0), 5)) or ""
                    except Exception:
                        label = ""
                    if RISKY_RE.search(label) and not cur.pop("ok_once", False):
                        cur["waiting"], cur["active"] = True, False
                        cur["history"].append(f"Хотел нажать «{label.strip()[:80]}» — спросил пользователя")
                        report(True, f"❓ Нажать «{label.strip()[:80]}»?\nОтветь «да» — нажму, или скажи, что поменять.")
                        return
                elif do == "type" and a.get("enter"):
                    a["ok"] = bool(cur.pop("ok_once", False))
                try:
                    res = json.loads(sh.tab_eval(cur["tid"], ACT_JS % json.dumps(a, ensure_ascii=False)))
                except Exception as e:
                    res = f"ошибка: {e}"
            cur["history"].append(f"Шаг {sum(x.startswith('Шаг') for x in cur['history']) + 1}: {a.get('say') or do} → {res}")
        else:
            report(False, "не уложился в 30 шагов — остановился; вкладка открыта, можно продолжить вручную")
    except Exception as e:
        log.exception("Дело на сайте")
        report(False, f"сбой: {e}")
    finally:
        if not cur.get("waiting"):
            cur["active"] = False
            if STATE["run"] is cur:
                STATE["run"] = None
