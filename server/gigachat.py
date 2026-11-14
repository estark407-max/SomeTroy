"""GigaChat (Сбер) — модель для Троя. Для физлиц — бесплатный пакет токенов на год.

Отличия от моделей «как у OpenAI», которые Трой сглаживает сам:
- вход: «ключ авторизации» из личного кабинета меняется на пропуск (токен), который живёт 30 минут —
  Трой обновляет его сам за минуту до конца;
- функции: у GigaChat поле functions и ответ function_call (один вызов за раз), результат — сообщение
  с ролью function; Трой переводит туда и обратно, остальной код ничего не замечает;
- сертификат: серверы Сбера подписаны российским корневым сертификатом (Минцифры). Если его нет в системе,
  Трой один раз скачивает его с официального сайта gu-st.ru и доверяет ему только для адресов GigaChat.
"""
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("troy.gigachat")
OAUTH = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
APIS = ["https://api.giga.chat/v1/", "https://gigachat.devices.sberbank.ru/api/v1/"]
RU_CA_URL = "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt"
MODELS = ["GigaChat-2-Max", "GigaChat-2-Pro", "GigaChat-2"]     # у каждой свой бесплатный пакет: кончился — следующая


class GigaChat:
    NAME = "GigaChat"

    def __init__(self, key: str, error_cls, tls_dir: Path, scope: str = "GIGACHAT_API_PERS", proxy: str = ""):
        self.key = key.strip()
        self.scope = scope
        self.Error = error_cls
        self.tls_dir = tls_dir
        self.s = requests.Session()
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.verify = True
        self.token, self.until = "", 0.0
        self.api = APIS[0]
        self.lock = threading.Lock()

    # ── сертификат Минцифры ──
    def _bundle(self) -> str:
        """Обычные сертификаты + российский корневой — одним файлом (только для запросов к GigaChat)."""
        import certifi
        self.tls_dir.mkdir(parents=True, exist_ok=True)
        ru = self.tls_dir / "russian_trusted_root_ca.pem"
        if not ru.exists():
            r = requests.get(RU_CA_URL, timeout=30)          # официальный сайт; проверяется обычными сертификатами
            r.raise_for_status()
            if b"BEGIN CERTIFICATE" not in r.content:
                raise RuntimeError("с gu-st.ru пришёл не сертификат")
            ru.write_bytes(r.content)
            log.info("Скачал корневой сертификат Минцифры для GigaChat")
        bundle = self.tls_dir / "ca-with-russian-root.pem"
        bundle.write_bytes(Path(certifi.where()).read_bytes() + b"\n" + ru.read_bytes())
        return str(bundle)

    def _req(self, method: str, url: str, **kw) -> requests.Response:
        try:
            return self.s.request(method, url, verify=self.verify, **kw)
        except requests.exceptions.SSLError:
            if self.verify is not True:
                raise
            self.verify = self._bundle()
            return self.s.request(method, url, verify=self.verify, **kw)

    def _api(self, method: str, path: str, **kw) -> requests.Response:
        """Новый адрес API, а если он недоступен с сервера — прежний."""
        order = [self.api] + [a for a in APIS if a != self.api]
        for n, base in enumerate(order):
            last = n == len(order) - 1
            try:
                r = self._req(method, base + path, **kw)
            except requests.ConnectionError:
                if last:
                    raise
                continue
            if r.status_code == 404 and not last:
                continue
            self.api = base
            return r
        raise requests.ConnectionError("GigaChat недоступен")

    # ── пропуск на 30 минут ──
    def _auth(self) -> str:
        with self.lock:
            if self.token and time.time() < self.until - 60:
                return self.token
            last = None
            for scheme in ("Basic", "Bearer"):                # в документации Сбера встречаются оба варианта
                r = self._req("POST", OAUTH, timeout=(10, 30), data={"scope": self.scope}, headers={
                    "Authorization": f"{scheme} {self.key}", "RqUID": str(uuid.uuid4()),
                    "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
                if r.status_code == 200:
                    j = r.json()
                    self.token = j["access_token"]
                    exp = float(j.get("expires_at") or 0)
                    self.until = exp / 1000 if exp > 1e11 else (exp or time.time() + 1800)
                    return self.token
                last = r
                if r.status_code not in (400, 401):
                    break
            text = (last.text or "")[:200] if last is not None else ""
            if last is not None and last.status_code in (400, 401) and "scope" in text.lower():
                raise self.Error(401, "ключ не подходит к тарифу для физлиц (scope) — проверь, что проект «для физлиц»",
                                 self.NAME)
            raise self.Error(last.status_code if last is not None else 503,
                             "GigaChat не принял ключ авторизации" if last is not None and last.status_code in (400, 401)
                             else f"вход в GigaChat не удался: {text}", self.NAME)

    # ── перевод сообщений: формат OpenAI → GigaChat ──
    @staticmethod
    def _messages(messages: list) -> list:
        out: list = []
        calls: dict = {}                                    # id вызова → как его записать для GigaChat
        for m in messages:
            role, content = m.get("role"), m.get("content")
            if isinstance(content, list):                   # картинки GigaChat так не принимает — оставляем текст
                content = "\n".join(p.get("text", "") for p in content if p.get("type") == "text")
            if role == "assistant" and m.get("tool_calls"):
                for c in m["tool_calls"]:
                    try:
                        args = json.loads(c["function"].get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    item = {"role": "assistant", "content": "",
                            "function_call": {"name": c["function"]["name"], "arguments": args}}
                    if c.get("state_id"):
                        item["functions_state_id"] = c["state_id"]
                    calls[c["id"]] = item
                continue
            if role == "tool":                              # вызов и сразу его результат — по одному, как ждёт GigaChat
                call = calls.pop(m.get("tool_call_id"), None)
                if call is None:
                    continue
                out.append(call)
                out.append({"role": "function", "name": call["function_call"]["name"],
                            "content": json.dumps({"result": content or ""}, ensure_ascii=False)})
                continue
            if role == "system" and out:                    # системное — только первым сообщением
                role = "user"
            out.append({"role": role, "content": content or ""})
        return out

    @staticmethod
    def _schema(sch: dict, packed: set, top: bool) -> dict:
        """GigaChat требует у каждого объекта список полей (properties). Объект «с любыми полями»
        (например поля сделки Битрикса) передаём ему строкой JSON, а ответ превращаем обратно в объект."""
        out = dict(sch)
        props = {}
        for k, v in (sch.get("properties") or {}).items():
            if not isinstance(v, dict):
                continue
            if v.get("type") == "object" and not v.get("properties"):
                props[k] = {"type": "string", "description": (v.get("description") or k) +
                            " — JSON-объект строкой, например {\"TITLE\": \"…\"}"}
                if top:
                    packed.add(k)
            elif v.get("type") == "object":
                props[k] = GigaChat._schema(v, packed, False)
            elif v.get("type") == "array" and isinstance(v.get("items"), dict) and v["items"].get("type") == "object":
                props[k] = {**v, "items": GigaChat._schema(v["items"], packed, False) if v["items"].get("properties")
                            else {"type": "string"}}
            else:
                props[k] = v
        out["type"] = "object"
        out["properties"] = props
        return out

    def _functions(self, tools: list) -> list:
        funcs = []
        self.packed = {}
        for t in tools:
            f = dict(t["function"])
            packed: set = set()
            f["parameters"] = self._schema(f.get("parameters") or {"type": "object"}, packed, True)
            if packed:
                self.packed[f["name"]] = packed
            funcs.append(f)
        return funcs

    def chat(self, model: str, messages: list, tools: Optional[list] = None, **extra) -> dict:
        body = {"model": model, "messages": self._messages(messages), "max_tokens": 2048, "temperature": 0.4}
        if tools:
            body["functions"] = self._functions(tools)
            body["function_call"] = "auto"
        for attempt in range(2):
            token = self._auth()
            r = self._api("POST", "chat/completions", json=body, timeout=(10, 90),
                          headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            if r.status_code == 401 and attempt == 0:      # пропуск истёк раньше срока — берём новый
                self.token = ""
                continue
            if r.status_code in (500, 502, 503) and attempt == 0:
                time.sleep(3)
                continue
            break
        if r.status_code != 200:
            try:
                msg = r.json().get("message") or r.text[:300]
            except ValueError:
                msg = r.text[:300]
            raise self.Error(r.status_code, str(msg), self.NAME)
        msg = r.json()["choices"][0]["message"]
        out = {"role": "assistant", "content": msg.get("content") or ""}
        fc = msg.get("function_call")
        if fc and fc.get("name"):
            args = fc.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            for k in getattr(self, "packed", {}).get(fc["name"], ()):     # строка JSON → снова объект
                if isinstance(args, dict) and isinstance(args.get(k), str):
                    try:
                        args[k] = json.loads(args[k])
                    except ValueError:
                        pass
            out["tool_calls"] = [{"id": f"gc_{uuid.uuid4().hex[:10]}", "type": "function",
                                  "state_id": msg.get("functions_state_id") or "",
                                  "function": {"name": fc["name"],
                                               "arguments": args if isinstance(args, str)
                                               else json.dumps(args or {}, ensure_ascii=False)}}]
        return out

    def models(self) -> list:
        token = self._auth()
        r = self._api("GET", "models", timeout=(10, 30), headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            raise self.Error(r.status_code, r.text[:200], self.NAME)
        return [m["id"] for m in r.json().get("data", [])]
