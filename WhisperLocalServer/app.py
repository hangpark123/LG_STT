import os
import time
import logging
import tempfile
from io import BytesIO
import wave
import numpy as np
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel
import uvicorn

MODEL_SIZE = os.getenv("WHISPER_MODEL", "base")
DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE")
DOWNLOAD_ROOT = os.getenv("WHISPER_DOWNLOAD_ROOT")
if not COMPUTE_TYPE:
    COMPUTE_TYPE = "int8" if DEVICE == "cpu" else "float16"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("whisper-local")

app = FastAPI()

def _resolve_snapshot_path(spec: str) -> str:
    try:
        if os.path.isdir(spec):
            snap_root = os.path.join(spec, "snapshots")
            if os.path.isdir(snap_root):
                # Prefer refs/main if present
                main_ref = os.path.join(spec, "refs", "main")
                if os.path.isfile(main_ref):
                    with open(main_ref, "r", encoding="utf-8") as f:
                        h = f.read().strip()
                    cand = os.path.join(snap_root, h)
                    if os.path.isdir(cand):
                        return cand
                # Fallback: latest modified snapshot dir
                entries = [
                    os.path.join(snap_root, d)
                    for d in os.listdir(snap_root)
                    if os.path.isdir(os.path.join(snap_root, d))
                ]
                if entries:
                    entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                    return entries[0]
        return spec
    except Exception:
        return spec

MODEL_PATH = _resolve_snapshot_path(MODEL_SIZE)
model = WhisperModel(MODEL_PATH, device=DEVICE, compute_type=COMPUTE_TYPE, download_root=DOWNLOAD_ROOT)
logger.info(
    "Model loaded: size_or_path=%s resolved=%s device=%s compute=%s download_root=%s",
    MODEL_SIZE,
    MODEL_PATH,
    DEVICE,
    COMPUTE_TYPE,
    DOWNLOAD_ROOT,
)

@app.get("/")
def root():
    return {"service": "faster-whisper", "endpoints": ["POST /transcribe", "GET /health", "GET /docs" ]}

@app.get("/health")
def health():
    return {
        "status": "ok",
        "loaded": True,
        "model_path": MODEL_PATH,
        "device": DEVICE,
        "compute": COMPUTE_TYPE,
    }

def _decode_wav_to_16k_float(data: bytes) -> np.ndarray:
    with wave.open(BytesIO(data), 'rb') as wf:
        n_channels = wf.getnchannels()
        fr = wf.getframerate()
        sampwidth = wf.getsampwidth()
        if sampwidth != 2:
            raise ValueError(f"Only 16-bit PCM WAV supported, got sampwidth={sampwidth}")
        frames = wf.readframes(wf.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels == 2:
        audio = audio.reshape(-1, 2).mean(axis=1)
    if fr != 16000:
        # simple linear resample
        x = np.linspace(0, len(audio) - 1, num=len(audio), dtype=np.float32)
        xi = np.linspace(0, len(audio) - 1, num=int(round(len(audio) * (16000 / fr))), dtype=np.float32)
        audio = np.interp(xi, x, audio).astype(np.float32)
    return audio

@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    try:
        data = await file.read()
        logger.info("/transcribe: recv bytes=%d", len(data))
        t0 = time.time()
        audio = _decode_wav_to_16k_float(data)
        dur = len(audio) / 16000.0
        # Simple silence detection (RMS)
        rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
        if dur < 0.35 or rms < 2e-4:
            logger.info("/transcribe: skip (dur=%.3fs, rms=%.4f)", dur, rms)
            return JSONResponse({"text": "", "ms": int((time.time()-t0)*1000), "skipped": True})

        # Language default to Korean unless overridden
        lang_env = os.getenv("WHISPER_LANG", "ko")
        language = None if (not lang_env or lang_env.lower() == "auto") else lang_env
        # VAD default off (robust for short chunks). Enable with WHISPER_VAD=1
        use_vad_env = os.getenv("WHISPER_VAD", "0")
        use_vad = use_vad_env not in ("0", "false", "False", "no", "No")

        segments, info = model.transcribe(
            audio,
            beam_size=1,
            vad_filter=use_vad,
            language=language,
        )
        dt = int((time.time() - t0) * 1000)
        text = "".join(seg.text for seg in segments)
        logger.info("/transcribe: done ms=%d text_len=%d", dt, len(text))
        return JSONResponse({"text": text, "ms": dt})
    except ValueError as e:
        # e.g., language detection on empty segment
        logger.warning("/transcribe: empty or unprocessable chunk: %s", e)
        return JSONResponse({"text": "", "ms": 0, "skipped": True})
    except Exception as e:
        logger.exception("/transcribe error")
        return JSONResponse(status_code=500, content={"error": str(e)})

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "9000")))
