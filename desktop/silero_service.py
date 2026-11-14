"""Голос Троя без Microsoft: Silero (русский нейросинтез) отдельной маленькой службой на этом же сервере.

Служба слушает только 127.0.0.1 — снаружи к ней не попасть; Трой спрашивает её и отдаёт звук программе.
Модель загружается один раз и держится в памяти, поэтому каждая фраза готовится за доли секунды.
Замедление речи без изменения высоты — WSOLA (тот же приём, что у ffmpeg atempo): короткие куски голоса
перекрываются внахлёст, и стык выбирается там, где волны совпадают, — без «бульканья».

Запуск: python silero_service.py  (порт 8790, модель v4_ru.pt рядом с файлом — скачивается сама).
"""
import io
import json
import logging
import os
import threading
import time
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import torch

import sys

HERE = Path(__file__).resolve().parent
MODEL_URL = "https://models.silero.ai/models/tts/ru/v4_ru.pt"
# в программе Трой модель лежит внутри (voice-engine), на сервере — рядом с этим файлом
MODEL = next((p for p in (Path(getattr(sys, "_MEIPASS", HERE)) / "v4_ru.pt", HERE / "v4_ru.pt") if p.exists()),
             HERE / "v4_ru.pt")
PORT = int(os.getenv("TROY_VOICE_PORT", "8790"))
RATE = 24000
SPEAKERS = {"eugene", "aidar", "baya", "kseniya", "xenia"}
log = logging.getLogger("troy.voice")
_lock = threading.Lock()                      # модель — по одной фразе за раз (так быстрее, чем толкаться)


def load():
    if not MODEL.exists():
        log.info("Скачиваю модель Silero...")
        tmp = MODEL.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URL, tmp)
        tmp.replace(MODEL)
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    m = torch.package.PackageImporter(str(MODEL)).load_pickle("tts_models", "model")
    m.to("cpu")
    return m


def wsola(x: np.ndarray, speed: float, sr: int = RATE) -> np.ndarray:
    """Изменить темп без изменения высоты: speed < 1 — медленнее."""
    if abs(speed - 1) < 0.01 or len(x) < sr // 10:
        return x
    n = int(sr * 0.04)                        # окно 40 мс
    hs = n // 2                               # шаг записи
    ha = hs * speed                           # шаг чтения
    tol = int(sr * 0.012)                     # поиск лучшего стыка ±12 мс
    win = np.hanning(n).astype(np.float32)
    out_len = int(len(x) / speed) + n
    out = np.zeros(out_len, np.float32)
    norm = np.zeros(out_len, np.float32)
    xp = np.concatenate([np.zeros(tol, np.float32), x.astype(np.float32), np.zeros(n + 2 * tol, np.float32)])
    prev = None
    k = 0
    while True:
        o = k * hs
        a = int(k * ha) + tol
        if a + n + tol >= len(xp) or o + n > out_len:
            break
        if prev is None:
            best = a
        else:                                 # где кусок лучше всего продолжает предыдущий
            seg = xp[a - tol:a + tol + n]
            target = prev
            corr = np.correlate(seg, target, "valid")
            best = a - tol + int(np.argmax(corr))
        frame = xp[best:best + n]
        out[o:o + n] += frame * win
        norm[o:o + n] += win
        prev = xp[best + hs:best + hs + n]    # естественное продолжение выбранного куска
        k += 1
    norm[norm < 1e-3] = 1
    return (out / norm)[:int(len(x) / speed)]


def to_wav(audio: np.ndarray) -> bytes:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    t = time.time()
    model = load()
    log.info("Голос Silero готов за %.1f с (порт %d)", time.time() - t, PORT)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(200, b'{"ok": true}')

        def do_POST(self):
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                text = str(data.get("text") or "").strip()[:900]
                speaker = data.get("speaker") if data.get("speaker") in SPEAKERS else "eugene"
                speed = min(1.5, max(0.6, float(data.get("speed") or 1)))
                if not text:
                    raise ValueError("нет текста")
                t0 = time.time()
                with _lock:
                    audio = model.apply_tts(text=text, speaker=speaker, sample_rate=RATE,
                                            put_accent=True, put_yo=True).numpy()
                audio = wsola(audio, speed)
                log.info("Фраза %d букв → %.1f с звука за %.2f с", len(text), len(audio) / RATE, time.time() - t0)
                self.reply(200, to_wav(audio), "audio/wav")
            except Exception as e:
                log.warning("Озвучка: %s", e)
                self.reply(500, json.dumps({"error": str(e)[:200]}, ensure_ascii=False).encode())

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.serve_forever()


if __name__ == "__main__":
    main()
