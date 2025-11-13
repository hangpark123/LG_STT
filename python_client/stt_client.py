import argparse
import json
import os
import sys
import time
import uuid
import wave
from pathlib import Path
from typing import Iterable, Optional

import grpc
import numpy as np

import recognizer_pb2 as rec_pb2  # type: ignore
import recognizer_pb2_grpc as rec_grpc  # type: ignore
from rapeech.asr.v1 import result_pb2  # type: ignore

try:
    import websocket  # type: ignore
except ImportError:
    websocket = None


def parse_args():
    parser = argparse.ArgumentParser(description="gRPC RTGW-style STT client (PCM only)")
    parser.add_argument("--target-server", type=str, default="localhost:13000",
                        help="gRPC target server (host:port)")
    parser.add_argument("--source-file", type=str, default="test.pcm",
                        help="PCM audio file (16-bit mono 8kHz)")
    parser.add_argument("--direction", choices=["rx", "tx"], default="rx")
    parser.add_argument("--mode", choices=["rt", "nt"], default="rt")
    parser.add_argument("--uuid", type=str, default="")
    parser.add_argument("--sample-rate", type=int, default=8000)
    parser.add_argument("--chunk-term-ms", type=int, default=100)
    parser.add_argument("--metadata-json", type=str, default="",
                        help="Optional metadata JSON path (defaults to <direction>.json if omitted)")
    parser.add_argument("--log", type=str, default="",
                        help="Optional log file path")
    parser.add_argument("--ws-url", type=str, default="",
                        help="Optional WebSocket endpoint to push live events (e.g. ws://localhost:5000/lg-feed)")
    parser.add_argument("--ws-session", type=str, default="",
                        help="Session id used by the Web UI when --ws-url is specified")
    parser.add_argument(
        "--channel-map",
        type=str,
        default="",
        help="Comma separated list of directions (e.g. 'tx,rx'). "
             "When provided, the input PCM/WAV is split per channel and streamed sequentially for each direction.",
    )
    return parser.parse_args()


def load_metadata(direction: str, json_path: Optional[str]):
    path = json_path or f"{direction}.json"
    data = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as jf:
            try:
                j = json.load(jf)
                if "data" in j:
                    data = j["data"]
            except json.JSONDecodeError:
                print(f"[Warn] invalid JSON: {path}")
    return data


def parse_channel_map(map_arg: str) -> list[str]:
    if not map_arg:
        return []
    parts = [p.strip().lower() for p in map_arg.split(",") if p.strip()]
    for part in parts:
        if part not in {"tx", "rx"}:
            raise ValueError(f"Unsupported channel label '{part}'. Use tx or rx.")
    return parts


def split_audio_channels(path: str, channel_map: list[str], fallback_sr: int) -> tuple[dict[str, bytes], int]:
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(path)
    suffix = src.suffix.lower()
    channel_count = len(channel_map)
    if suffix == ".wav":
        with wave.open(str(src), "rb") as wf:
            channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            if channels < channel_count:
                raise ValueError(f"{path} has {channels} channels, but --channel-map expects {channel_count}")
            frames = wf.readframes(wf.getnframes())
        data = np.frombuffer(frames, dtype=np.int16).reshape(-1, channels)
    else:
        raw = src.read_bytes()
        if len(raw) % 2 != 0:
            raise ValueError("PCM file must be 16-bit little endian.")
        data = np.frombuffer(raw, dtype=np.int16)
        if len(data) % channel_count != 0:
            raise ValueError("PCM size is not divisible by channel count length.")
        data = data.reshape(-1, channel_count)
        sample_rate = fallback_sr
    buffers: dict[str, bytes] = {}
    for idx, label in enumerate(channel_map):
        buffers[label] = data[:, idx].astype(np.int16).tobytes()
    return buffers, sample_rate


def audio_generator_from_bytes(payload: bytes, sample_rate: int, chunk_ms: int, mode: str) -> Iterable[rec_pb2.RecognitionRequest]:
    chunk_size = sample_rate * 2 * chunk_ms // 1000
    frame = 0
    for offset in range(0, len(payload), chunk_size):
        chunk = payload[offset:offset + chunk_size]
        if not chunk:
            break
        req = rec_pb2.RecognitionRequest()
        req.audio = chunk
        print(f"write audio (size={len(chunk)}, frame={frame})")
        frame += 1
        yield req
        if mode == "rt":
            time.sleep(chunk_ms / 1000.0)


def create_init_request(direction: str, mode: str, call_uuid: str,
                        sample_rate: int, extra_data: dict) -> rec_pb2.RecognitionRequest:
    init_req = rec_pb2.RecognitionRequest()
    init_msg = init_req.recognition_init_message
    init_msg.call_id = call_uuid or str(uuid.uuid4()).upper()
    init_msg.channel_type = rec_pb2.ChannelType.RX if direction == "rx" else rec_pb2.ChannelType.TX
    if mode == "rt":
        init_msg.parameters.recognition_type = rec_pb2.RecognitionType.REALTIME
        init_msg.parameters.result_type = result_pb2.ResultType.IMMUTABLE_PARTIAL
    else:
        init_msg.parameters.recognition_type = rec_pb2.RecognitionType.NEARREALTIME
        init_msg.parameters.result_type = result_pb2.ResultType.FINAL
    init_msg.parameters.audio_format.pcm.sample_rate_hz = sample_rate
    for k, v in extra_data.items():
        init_msg.data[k] = str(v)
    print(f"UUID: {init_msg.call_id}")
    print("create_init_request() done")
    return init_req


def audio_generator_from_file(path: str, sample_rate: int, chunk_ms: int, mode: str):
    chunk_size = sample_rate * 2 * chunk_ms // 1000  # 16-bit mono
    frame = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            req = rec_pb2.RecognitionRequest()
            req.audio = chunk
            print(f"write audio (size={len(chunk)}, frame={frame})")
            frame += 1
            yield req
            if mode == "rt":
                time.sleep(chunk_ms / 1000.0)


def merged_request(init_req, audio_gen):
    yield init_req
    for req in audio_gen:
        yield req


def run_stream(
    stub: rec_grpc.RecognizerStub,
    direction: str,
    sample_rate: int,
    audio_iter: Iterable[rec_pb2.RecognitionRequest],
    args,
    log,
    emit,
    speaker_tag: Optional[str] = None,
):
    metadata = load_metadata(direction, args.metadata_json or None)
    init_req = create_init_request(direction, args.mode, args.uuid, sample_rate, metadata)
    requests = merged_request(init_req, audio_iter)
    tag = speaker_tag or direction.upper()
    log(f"[{tag}] Start streaming Recognize call...")

    def emit_with_speaker(payload: dict):
        body = dict(payload)
        body.setdefault("speaker", tag)
        emit(body)

    try:
        for resp in stub.Recognize(requests):
            log(f"[{tag}] read response")
            if resp.HasField("status"):
                log(f"[{tag}] has_status ({resp.status.staus_code})")
                emit_with_speaker({
                    "source": "rapeech",
                    "type": "status",
                    "code": int(resp.status.staus_code),
                    "message": resp.status.message or ""
                })
                if resp.status.staus_code != rec_pb2.StatusCode.OK:
                    break
            if resp.HasField("result"):
                if resp.result.hypotheses:
                    hyp = resp.result.hypotheses[0]
                    log(f"[{tag}] hypothesis: {hyp.text}")
                    emit_with_speaker({
                        "source": "rapeech",
                        "type": "final" if resp.result.result_type == result_pb2.ResultType.FINAL else "partial",
                        "text": hyp.text,
                        "startMs": getattr(resp.result, "abs_start_ms", None),
                        "endMs": getattr(resp.result, "abs_end_ms", None)
                    })
                else:
                    log(f"[{tag}] result has no hypothesis")
    except grpc.RpcError as exc:
        log(f"[{tag}] RPC Error: {exc}")


def main():
    args = parse_args()

    if not os.path.exists(args.source_file):
        print(f"PCM file not found: {args.source_file}")
        sys.exit(1)

    log_fp = open(args.log, "w", encoding="utf-8") if args.log else None

    def log(line: str):
        print(line)
        if log_fp:
            log_fp.write(line + "\n")
            log_fp.flush()

    ws_conn = None

    if args.ws_url:
        if websocket is None:
            log("[WS] websocket-client 패키지가 없어 실시간 전송을 비활성화합니다.")
        else:
            target = args.ws_url
            if args.ws_session:
                target += ("&" if "?" in target else "?") + f"session={args.ws_session}"
            try:
                ws_conn = websocket.create_connection(target, timeout=5)
                log(f"[WS] connected to {target}")
            except Exception as exc:
                log(f"[WS] connection failed: {exc}")
                ws_conn = None

    def emit(payload: dict):
        nonlocal ws_conn
        if ws_conn is None:
            return
        try:
            ws_conn.send(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            print(f"[WS error] {exc}")
            try:
                ws_conn.close()
            except Exception:
                pass
            ws_conn = None

    channel = grpc.insecure_channel(args.target_server)
    try:
        grpc.channel_ready_future(channel).result(timeout=10)
        log("Channel state: GRPC_CHANNEL_READY")
        emit({"source": "rapeech", "type": "status", "message": "Channel state: GRPC_CHANNEL_READY"})
    except grpc.FutureTimeoutError:
        log("Channel not ready, exit.")
        emit({"source": "rapeech", "type": "error", "error": "Channel not ready"})
        if log_fp:
            log_fp.close()
        if ws_conn:
            try:
                ws_conn.close()
            except Exception:
                pass
        return

    stub = rec_grpc.RecognizerStub(channel)

    try:
        channel_map = parse_channel_map(args.channel_map)
    except ValueError as exc:
        log(f"[Error] {exc}")
        if log_fp:
            log_fp.close()
        if ws_conn:
            try:
                ws_conn.close()
            except Exception:
                pass
        return

    try:
        if channel_map:
            buffers, effective_sr = split_audio_channels(args.source_file, channel_map, args.sample_rate)
            for label in channel_map:
                buf = buffers[label]
                audio_iter = audio_generator_from_bytes(buf, effective_sr, args.chunk_term_ms, args.mode)
                run_stream(stub, label, effective_sr, audio_iter, args, log, emit, speaker_tag=label.upper())
        else:
            audio_iter = audio_generator_from_file(args.source_file, args.sample_rate, args.chunk_term_ms, args.mode)
            run_stream(stub, args.direction, args.sample_rate, audio_iter, args, log, emit, speaker_tag=args.direction.upper())
    finally:
        log("Finish")
        emit({"source": "rapeech", "type": "info", "text": "Finish"})
        if log_fp:
            log_fp.close()
        if ws_conn:
            try:
                ws_conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
