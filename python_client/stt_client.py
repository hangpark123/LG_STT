import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
import wave

import numpy as np
from scipy.signal import resample_poly
from google.protobuf.json_format import MessageToJson

import grpc
import grpc.aio as grpc_aio

# Generated modules (after running gen_protos.py)
import recognizer_pb2 as rec_pb2  # type: ignore
import recognizer_pb2_grpc as rec_grpc  # type: ignore
from rapeech.asr.v1 import result_pb2 as result_pb2  # type: ignore


def open_wav(path: Path):
    wf = wave.open(str(path), 'rb')
    channels = wf.getnchannels()
    sampwidth = wf.getsampwidth()
    framerate = wf.getframerate()
    nframes = wf.getnframes()
    pcm = wf.readframes(nframes)
    wf.close()
    return channels, sampwidth, framerate, np.frombuffer(pcm, dtype=np.int16)


def to_mono16_resampled(int16_data: np.ndarray, src_sr: int, target_sr: int, channels: int, gain: float) -> np.ndarray:
    arr = int16_data.astype(np.float32)
    if channels == 2:
        arr = arr.reshape(-1, 2).mean(axis=1)
    if src_sr != target_sr:
        # Use resample_poly for quality and speed
        # Convert to float [-1,1], resample, then back to int16
        x = arr / 32768.0
        # Up/Down factors
        from math import gcd
        g = gcd(src_sr, target_sr)
        up = target_sr // g
        down = src_sr // g
        y = resample_poly(x, up, down).astype(np.float32)
    else:
        y = (arr / 32768.0).astype(np.float32)
    if gain != 1.0:
        y *= gain
    y = np.clip(y, -1.0, 1.0)
    return (y * 32767.0).astype(np.int16)


class TeeLogger:
    def __init__(self, file_path: Path | None):
        self.file_path = file_path
        self.fp = None
        if file_path:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            self.fp = open(file_path, 'w', encoding='utf-8')

    def write(self, line: str):
        print(line)
        if self.fp and not line.startswith('[Tx]'):
            self.fp.write(line + '\n')
            self.fp.flush()

    def close(self):
        try:
            if self.fp:
                self.fp.close()
        except Exception:
            pass


def _make_channel(address: str):
    addr = address.strip()
    use_tls = False
    if addr.startswith('http://'):
        addr = addr[7:]
    elif addr.startswith('https://'):
        addr = addr[8:]
        use_tls = True
    # drop any path suffix
    addr = addr.split('/', 1)[0]
    if use_tls:
        return grpc_aio.secure_channel(addr, grpc.ssl_channel_credentials())
    return grpc_aio.insecure_channel(addr)


async def run(args):
    address = args.address
    target_sr = args.sr
    chunk_ms = args.chunk_ms
    chunk_bytes = int(target_sr * (chunk_ms / 1000.0) * 2)
    result_type_map = {
        'final': result_pb2.ResultType.FINAL,
        'partial': result_pb2.ResultType.PARTIAL,
        'immutable': result_pb2.ResultType.IMMUTABLE_PARTIAL,
    }
    result_type = result_type_map.get(args.result.lower(), result_pb2.ResultType.FINAL)
    channel_type = rec_pb2.ChannelType.TX if args.channel.lower() == 'tx' else rec_pb2.ChannelType.RX

    log_path = None
    if not args.no_log:
        if args.log:
            log_path = Path(args.log)
        else:
            logs_dir = Path(__file__).parent / 'logs'
            logs_dir.mkdir(parents=True, exist_ok=True)
            # placeholder name; will include callId later
            log_path = logs_dir / f'session_{time.strftime("%Y%m%d_%H%M%S")}.txt'
    logger = TeeLogger(log_path)

    # Channel: supports host:port, http://host:port, https://host:port
    channel = _make_channel(address)
    stub = rec_grpc.RecognizerStub(channel)

    metadata = []
    if args.auth:
        metadata.append(('authorization', args.auth))
    if args.policy_id:
        metadata.append(('x-session-policy-id', args.policy_id))
    if args.tenant_id:
        metadata.append(('x-tenant-id', args.tenant_id))

    call_id = args.call_id or __import__('uuid').uuid4().hex
    logger.write(f"[Config] addr={address} SR={target_sr}Hz, chunk={chunk_ms}ms, result={args.result}, channel={args.channel}, tailMs={args.tail_ms}, gain={args.gain:.1f}{' burst' if args.burst else ''}, postWaitMs={args.post_wait_ms}")

    async def request_iter():
        # Init
        init = rec_pb2.RecognitionRequest(
            recognition_init_message=rec_pb2.RecognitionInitMessage(
                parameters=rec_pb2.RecognitionParameters(
                    audio_format=rec_pb2.AudioFormat(pcm=rec_pb2.PCM(sample_rate_hz=target_sr)),
                    recognition_type=rec_pb2.RecognitionType.REALTIME,
                    result_type=result_type,
                ),
                call_id=call_id,
                channel_type=channel_type,
            )
        )
        # Data map
        init.recognition_init_message.data['client'] = args.client_id
        init.recognition_init_message.data['custom_number'] = args.custom_number
        init.recognition_init_message.data['user_exten'] = args.user_exten
        init.recognition_init_message.data['channel_type'] = args.channel.upper()
        init.recognition_init_message.data['engine'] = args.engine
        yield init
        logger.write(f"[Init] sent. callId={call_id}")

        # File mode only (v1)
        if not args.file:
            logger.write('[Error] --file is required in Python v1 client')
            return

        # Wait for init OK (receiver will flip this flag via closure)
        while not init_ok.is_set() and (time.time() - t0) < 10:
            await asyncio.sleep(0.05)

        # Load and prepare audio
        ch, sw, src_sr, data = open_wav(Path(args.file))
        if sw != 2:
            logger.write(f"[Warn] sample width {sw*8} bits not 16-bit PCM; attempting to interpret as 16-bit")
        mono16 = to_mono16_resampled(data, src_sr, target_sr, ch, args.gain)

        # Stream in chunks
        pos = 0
        next_tick = time.time()
        while pos < mono16.size:
            n = min(chunk_bytes//2, mono16.size - pos)
            chunk = mono16[pos:pos+n].tobytes()
            yield rec_pb2.RecognitionRequest(audio=chunk)
            pos += n
            logger.write(f"[Tx] +{len(chunk)} bytes")
            if not args.burst:
                next_tick += (chunk_ms/1000.0)
                delay = next_tick - time.time()
                if delay > 0:
                    await asyncio.sleep(delay)

        # Tail silence
        if args.tail_ms > 0:
            tail_bytes = int(target_sr * (args.tail_ms/1000.0) * 2)
            sent = 0
            zero = b'\x00' * min(chunk_bytes, tail_bytes)
            while sent < tail_bytes:
                n = min(len(zero), tail_bytes - sent)
                yield rec_pb2.RecognitionRequest(audio=zero[:n])
                logger.write(f"[Tx] +{n} bytes")
                sent += n
                if not args.burst:
                    await asyncio.sleep(chunk_ms/1000.0)

        # End
        yield rec_pb2.RecognitionRequest(recognition_end_message=rec_pb2.RecognitionEndMessage(call_id=call_id))
        logger.write('[End] sent')

    init_ok = asyncio.Event()
    t0 = time.time()

    async def receiver(resp_async):
        try:
            async for resp in resp_async:
                if resp.HasField('status'):
                    st = resp.status
                    logger.write(f"[Status] code={st.staus_code} msg={st.message} details={st.details}")
                    # OK -> init completed
                    if st.staus_code == rec_pb2.StatusCode.OK:
                        init_ok.set()
                elif resp.HasField('result'):
                    js = MessageToJson(resp.result, including_default_value_fields=False, preserving_proto_field_name=True)
                    logger.write('[Result JSON]')
                    logger.write(js)
                    # Extract text
                    text = None
                    if resp.result.hypotheses:
                        text = resp.result.hypotheses[0].text or ''
                    is_final = (resp.result.result_type == result_pb2.ResultType.FINAL)
                    logger.write(f"[Result TEXT]{' (FINAL)' if is_final else ''}: {text if text else '(no text)'}")
                else:
                    logger.write('[Resp] (empty oneof?)')
        except grpc.RpcError as e:
            logger.write(f"[gRPC Error] {e.code()} - {e.details()}")

    # Start bidi streaming
    call = stub.Recognize(metadata=tuple(metadata))
    recv_task = asyncio.create_task(receiver(call))
    try:
        async for req in request_iter():
            await call.write(req)
    finally:
        with contextlib.suppress(Exception):
            await call.done_writing()

    # Post-wait for more results
    if args.post_wait_ms > 0:
        try:
            await asyncio.wait_for(recv_task, timeout=args.post_wait_ms/1000.0)
        except asyncio.TimeoutError:
            pass
    else:
        await recv_task

    # Final status/metadata
    try:
        st = await call.code()
        logger.write(f"[FinalStatus] {st}")
        md = await call.trailing_metadata()
        logger.write(f"[Trailers] {md}")
    except Exception:
        pass
    logger.write('[Done]')
    logger.close()


def build_argparser():
    p = argparse.ArgumentParser()
    p.add_argument('--address', default='http://nlb.aibot-dev.lguplus.co.kr:13000')
    p.add_argument('--file')
    p.add_argument('--sr', type=int, default=8000)
    p.add_argument('--chunk-ms', type=int, default=200)
    p.add_argument('--result', choices=['final','partial','immutable'], default='final')
    p.add_argument('--channel', choices=['rx','tx'], default='tx')
    p.add_argument('--tail-ms', type=int, default=1200)
    p.add_argument('--gain', type=float, default=1.0)
    p.add_argument('--burst', action='store_true')
    p.add_argument('--post-wait-ms', type=int, default=3000)
    p.add_argument('--log')
    p.add_argument('--no-log', action='store_true')
    p.add_argument('--auth')
    p.add_argument('--policy-id')
    p.add_argument('--tenant-id')
    p.add_argument('--engine', default='IxiRecognizer')
    p.add_argument('--client-id', default='IRLink-Ivo')
    p.add_argument('--custom-number', default='3002')
    p.add_argument('--user-exten', default='2825')
    p.add_argument('--call-id')
    return p


if __name__ == '__main__':
    import contextlib
    try:
        asyncio.run(run(build_argparser().parse_args()))
    except KeyboardInterrupt:
        pass
