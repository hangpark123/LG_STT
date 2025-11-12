import argparse
import json
import os
import sys
import time
import uuid

import grpc

import recognizer_pb2 as rec_pb2  # type: ignore
import recognizer_pb2_grpc as rec_grpc  # type: ignore
from rapeech.asr.v1 import result_pb2  # type: ignore


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
    return parser.parse_args()


def load_metadata(direction: str, json_path: str | None):
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


def audio_generator(path: str, sample_rate: int, chunk_ms: int, mode: str):
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

    channel = grpc.insecure_channel(args.target_server)
    try:
        grpc.channel_ready_future(channel).result(timeout=10)
        log("Channel state: GRPC_CHANNEL_READY")
    except grpc.FutureTimeoutError:
        log("Channel not ready, exit.")
        if log_fp:
            log_fp.close()
        return

    stub = rec_grpc.RecognizerStub(channel)
    metadata = load_metadata(args.direction, args.metadata_json or None)

    init_req = create_init_request(args.direction, args.mode, args.uuid,
                                   args.sample_rate, metadata)
    audio_gen = audio_generator(args.source_file, args.sample_rate,
                                args.chunk_term_ms, args.mode)
    requests = merged_request(init_req, audio_gen)

    log("Start streaming Recognize call...")
    try:
        for resp in stub.Recognize(requests):
            log("read response")
            if resp.HasField("status"):
                log(f"has_status ({resp.status.staus_code})")
                if resp.status.staus_code != rec_pb2.StatusCode.OK:
                    break
            if resp.HasField("result"):
                log("has_result")
                if resp.result.hypotheses:
                    for idx, hyp in enumerate(resp.result.hypotheses):
                        log(f"hypothesis[{idx}]: {hyp.text}")
                else:
                    log("result has no hypothesis")
    except grpc.RpcError as exc:
        log(f"RPC Error: {exc}")

    log("Finish")
    if log_fp:
        log_fp.close()


if __name__ == "__main__":
    main()
