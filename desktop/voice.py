"""Голос Троя: озвучка ответов.

Два движка:
  нейросетевой — голоса Microsoft (те же, что «Читать вслух» в Edge): живые интонации, нужен интернет,
                 бесплатно. «Сверхразум» — бархатный низкий голос с «расщеплением»: под основным голосом
                 тихо звучит тот же голос на полторы октавы ниже, плюс лёгкое удвоение. Слои сводит окно Троя
                 (Web Audio) — сам голос при этом не портится;
  Piper        — запасной, без интернета: стили ниже.

Piper и русские голоса скачиваются один раз при первом включении голоса (~85 МБ) в папку данных Троя.
Дальше всё работает без интернета. Обработка звука — на чистом Python, без лишних библиотек.

Стили:
  station — «ИИ станции»: холодный металл, многоголосие, эхо и цифровые сбои с «заиканием»;
  ship    — «Бортовой компьютер»: спокойный, чуть медленнее, приглушённый ламповый тон с лёгким эхом отсека;
  plain   — обычный голос без обработки.
"""
import array
import hashlib
import io
import logging
import math
import os
import random
import re
import subprocess
import threading
import time
import wave
import zipfile
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("troy.voice")
PIPER_ZIP = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip"
VOICE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/{name}/medium/ru_RU-{name}-medium.onnx"
VOICES = {"irina": "Ирина (женский)", "denis": "Денис (мужской)", "dmitri": "Дмитрий (мужской)",
          "ruslan": "Руслан (мужской)"}
STYLES = {"mind": "Сверхразум", "station": "ИИ станции", "ship": "Бортовой компьютер", "plain": "Обычный"}
NEURAL = {"dmitry": ("ru-RU-DmitryNeural", "Дмитрий"), "svetlana": ("ru-RU-SvetlanaNeural", "Светлана"),
          "dariya": ("ru-RU-DariyaNeural", "Дария")}
# «Сверхразум» на нейросетевом голосе: размеренно, чуть ниже; слой «расщепления» — тот же текст заметно ниже
NEURAL_STYLE = {
    # голос просим чуть быстрее (+6%), а окно Троя играет его на 10% медленнее — так он становится ниже
    # и плотнее (почти на 2 полутона), а темп выходит размеренным
    # Низкий слой «расщепления» делает окно Троя само (synth_layer: на 30% ниже) — у Microsoft голос
    # просим один раз и без изменения высоты: так быстрее и подходит любому голосу, в том числе Дмитрию.
    # Дальше — металл (резонансы, вибрация, флэнджер) и отражения металлического зала (hall).
    "mind": {"rate": "-7%", "pitch": "-8Hz", "layer_pitch": "",
             "fx": {"layer": 0.55, "layer_lp": 1600, "synth_layer": 0.7, "chorus": 0.2, "delay": 0.012, "warm": 1,
                    "bass": 7, "bass_f": 200, "treble": 2, "lower": 0.9, "metal": 1, "hall": 1}},
    "plain": {"rate": "+0%", "pitch": "+0Hz", "layer_pitch": "", "fx": {}},
}
VOICE_ID_RE = re.compile(r"[a-z]{2}-[A-Z]{2}-[A-Za-z]+Neural|silero:[a-z]+")   # голос Microsoft или свой (Silero)
# Свой голос Троя — Евгений (Silero, на сервере Троя): ниже (0.88), размеренно и со «звоном» как у Дмитрия.
# Темп: сервер замедляет речь на 16%, понижение тона добавляет ещё ~12% — итог как у выбранного образца
VOICE_STYLE = {
    "silero:eugene": {"rate": "-12%", "pitch": "+0Hz", "layer_pitch": "",
                      "fx": {"layer": 0.5, "layer_lp": 1600, "synth_layer": 0.7, "chorus": 0.2, "delay": 0.012,
                             "warm": 1, "bass": 6, "bass_f": 200, "treble": 7.5, "treble_f": 4800, "presence": 6,
                             "lower": 0.88, "metal": 1, "hall": 1}},
}


def style_for(voice_id: str, style: str) -> dict:
    """Настройки голоса: у своего голоса — свои, у голосов Microsoft — общие для стиля."""
    if style == "mind" and voice_id in VOICE_STYLE:
        return VOICE_STYLE[voice_id]
    return NEURAL_STYLE.get(style) or NEURAL_STYLE["mind"]
NEURAL_STATE = {"local_bad": False}         # напрямую не вышло — до перезапуска программы ходим через сервер


def _edge_local(text: str, voice: str, rate: str, pitch: str) -> bytes:
    """Озвучка голосом Microsoft прямо с этого компьютера (MP3)."""
    import asyncio
    import edge_tts

    async def run():
        out = bytearray()
        async for chunk in edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).stream():
            if chunk.get("type") == "audio":
                out += chunk["data"]
        return bytes(out)
    return asyncio.run(asyncio.wait_for(run(), 60))


FILE_RE = re.compile(r"^[0-9a-f]{20}(?:\.layer)?\.(?:mp3|wav)$")
STATE = {"busy": False, "step": "", "error": "", "pct": 0}
_lock = threading.Lock()


class Voice:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "voice"
        self.cache = self.dir / "cache"
        self.local = None                                 # LocalSilero — свой голос на этом компьютере

    # ── установка ──
    def exe(self) -> Path:
        return self.dir / "piper" / "piper.exe"

    def model(self, name: str) -> Path:
        return self.dir / f"ru_RU-{name}-medium.onnx"

    def ready(self, name: str) -> bool:
        m = self.model(name)
        return self.exe().exists() and m.exists() and Path(str(m) + ".json").exists()

    def status(self, name: str) -> dict:
        return {"ready": self.ready(name), "busy": STATE["busy"], "step": STATE["step"], "pct": STATE["pct"],
                "error": STATE["error"], "voices": VOICES, "styles": STYLES}

    def _download(self, url: str, target: Path, label: str) -> None:
        STATE.update(step=label, pct=0)
        tmp = target.with_suffix(target.suffix + ".part")
        with requests.get(url, stream=True, timeout=(15, 60)) as r:
            r.raise_for_status()
            total, got = int(r.headers.get("Content-Length") or 0), 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        STATE["pct"] = int(got * 100 / total)
        tmp.replace(target)

    def install(self, name: str) -> None:
        """Скачать Piper и голос (в фоне). Повторный вызов во время скачивания ничего не делает."""
        with _lock:
            if STATE["busy"]:
                return
            STATE.update(busy=True, error="")

        def run():
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                if not self.exe().exists():
                    z = self.dir / "piper.zip"
                    self._download(PIPER_ZIP, z, "программа синтеза речи")
                    with zipfile.ZipFile(z) as zf:
                        zf.extractall(self.dir)
                    z.unlink()
                m = self.model(name)
                if not m.exists():
                    self._download(VOICE_URL.format(name=name), m, f"голос «{VOICES.get(name, name)}»")
                if not Path(str(m) + ".json").exists():
                    self._download(VOICE_URL.format(name=name) + ".json", Path(str(m) + ".json"), "настройки голоса")
                log.info("Голос готов: %s", name)
            except Exception as e:
                log.warning("Голос не скачался: %s", e)
                STATE["error"] = f"не получилось скачать: {e}"
            finally:
                STATE.update(busy=False, step="", pct=0)
        threading.Thread(target=run, daemon=True).start()

    # ── синтез ──
    def speak(self, text: str, name: str = "irina", style: str = "station") -> bytes:
        text = clean(text)
        if not text:
            raise ValueError("нечего озвучивать")
        key = self._piper_key(name, style, text)          # fx3 — после правки эффектов и темпа
        self.cache.mkdir(parents=True, exist_ok=True)
        hit = self.cache / f"{key}.wav"
        if hit.exists():
            return hit.read_bytes()
        speed = {"ship": "1.12", "station": "1.05", "mind": f"{MIND_TEMPO * MIND_PITCH:.3f}"}.get(style, "1.0")
        # «Сверхразум» говорит размеренно, будто слегка устало: медленнее, ровнее, с паузами между фразами
        extra = ["--noise_scale", "0.5", "--noise_w", "0.6"] if style == "mind" else []
        pause = "0.35" if style == "mind" else "0.25"
        out = self.cache / f"{key}.raw.wav"
        r = subprocess.run([str(self.exe()), "--model", str(self.model(name)), "--output_file", str(out),
                            "--length_scale", speed, "--sentence_silence", pause] + extra,
                           input=text.encode("utf-8"), capture_output=True, timeout=120,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), cwd=str(self.exe().parent))
        if r.returncode != 0 or not out.exists():
            raise RuntimeError("синтез не удался: " + r.stderr.decode("utf-8", "replace")[-200:])
        rate, samples = read_wav(out.read_bytes())
        out.unlink()
        if style == "mind":
            samples = fx_mind(samples, rate)
        elif style == "station":
            samples = fx_station(samples, rate)
        elif style == "ship":
            samples = fx_ship(samples, rate)
        data = write_wav(rate, samples)
        hit.write_bytes(data)
        self._trim_cache()
        return data

    # ── нейросетевой голос (Microsoft, через интернет) ──
    def neural(self, text: str, name: str = "dmitry", style: str = "mind", via_server=None) -> dict:
        """Озвучить нейросетевым голосом. Возвращает имена файлов в кэше и настройки сведения слоёв."""
        text = clean(text)
        if not text:
            raise ValueError("нечего озвучивать")
        voice = name if VOICE_ID_RE.fullmatch(name or "") else NEURAL.get(name, NEURAL["dmitry"])[0]
        if voice.startswith("silero:"):                  # Silero не читает цифры — время, даты и числа словами
            text = say_numbers(text)
        st = style_for(voice, style)
        key = hashlib.sha1(f"nv4|{voice}|{style}|{text}".encode("utf-8")).hexdigest()[:20]
        self.cache.mkdir(parents=True, exist_ok=True)
        main, layer = self.cache / f"{key}.mp3", self.cache / f"{key}.layer.mp3"
        jobs = [] if main.exists() else [(main, st["pitch"])]
        if st["layer_pitch"] and not layer.exists():
            jobs.append((layer, st["layer_pitch"]))
        errors: list = []

        def one(path, pitch):
            audio = b""
            if voice.startswith("silero:") and self.local is not None and self.local.up():   # Евгений на компьютере
                try:
                    audio = self.local.synth(text, voice.split(":", 1)[1], 1 + int(st["rate"].rstrip("%")) / 100)
                except Exception as e:
                    log.info("Евгений на компьютере не ответил: %s", e)
            if not voice.startswith("silero:") and not self._local_bad():   # свой голос — только с сервера
                try:
                    audio = _edge_local(text, voice, st["rate"], pitch)
                except Exception as e:                 # из России Microsoft часто не отдаёт звук — идём через сервер
                    log.info("Голос напрямую не вышел (%s: %s) — дальше через сервер Троя", type(e).__name__, e)
                    self._local_bad(True)
            if not audio and via_server:
                audio = via_server(text, voice, st["rate"], pitch)
            if audio:
                tmp = path.with_name(f"{path.name}.{threading.get_ident()}.part")
                tmp.write_bytes(audio)
                tmp.replace(path)
            else:
                errors.append(path.name)
        threads = [threading.Thread(target=one, args=j, daemon=True) for j in jobs if not j[0].exists()]
        for th in threads:                               # основной голос и слой «расщепления» — одновременно
            th.start()
        for th in threads:
            th.join(90)
        if not main.exists():
            raise RuntimeError("нейросетевой голос сейчас недоступен ни напрямую, ни через сервер")
        if errors:
            log.info("Озвучка: слой не получился (%s) — играю без него", ", ".join(errors))
        self._trim_cache()
        files = [main.name] + ([layer.name] if st["layer_pitch"] and layer.exists() else [])
        return {"files": files, "fx": st["fx"]}

    def _local_bad(self, mark: bool = False) -> bool:
        """Напрямую голос не работает — запоминаем на сутки (и после перезапуска не тратим время на пробу)."""
        f = self.dir / "direct-voice-off"
        if mark:
            NEURAL_STATE["local_bad"] = True
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                f.write_text(str(int(time.time())))
            except OSError:
                pass
            return True
        if NEURAL_STATE["local_bad"]:
            return True
        try:
            if time.time() - int(f.read_text() or 0) < 86400:
                NEURAL_STATE["local_bad"] = True
                return True
        except (OSError, ValueError):
            pass
        return False

    def piper_file(self, text: str, name: str, style: str) -> dict:
        self.speak(text, name, style)                    # кладёт готовый WAV в кэш под этим же ключом
        return {"files": [self._piper_key(name, style, clean(text)) + ".wav"], "fx": {}}

    @staticmethod
    def _piper_key(name: str, style: str, text: str) -> str:
        return hashlib.sha1(f"fx3|{name}|{style}|{text}".encode("utf-8")).hexdigest()[:20]

    def file(self, name: str) -> Optional[Path]:
        if not FILE_RE.match(name or ""):
            return None
        f = self.cache / name
        return f if f.exists() else None

    def _trim_cache(self, keep: int = 60) -> None:
        files = sorted(list(self.cache.glob("*.wav")) + list(self.cache.glob("*.mp3")), key=lambda p: p.stat().st_mtime)
        for f in files[:-keep * 2]:
            try:
                f.unlink()
            except OSError:
                pass


# ── текст для чтения вслух ──
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿️‍]")


# ── сокращения и единицы — словами, в нужном падеже («5 м/с» → «5 метров в секунду») ──
UNITS = [                                    # длинные — раньше коротких: «мм рт. ст.» раньше «мм», «м/с» раньше «м»
    (r"мм\s*рт\.?\s*ст\.?", ("миллиметр ртутного столба", "миллиметра ртутного столба", "миллиметров ртутного столба"), True),
    (r"м/с(?:ек)?", ("метр в секунду", "метра в секунду", "метров в секунду"), True),
    (r"км/ч(?:ас)?", ("километр в час", "километра в час", "километров в час"), True),
    (r"гПа", ("гектопаскаль", "гектопаскаля", "гектопаскалей"), True),
    (r"°\s*[CС]?", ("градус", "градуса", "градусов"), False),
    (r"%", ("процент", "процента", "процентов"), False),
    (r"₽|руб", ("рубль", "рубля", "рублей"), True),
    (r"млрд", ("миллиард", "миллиарда", "миллиардов"), False),
    (r"млн", ("миллион", "миллиона", "миллионов"), False),
    (r"тыс", ("тысяча", "тысячи", "тысяч"), False),
    (r"км", ("километр", "километра", "километров"), False),
    (r"мм", ("миллиметр", "миллиметра", "миллиметров"), False),
    (r"см", ("сантиметр", "сантиметра", "сантиметров"), False),
    (r"кг", ("килограмм", "килограмма", "килограммов"), False),
    (r"мин", ("минута", "минуты", "минут"), False),
    (r"ч", ("час", "часа", "часов"), False),
    (r"м", ("метр", "метра", "метров"), False),
]
ABBR = [(r"\bт\.\s*е\.", "то есть"), (r"\bт\.\s*д\.", "так далее"), (r"\bт\.\s*п\.", "тому подобное"),
        (r"\bт\.\s*к\.", "так как"), (r"\bт\.\s*н\.", "так называемый"), (r"\bи\s+пр\.", "и прочее")]
_NUM = r"(\d+(?:[.,]\d+)?)"
_END = r"(?:\.(?!\s*[А-ЯЁA-Z]|\s*$))?(?![а-яёa-z/])"      # точку сокращения съедаем, точку конца фразы — нет


def _form(num: str, forms: tuple) -> str:
    if re.search(r"[.,]\d", num):                # дробное: «3,5 метра», «0,5 процента»
        return forms[1]
    n = int(num)
    d, dd = n % 10, n % 100
    return forms[0] if d == 1 and dd != 11 else forms[1] if 2 <= d <= 4 and not 12 <= dd <= 14 else forms[2]


def say_units(text: str) -> str:
    """«-3°C, ветер 5 м/с, давление 745 мм рт. ст.» → «минус 3 градуса, ветер 5 метров в секунду, …»."""
    t = text
    for rx, word in ABBR:
        t = re.sub(rx, word, t)
    t = re.sub(r"(\d)\s*[–—]\s*(\d)", r"\1 — \2", t)                     # 3–5 → «три — пять», а не «три минус пять»
    t = re.sub(r"(^|[\s(:;,])[-−](\d)", r"\1минус \2", t)                # -5 → минус 5 (иначе голос пропускает)
    t = re.sub(r"(^|[\s(:;,])\+(\d)", r"\1плюс \2", t)
    for rx, forms, alone in UNITS:
        t = re.sub(_NUM + r"\s*(?:" + rx + r")" + _END, lambda m, f=forms: f"{m.group(1)} {_form(m.group(1), f)}", t)
        if alone:                                                       # без числа: «ветер, м/с» → «метров в секунду»
            t = re.sub(r"(?<![а-яёa-z\d])(?:" + rx + r")" + _END, forms[2], t)
    return t


# ── ссылки на источники, которые модели иногда вставляют в текст: [sources=[1]], 【3†source】, [1], [2, 3] ──
def strip_cites(text: str) -> str:
    t = CITE_RE.sub("", text or "")
    return re.sub(r"[ \t]*,[ \t]*(?=[.!?;:]|$)", "", t, flags=re.M)


CITE_RE = re.compile(r"\s*\[(?:sources?|source_ids?|источники?|ref)\s*[=:]?\s*\[?[\d,\s]*\]?\]|\s*【[^】]{0,60}】|"
                     r"\s*\[\d{1,2}(?:\s*[,–-]\s*\d{1,2})*\](?!\()", re.I)


# строки «Источник: …» и ссылки голос не читает: источник виден на экране
SOURCE_LINE = re.compile(r"(?im)^[ \t]*(?:[-•*][ \t]*)?(?:источник(?:и)?|sources?|ссылк[аи]|подробнее)[ \t]*[:—-].*$")


def no_sources(text: str) -> str:
    t = strip_cites(text)
    t = SOURCE_LINE.sub("", t)
    t = re.sub(r"\((?:источник|по данным|данные)[^)]*\)", "", t, flags=re.I)
    return re.sub(r"\[([^\]]+)\]\((?:https?://|www\.)[^)]*\)", r"\1", t)        # [текст](ссылка) → текст


def clean(text: str, limit: int = 900) -> str:
    t = re.sub(r"https?://\S+|magnet:\S+|\bwww\.\S+", " ", no_sources(text))   # ссылки и источники не читаем
    t = EMOJI.sub("", t)
    t = re.sub(r"[*_`#>|]+", "", t)
    t = say_units(t.replace("№", "номер "))
    t = re.sub(r"(?m)^\s*[-•]\s*", "", t)
    t = re.sub(r"\s*\n+\s*", ". ", t)
    t = re.sub(r"\.\s*\.", ".", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    if len(t) > limit:                                   # длинное — читаем начало, до конца предложения
        cut = t[:limit]
        t = cut[:cut.rfind(".") + 1] if "." in cut[200:] else cut
        t += " Остальное — на экране."
    return t


# ── звук: чтение и запись WAV (16 бит, моно) ──
def read_wav(data: bytes) -> tuple:
    with wave.open(io.BytesIO(data)) as w:
        rate, ch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    a = array.array("h", raw)
    if ch > 1:
        a = a[::ch]
    return rate, [x / 32768.0 for x in a]


def write_wav(rate: int, samples: list) -> bytes:
    peak = max(1e-6, max((abs(x) for x in samples), default=0))
    gain = min(1.0, 0.92 / peak) if peak > 0.92 else 1.0
    a = array.array("h", (int(max(-1.0, min(1.0, x * gain)) * 32767) for x in samples))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(a.tobytes())
    return buf.getvalue()


# ── эффекты ──
def _comb(x: list, delay: int, fb: float, mix: float) -> list:
    y = [0.0] * len(x)
    for i, v in enumerate(x):
        y[i] = v + fb * (y[i - delay] if i >= delay else 0.0)
    return [a * (1 - mix) + b * mix for a, b in zip(x, y)]


def _echo(x: list, delay: int, gain: float, taps: int = 2) -> list:
    y = list(x) + [0.0] * delay * taps
    for t in range(1, taps + 1):
        g, d = gain ** t, delay * t
        for i, v in enumerate(x):
            y[i + d] += v * g
    return y


def _lowpass(x: list, rate: int, freq: float) -> list:
    a = 1 - math.exp(-2 * math.pi * freq / rate)
    y, s = [0.0] * len(x), 0.0
    for i, v in enumerate(x):
        s += a * (v - s)
        y[i] = s
    return y


def _highpass(x: list, rate: int, freq: float) -> list:
    low = _lowpass(x, rate, freq)
    return [a - b for a, b in zip(x, low)]


def _resample(x: list, ratio: float) -> list:
    """Сдвиг высоты простым пересэмплированием (ratio > 1 — выше и короче)."""
    n = int(len(x) / ratio)
    out = [0.0] * n
    for i in range(n):
        p = i * ratio
        j = int(p)
        f = p - j
        out[i] = x[j] * (1 - f) + (x[j + 1] if j + 1 < len(x) else 0.0) * f
    return out


def _words(x: list, rate: int) -> list:
    """Начала слов (после пауз) — места, где уместен цифровой сбой."""
    win = rate // 100
    starts, quiet = [], 0
    for i in range(0, len(x) - win, win):
        e = max(abs(v) for v in x[i:i + win])
        if e < 0.02:
            quiet += 1
        else:
            if quiet >= 4:
                starts.append(i)
            quiet = 0
    return starts


def fx_station(x: list, rate: int) -> list:
    """«ИИ станции»: многоголосие (тон ниже и выше), кольцевая модуляция, металлический резонанс, эхо, сбои."""
    rnd = random.Random(len(x))                         # одинаковый текст — одинаковые сбои
    # цифровые сбои: в начале некоторых слов кусочек звучит 2–3 раза подряд или «подпрыгивает» по тону
    out, last = [], 0
    for s in _words(x, rate):
        if rnd.random() < 0.3 and s - last > rate // 2:
            out += x[last:s]
            chunk = x[s:s + int(rate * rnd.uniform(0.05, 0.09))]
            if rnd.random() < 0.6:
                out += chunk * rnd.randint(2, 3)
            else:
                out += _resample(chunk, 1.35) + chunk
            last = s + len(chunk)
    x = out + x[last:]
    n = len(x)
    lo, hi = _resample(x, 0.94), _resample(x, 1.06)     # чуть «расслоённый» голос
    lo, hi = (lo + [0.0] * n)[:n], (hi + [0.0] * n)[:n]
    mixed = [a + 0.28 * b + 0.22 * c for a, b, c in zip(x, lo, hi)]
    w = 2 * math.pi * 55 / rate                         # кольцевая модуляция — «электрический» оттенок
    mixed = [v * (0.62 + 0.38 * math.sin(w * i)) for i, v in enumerate(mixed)]
    mixed = _comb(mixed, int(rate * 0.0042), 0.4, 0.35)    # металлический резонанс
    mixed = [round(v * 48) / 48 * 0.25 + v * 0.75 for v in mixed]   # лёгкая «цифра»
    return _echo(mixed, int(rate * 0.12), 0.1, 1)           # эхо — едва-едва, чтобы не гулко


def fx_ship(x: list, rate: int) -> list:
    """«Бортовой компьютер»: узкая полоса как у старого динамика, мягкий тёплый тон, короткое эхо отсека."""
    y = _highpass(_lowpass(x, rate, 3600), rate, 260)
    y = [math.tanh(v * 1.8) / 1.4 for v in y]           # «ламповая» мягкость
    w = 2 * math.pi * 0.7 / rate                        # едва заметное плавание тона
    d0 = int(rate * 0.003)
    y = [v + 0.25 * y[i - d0 - int(d0 * 0.5 * (1 + math.sin(w * i)))] if i > 2 * d0 else v for i, v in enumerate(y)]
    return _comb(y, int(rate * 0.021), 0.2, 0.12)       # чуть-чуть «отсека», без гула


MIND_PITCH = 0.84            # «Сверхразум»: голос на ~3 полутона ниже
MIND_TEMPO = 1.12            # и на ~10% медленнее обычного — размеренно, но не заторможенно


def fx_mind(x: list, rate: int) -> list:
    """«Сверхразум»: низкий плотный голос с металлом внутри — без эха, близко, как в наушниках.
    Тон ниже на ~3 полутона (вместе с ним «тяжелеет» и тембр), тонкий металлический резонанс,
    лёгкое «удвоение» голоса, едва заметная кольцевая модуляция и мягкое насыщение."""
    y = _resample(x, MIND_PITCH)
    d0 = int(rate * 0.009)                              # удвоение: та же фраза с плавающей задержкой 9±3 мс
    w = 2 * math.pi * 0.35 / rate
    y = [v + 0.35 * y[i - d0 - int(rate * 0.003 * math.sin(w * i))] if i > 2 * d0 else v for i, v in enumerate(y)]
    y = _comb(y, int(rate * 0.0016), 0.3, 0.2)          # тонкий металл, не «труба»
    w = 2 * math.pi * 95 / rate
    y = [v * (0.9 + 0.1 * math.sin(w * i)) for i, v in enumerate(y)]
    y = _highpass(y, rate, 60)                          # убираем гул ниже голоса
    return [math.tanh(v * 1.7) / 1.25 for v in y]       # плотность и «давление»


def warmup_hint() -> str:
    return "Голос скачивается один раз (около 85 МБ) — дальше Трой говорит без интернета."


if __name__ == "__main__":                              # проверка эффектов без Piper: python voice.py in.wav style
    import sys
    rate, s = read_wav(Path(sys.argv[1]).read_bytes())
    t = time.time()
    s = {"station": fx_station, "ship": fx_ship, "mind": fx_mind}[sys.argv[2]](s, rate)
    Path(sys.argv[1] + f".{sys.argv[2]}.wav").write_bytes(write_wav(rate, s))
    print(f"{time.time() - t:.1f} c")
    os._exit(0)


# ── Свой голос (Silero) на этом компьютере ──
# Отдельный процесс со своим Python и библиотекой torch: ставится один раз в папку данных Троя
# (~800 МБ) системным Python, модель Евгения (~40 МБ) скачивается при первом запуске.
SILERO = {"proc": None, "busy": False, "step": "", "error": ""}
SILERO_PORT = 8791


def _system_python() -> Optional[list]:
    for cmd in (["py", "-3"], ["python"], ["python3"]):
        try:
            r = subprocess.run(cmd + ["-c", "import sys; print(sys.version_info[:2] >= (3, 9))"], capture_output=True,
                               text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0 and r.stdout.strip() == "True":
                return cmd
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


class LocalSilero:
    def __init__(self, data_dir: Path, service_file: Path, engine: Optional[Path] = None):
        self.dir = data_dir / "voice" / "silero"
        self.src = service_file
        self.engine = engine if engine is not None and engine.exists() else None   # голос, вшитый в установщик
        self.py = self.dir / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def installed(self) -> bool:
        return self.engine is not None or ((self.dir / ".installed").exists() and self.py.exists())

    def possible(self) -> bool:
        return self.engine is not None or (self.src.exists() and (self.installed() or _system_python() is not None))

    def status(self) -> dict:
        return {"ready": self.up(), "busy": SILERO["busy"], "step": SILERO["step"], "pct": 0, "error": SILERO["error"]}

    def up(self) -> bool:
        try:
            return requests.get(f"http://127.0.0.1:{SILERO_PORT}/", timeout=1).status_code == 200
        except requests.RequestException:
            return False

    def ensure(self) -> bool:
        """Готов — True. Нет — запускаем установку/старт в фоне и возвращаем False."""
        if self.up():
            return True
        with _lock:
            if SILERO["busy"]:
                return False
            SILERO.update(busy=True, error="")
        threading.Thread(target=self._prepare, daemon=True).start()
        return False

    def _prepare(self) -> None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            if self.engine is None and not self.installed():
                py = _system_python()
                if not py:
                    raise RuntimeError("на компьютере нет Python 3.9+ (python.org)")
                SILERO["step"] = "окружение для голоса"
                subprocess.run(py + ["-m", "venv", str(self.dir / "venv")], check=True, capture_output=True,
                               timeout=600, creationflags=flags)
                for label, args in (("библиотеку голоса (~200 МБ)", ["torch", "--index-url",
                                                                     "https://download.pytorch.org/whl/cpu"]),
                                    ("numpy", ["numpy"])):
                    SILERO["step"] = label
                    r = subprocess.run([str(self.py), "-m", "pip", "install", "-q", "--no-cache-dir"] + args,
                                       capture_output=True, text=True, timeout=3600, creationflags=flags)
                    if r.returncode != 0:
                        raise RuntimeError((r.stderr or r.stdout)[-300:])
                (self.dir / ".installed").write_text(str(int(time.time())))
            if self.engine is None:
                import shutil
                dst = self.dir / "silero_service.py"
                if not dst.exists() or dst.read_bytes() != self.src.read_bytes():
                    shutil.copyfile(self.src, dst)
            SILERO["step"] = "запускаю голос (первый раз — скачивание модели)"
            self.start()
            for _ in range(180):
                if self.up():
                    log.info("Голос Евгений готов на этом компьютере")
                    return
                if SILERO["proc"] is not None and SILERO["proc"].poll() is not None:
                    raise RuntimeError("процесс голоса завершился — подробности в voice\\silero\\service.log")
                time.sleep(1)
            raise RuntimeError("голос не запустился за 3 минуты")
        except Exception as e:
            log.warning("Голос Евгений не готов: %s", e)
            SILERO["error"] = str(e)[:200]
        finally:
            SILERO.update(busy=False, step="")

    def start(self) -> None:
        p = SILERO["proc"]
        if p is not None and p.poll() is None:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        logf = open(self.dir / "service.log", "ab")
        cmd = [str(self.engine)] if self.engine else [str(self.py), str(self.dir / "silero_service.py")]
        SILERO["proc"] = subprocess.Popen(
            cmd, cwd=str(self.dir), stdout=logf, stderr=logf,
            env={**os.environ, "TROY_VOICE_PORT": str(SILERO_PORT)},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))

    def stop(self) -> None:
        p = SILERO["proc"]
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def synth(self, text: str, speaker: str, speed: float) -> bytes:
        r = requests.post(f"http://127.0.0.1:{SILERO_PORT}/", json={"text": text, "speaker": speaker, "speed": speed},
                          timeout=(2, 60))
        if r.status_code != 200 or r.content[:4] != b"RIFF":
            raise RuntimeError("голос Евгений не ответил")
        return r.content


# ── числа словами (Silero читает только буквы: «16:19», «40 %», «2026» он пропускает) ──
_U = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять", "одиннадцать",
      "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать",
      "девятнадцать"]
_T = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_H = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]
_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
           "декабря"]
_ORD = {1: "первое", 2: "второе", 3: "третье", 4: "четвёртое", 5: "пятое", 6: "шестое", 7: "седьмое", 8: "восьмое",
        9: "девятое", 10: "десятое", 11: "одиннадцатое", 12: "двенадцатое", 13: "тринадцатое", 14: "четырнадцатое",
        15: "пятнадцатое", 16: "шестнадцатое", 17: "семнадцатое", 18: "восемнадцатое", 19: "девятнадцатое",
        20: "двадцатое", 30: "тридцатое"}


def _three(n: int, fem: bool = False) -> list:
    out = []
    if n >= 100:
        out.append(_H[n // 100])
        n %= 100
    if n >= 20:
        out.append(_T[n // 10])
        n %= 10
    if n or not out:
        if n or not out:
            w = _U[n]
            if fem and n in (1, 2):
                w = "одна" if n == 1 else "две"
            if n or not out:
                out.append(w)
    return [w for w in out if w and not (w == "ноль" and len(out) > 1)]


def num_words(n: int) -> str:
    if n < 0:
        return "минус " + num_words(-n)
    if n == 0:
        return "ноль"
    parts = []
    for div, forms, fem in ((10 ** 9, ("миллиард", "миллиарда", "миллиардов"), False),
                            (10 ** 6, ("миллион", "миллиона", "миллионов"), False),
                            (1000, ("тысяча", "тысячи", "тысяч"), True)):
        k = n // div
        if k:
            if not (div == 1000 and k == 1):                  # «тысяча», а не «одна тысяча»
                parts += _three(k, fem)
            d, dd = k % 10, k % 100
            parts.append(forms[0] if d == 1 and dd != 11 else forms[1] if 2 <= d <= 4 and not 12 <= dd <= 14
                         else forms[2])
            n %= div
    if n:
        parts += _three(n)
    return " ".join(parts)


def _day(d: int) -> str:
    if d in _ORD:
        return _ORD[d]
    return f"{_T[d // 10]} {_ORD[d % 10]}"


def say_numbers(text: str) -> str:
    """Время, даты, проценты и числа — словами, чтобы голос их прочитал."""
    t = text
    t = re.sub(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", lambda m: (f"{_day(int(m.group(1)))} {_MONTHS[int(m.group(2)) - 1]} "
               f"{num_words(int(m.group(3)))}") if 1 <= int(m.group(1)) <= 31 and 1 <= int(m.group(2)) <= 12
               else m.group(0), t)
    t = re.sub(r"\b(\d{1,2})\.(\d{1,2})\b(?![.,]\d)", lambda m: f"{_day(int(m.group(1)))} {_MONTHS[int(m.group(2)) - 1]}"
               if 1 <= int(m.group(1)) <= 31 and 1 <= int(m.group(2)) <= 12 else m.group(0), t)

    def hhmm(m):
        h, mm = int(m.group(1)), int(m.group(2))
        if h > 23 or mm > 59:
            return m.group(0)
        mins = "ноль-ноль" if mm == 0 else ("ноль " + _U[mm]) if mm < 10 else num_words(mm)
        return f"{num_words(h)} {mins}"
    t = re.sub(r"\b(\d{1,2}):(\d{2})\b", hhmm, t)
    while True:                                                                 # 1 250 000 → 1250000
        t2 = re.sub(r"(\d)[\s\u00a0](\d{3})\b", r"\1\2", t)
        if t2 == t:
            break
        t = t2
    t = re.sub(r"(^|[\s(])[-−](\d)", r"\1минус \2", t)                          # -5 → минус 5
    forms = {"градусов": ("градус", "градуса", "градусов"), "процентов": ("процент", "процента", "процентов"),
             "рублей": ("рубль", "рубля", "рублей")}

    def agree(m):                                                               # 4 градусов → 4 градуса
        n, word = int(m.group(1)), m.group(2)
        d, dd = n % 10, n % 100
        f = forms[word]
        return f"{m.group(1)} " + (f[0] if d == 1 and dd != 11 else f[1] if 2 <= d <= 4 and not 12 <= dd <= 14 else f[2])
    t = re.sub(r"\b(\d+)\s+(градусов|процентов|рублей)\b", agree, t)
    t = re.sub(r"\b(\d+)(?=\s+(?:тысяч|минут|секунд|недел|задач|копе|штук|ступен))",   # одна/две — для женского рода
               lambda m: " ".join(_three(int(m.group(1)), True)) if int(m.group(1)) < 1000 else m.group(1), t)
    t = re.sub(r"\b(\d+)[.,](\d+)\b", lambda m: f"{num_words(int(m.group(1)))} и {num_words(int(m.group(2)))}"
               if len(m.group(2)) <= 3 else m.group(0), t)
    t = re.sub(r"\d+", lambda m: num_words(int(m.group(0))) if len(m.group(0)) <= 12 else m.group(0), t)
    t = t.replace("+", " плюс ").replace("UTC", "по Гринвичу")
    return re.sub(r"\s+", " ", t).strip()
