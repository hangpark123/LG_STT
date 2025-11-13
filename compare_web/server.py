import asyncio
import contextlib
import json
import os
import secrets
import sys
import time
import uuid
import wave
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Tuple

import grpc
import numpy as np
from aiohttp import web, ClientSession, ClientTimeout, FormData, WSMsgType

BASE_DIR = Path(__file__).resolve().parent.parent
AUDIO_ROOT = (BASE_DIR / "python_client").resolve()
STATIC_DIR = Path(__file__).resolve().parent / "static"
WHISPER_URL = os.getenv("WHISPER_URL", "http://127.0.0.1:9000/transcribe")
PCM_DEFAULT_SR = int(os.getenv("PCM_SAMPLE_RATE", "8000"))
CHUNK_MS = int(os.getenv("PCM_CHUNK_MS", "100"))
FLUSH_MS = int(os.getenv("PCM_FLUSH_MS", "7000"))
OVERLAP_MS = int(os.getenv("PCM_OVERLAP_MS", "1200"))
LG_ADDRESS = os.getenv("LG_GRPC_ADDRESS", "nlb.aibot-dev.lguplus.co.kr:13000")
LG_DEFAULT_CHANNEL = os.getenv("LG_CHANNEL_TYPE", "TX").upper()
CHANNEL_NAME_ORDER = ["tx", "rx", "ch3", "ch4", "ch5", "ch6", "ch7", "ch8"]

python_client_path = BASE_DIR / "python_client"
if str(python_client_path) not in sys.path:
    sys.path.insert(0, str(python_client_path))

from recognizer_pb2 import (  # type: ignore  # noqa: E402
    RecognitionRequest,
    RecognitionInitMessage,
    RecognitionEndMessage,
    RecognitionParameters,
    RecognitionType,
    AudioFormat,
    PCM,
    ChannelType,
)
from recognizer_pb2_grpc import RecognizerStub  # type: ignore  # noqa: E402
from rapeech.asr.v1 import result_pb2 as result_messages  # type: ignore  # noqa: E402

SendFunc = Callable[[Dict[str, object]], Awaitable[None]]
session_senders: Dict[str, SendFunc] = {}


def _read_wav_meta(path: Path) -> Tuple[int, int]:
    with wave.open(str(path), "rb") as wf:
        return wf.getnchannels(), wf.getframerate()


def resample_array(data: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return data
    x = np.linspace(0, len(data) - 1, num=len(data), dtype=np.float32)
    xi = np.linspace(0, len(data) - 1, num=int(round(len(data) * dst_sr / src_sr)), dtype=np.float32)
    return np.interp(xi, x, data).astype(np.int16)


def map_channel_names(count: int) -> List[str]:
    names = []
    for idx in range(count):
        if idx < len(CHANNEL_NAME_ORDER):
            names.append(CHANNEL_NAME_ORDER[idx])
        else:
            names.append(f"ch{idx + 1}")
    return names


def discover_samples() -> Dict[str, Dict[str, object]]:
    entries: Dict[str, Dict[str, object]] = {}
    if AUDIO_ROOT.exists():
        for wav in sorted(AUDIO_ROOT.glob("*.wav")):
            base = wav.stem
            entry = entries.setdefault(base, {"label": base})
            entry["wav"] = wav
            try:
                channels, sr = _read_wav_meta(wav)
                entry["channel_count"] = channels
                entry.setdefault("default_sr", sr)
            except Exception:
                entry["channel_count"] = 1
        for pcm in sorted(AUDIO_ROOT.glob("*.pcm")):
            base = pcm.stem
            entry = entries.setdefault(base, {"label": base})
            entry["pcm"] = pcm
            entry.setdefault("channel_count", 1)
    return entries


SAMPLES = discover_samples()
if not SAMPLES:
    raise RuntimeError(f"No audio samples found under {AUDIO_ROOT}")


def prepare_channel_buffers(path: Path, target_sr: int, override_channels: int | None = None) -> Dict[str, bytes]:
    suffix = path.suffix.lower()
    data: np.ndarray
    src_sr = target_sr
    num_channels = 1

    if suffix == ".wav":
        with wave.open(str(path), "rb") as wf:
            src_sr = wf.getframerate()
            num_channels = wf.getnchannels()
            if wf.getsampwidth() != 2:
                raise ValueError("Only 16-bit WAV supported.")
            frames = wf.readframes(wf.getnframes())
        data = np.frombuffer(frames, dtype=np.int16).reshape(-1, num_channels)
    else:
        raw = path.read_bytes()
        if len(raw) % 2 != 0:
            raise ValueError("PCM must be 16-bit little endian.")
        data = np.frombuffer(raw, dtype=np.int16)
        if override_channels and override_channels > 1:
            if len(data) % override_channels != 0:
                raise ValueError("PCM size not divisible by channel count.")
            num_channels = override_channels
            data = data.reshape(-1, num_channels)
        else:
            num_channels = 1
            data = data.reshape(-1, 1)

    channel_names = map_channel_names(num_channels)
    buffers: Dict[str, bytes] = {}
    for idx, name in enumerate(channel_names):
        channel = resample_array(data[:, idx], src_sr, target_sr)
        buffers[name] = channel.astype(np.int16).tobytes()
    return buffers


def resolve_sample_entry(identifier: str | None) -> Dict[str, object]:
    if identifier and identifier in SAMPLES:
        return SAMPLES[identifier]
    if identifier:
        path = Path(identifier)
        if not path.is_absolute():
            path = AUDIO_ROOT / identifier
        if path.exists():
            entry: Dict[str, object] = {"label": path.stem}
            if path.suffix.lower() == ".wav":
                entry["wav"] = path
                try:
                    channels, sr = _read_wav_meta(path)
                    entry["channel_count"] = channels
                    entry["default_sr"] = sr
                except Exception:
                    entry["channel_count"] = 1
            else:
                entry["pcm"] = path
                entry["channel_count"] = 1
            return entry
    first_key = next(iter(SAMPLES))
    return SAMPLES[first_key]


async def handle_index(request):
    return web.FileResponse(STATIC_DIR / "index.html")


async def list_samples(request):
    items = []
    for base, info in SAMPLES.items():
        def rel(p):
            if not p:
                return None
            p = Path(p)
            try:
                return str(p.relative_to(BASE_DIR))
            except ValueError:
                return str(p)
        pcm_rel = rel(info.get("pcm"))
        wav_rel = rel(info.get("wav"))
        command = pcm_rel or wav_rel or base
        items.append({
            "id": base,
            "label": info.get("label", base),
            "pcm": pcm_rel,
            "wav": wav_rel,
            "command": command,
        })
    default = items[0]["id"] if items else None
    return web.json_response({"items": items, "default": default})


async def websocket_entry(request):
    params = request.rel_url.query
    try:
        sample_rate = int(params.get("sr", PCM_DEFAULT_SR))
    except ValueError:
        sample_rate = PCM_DEFAULT_SR
    initial_file = params.get("file")
    autoplay = params.get("autoplay", "0").lower() in ("1", "true")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    session_id = secrets.token_hex(8)

    async def send(payload):
        if ws.closed:
            return
        try:
            await ws.send_str(json.dumps(payload, ensure_ascii=False))
        except ConnectionResetError:
            pass

    session_senders[session_id] = send
    await send({"type": "session", "sessionId": session_id})

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()
    whisper_tasks: List[asyncio.Task] = []
    lg_tasks: List[asyncio.Task] = []

    async def stop_stream():
        stop_event.set()
        for task in whisper_tasks + lg_tasks:
            task.cancel()
        for task in whisper_tasks + lg_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        whisper_tasks.clear()
        lg_tasks.clear()

    async def start_stream(target_file: str | None, sr: int):
        nonlocal stop_event, sample_rate
        await stop_stream()
        stop_event = asyncio.Event()
        sample_rate = sr
        entry = resolve_sample_entry(target_file or initial_file)

        pcm_path = entry.get("pcm")
        wav_path = entry.get("wav")
        if not pcm_path and not wav_path:
            await send({"source": "whisper", "type": "error", "error": "Sample has no audio files"})
            return

        lg_source = Path(pcm_path) if pcm_path else Path(wav_path)
        whisper_source = Path(wav_path) if wav_path else Path(pcm_path)
        channel_override = entry.get("channel_count") if isinstance(entry.get("channel_count"), int) else None

        try:
            lg_buffers = prepare_channel_buffers(lg_source, sample_rate, channel_override)
            whisper_buffers = prepare_channel_buffers(whisper_source, sample_rate)
        except Exception as exc:
            await send({"source": "whisper", "type": "error", "error": str(exc)})
            return

        speakers = ", ".join(lg_buffers.keys())
        await send({
            "source": "whisper",
            "type": "info",
            "text": f"Streaming {entry.get('label')} ({speakers}) @ {sample_rate} Hz"
        })

        for speaker, buf in whisper_buffers.items():
            whisper_tasks.append(loop.create_task(
                whisper_worker(speaker, buf, sample_rate, send, stop_event)
            ))
        for speaker, buf in lg_buffers.items():
            lg_tasks.append(loop.create_task(
                lg_worker(speaker, buf, sample_rate, send, stop_event, loop)
            ))

    if autoplay:
        await start_stream(initial_file, sample_rate)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except ValueError:
                    continue
                op = payload.get("op")
                if op == "start":
                    requested_sr = payload.get("sr")
                    try:
                        requested_sr = int(requested_sr)
                    except (TypeError, ValueError):
                        requested_sr = sample_rate
                    await start_stream(payload.get("file") or initial_file, requested_sr)
                elif op == "stop":
                    await stop_stream()
                continue
            if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        await stop_stream()
        session_senders.pop(session_id, None)
        if not ws.closed:
            await ws.close()

    return ws


async def lg_feed(request):
    session_id = request.rel_url.query.get("session", "")
    sender = session_senders.get(session_id)
    if sender is None:
        return web.Response(status=404, text="session not found")

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(msg.data)
            except ValueError:
                continue
            if "source" not in payload:
                payload["source"] = "rapeech"
            await sender(payload)
    finally:
        if not ws.closed:
            await ws.close()
    return ws


async def whisper_worker(speaker, pcm_bytes, sample_rate, send, stop_event: asyncio.Event):
    bytes_per_ms = max(1, sample_rate * 2 // 1000)
    chunk_bytes = max(1, bytes_per_ms * CHUNK_MS)
    flush_bytes = max(chunk_bytes, bytes_per_ms * FLUSH_MS)
    overlap_bytes = max(0, bytes_per_ms * OVERLAP_MS)
    buffer = bytearray()
    seq = 0
    aggregate: List[str] = []
    timeout = ClientTimeout(total=60)
    elapsed_ms = 0.0
    keep_ms = OVERLAP_MS
    total_len = len(pcm_bytes)
    idx = 0
    async with ClientSession(timeout=timeout) as client:
        try:
            while idx < total_len and not stop_event.is_set():
                chunk = pcm_bytes[idx: idx + chunk_bytes]
                idx += chunk_bytes
                buffer.extend(chunk)
                if len(buffer) >= flush_bytes:
                    seq += 1
                    chunk_ms = len(buffer) / bytes_per_ms
                    start_ms = elapsed_ms
                    end_ms = start_ms + chunk_ms
                    await send_whisper_chunk(
                        client, buffer, sample_rate, send, False, seq, aggregate, start_ms, end_ms, speaker
                    )
                    if overlap_bytes > 0 and len(buffer) > overlap_bytes:
                        elapsed_ms += max(0.0, chunk_ms - keep_ms)
                        buffer = bytearray(buffer[-overlap_bytes:])
                    else:
                        elapsed_ms += chunk_ms
                        buffer = bytearray()
            if buffer and not stop_event.is_set():
                seq += 1
                chunk_ms = len(buffer) / bytes_per_ms
                start_ms = elapsed_ms
                end_ms = start_ms + chunk_ms
                await send_whisper_chunk(
                    client, buffer, sample_rate, send, True, seq, aggregate, start_ms, end_ms, speaker
                )
        except Exception as exc:
            await send({"source": "whisper", "speaker": speaker, "type": "error", "error": str(exc)})


async def send_whisper_chunk(
    client,
    pcm_chunk,
    sample_rate,
    send,
    is_final,
    seq,
    aggregate,
    start_ms,
    end_ms,
    speaker,
):
    wav = pcm_to_wav(bytes(pcm_chunk), sample_rate)
    form = FormData()
    form.add_field("file", wav, filename=f"chunk_{seq}.wav", content_type="audio/wav")
    t0 = time.perf_counter()
    async with client.post(WHISPER_URL, data=form) as resp:
        dt = int((time.perf_counter() - t0) * 1000)
        if resp.status >= 400:
            text = await resp.text()
            await send({"source": "whisper", "speaker": speaker, "type": "error", "error": text})
            return
        data = await resp.json()
        text = (data.get("text") or "").strip()
        if text:
            aggregate.append(text)
            combined = " ".join(aggregate).strip()
            await send({
                "source": "whisper",
                "speaker": speaker,
                "type": "final" if is_final else "partial",
                "text": combined,
                "detail": f"chunk#{seq} {len(pcm_chunk)} bytes {dt} ms",
                "startMs": int(start_ms),
                "endMs": int(end_ms),
            })
            if is_final:
                aggregate.clear()
        else:
            await send({
                "source": "whisper",
                "speaker": speaker,
                "type": "info",
                "text": f"chunk#{seq} no speech ({dt} ms)",
                "startMs": int(start_ms),
                "endMs": int(end_ms),
            })


async def lg_worker(speaker, pcm_bytes, sample_rate, send, stop_event: asyncio.Event, loop: asyncio.AbstractEventLoop):
    speaker_lower = speaker.lower()
    if speaker_lower == "rx":
        channel_type = ChannelType.RX
    elif speaker_lower == "tx":
        channel_type = ChannelType.TX
    else:
        try:
            channel_type = ChannelType.Value(LG_DEFAULT_CHANNEL)
        except ValueError:
            channel_type = ChannelType.TX

    def _run():
        def requests():
            call_id = uuid.uuid4().hex
            init = RecognitionInitMessage(
                parameters=RecognitionParameters(
                    audio_format=AudioFormat(pcm=PCM(sample_rate_hz=sample_rate)),
                    recognition_type=RecognitionType.REALTIME,
                    result_type=result_messages.ResultType.IMMUTABLE_PARTIAL,
                ),
                call_id=call_id,
                channel_type=channel_type,
            )
            yield RecognitionRequest(recognition_init_message=init)

            bytes_per_chunk = max(1, sample_rate * 2 * CHUNK_MS // 1000)
            idx = 0
            total = len(pcm_bytes)
            while idx < total:
                if stop_event.is_set():
                    break
                chunk = pcm_bytes[idx: idx + bytes_per_chunk]
                idx += bytes_per_chunk
                yield RecognitionRequest(audio=chunk)
                time.sleep(CHUNK_MS / 1000.0)

            yield RecognitionRequest(recognition_end_message=RecognitionEndMessage(call_id=call_id))

        try:
            with grpc.insecure_channel(LG_ADDRESS) as channel:
                stub = RecognizerStub(channel)
                responses = stub.Recognize(requests())
                for resp in responses:
                    if stop_event.is_set():
                        break
                    if resp.HasField("status"):
                        asyncio.run_coroutine_threadsafe(
                            send({
                                "source": "rapeech",
                                "speaker": speaker,
                                "type": "status",
                                "message": f"{resp.status.staus_code} {resp.status.message}"
                            }),
                            loop,
                        )
                    if resp.HasField("result"):
                        text = resp.result.hypotheses[0].text if resp.result.hypotheses else ""
                        msg_type = "final" if resp.result.result_type == result_messages.ResultType.FINAL else "partial"
                        asyncio.run_coroutine_threadsafe(
                            send({
                                "source": "rapeech",
                                "speaker": speaker,
                                "type": msg_type,
                                "text": text,
                                "startMs": resp.result.abs_start_ms,
                                "endMs": resp.result.abs_end_ms,
                            }),
                            loop,
                        )
        except Exception as exc:
            asyncio.run_coroutine_threadsafe(
                send({"source": "rapeech", "speaker": speaker, "type": "error", "error": str(exc)}),
                loop,
            )

    await asyncio.to_thread(_run)


def pcm_to_wav(pcm, sample_rate, target_rate=16000):
    if not pcm:
        return b""
    if sample_rate != target_rate:
        src = np.frombuffer(pcm, dtype=np.int16)
        resampled = resample_array(src, sample_rate, target_rate)
    else:
        resampled = np.frombuffer(pcm, dtype=np.int16)

    out = bytearray()
    data = resampled.tobytes()
    byte_rate = target_rate * 2
    block_align = 2
    chunk_size = 36 + len(data)
    out.extend(b"RIFF")
    out.extend(chunk_size.to_bytes(4, "little"))
    out.extend(b"WAVEfmt ")
    out.extend((16).to_bytes(4, "little"))
    out.extend((1).to_bytes(2, "little"))
    out.extend((1).to_bytes(2, "little"))
    out.extend(target_rate.to_bytes(4, "little"))
    out.extend(byte_rate.to_bytes(4, "little"))
    out.extend(block_align.to_bytes(2, "little"))
    out.extend((16).to_bytes(2, "little"))
    out.extend(b"data")
    out.extend(len(data).to_bytes(4, "little"))
    out.extend(data)
    return bytes(out)


def create_app():
    app = web.Application()
    app.add_routes([
        web.get("/", handle_index),
        web.get("/api/samples", list_samples),
        web.get("/ws", websocket_entry),
        web.get("/lg-feed", lg_feed),
    ])
    app.add_routes([web.static("/static", STATIC_DIR, show_index=False)])
    return app


if __name__ == "__main__":
    web.run_app(create_app(), port=5000)
