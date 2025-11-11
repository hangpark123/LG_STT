# Python STT Client (backup-friendly)

This is a Python reimplementation of the C# streaming client. It sends audio to the same gRPC ASR service and prints Status/Result in real time, with optional logging to a text file.

## Quick start

1) Install Python 3.10+ and pip
2) Create a venv (recommended)
3) Install deps and generate protobuf stubs

```
python -m venv .venv
. .venv/Scripts/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python gen_protos.py
```

4) Run (file mode)
```
python stt_client.py --file ..\test2.wav --result final --sr 8000 --chunk-ms 200 --tail-ms 1200 --channel tx
```

- Logs are saved to `logs/session_YYYYMMDD_HHMMSS_<callId>.txt` (without [Tx] lines)
- Change server address or headers in CLI options

## CLI options (subset)
- `--address http://host:port` gRPC endpoint
- `--file PATH` file-input mode
- `--sr 8000|16000` sample-rate
- `--chunk-ms N` chunk size in ms
- `--result final|partial|immutable` desired result type
- `--channel rx|tx` channel type
- `--tail-ms N` tail silence appended in file mode
- `--gain X.Y` linear volume multiplier
- `--burst` disable pacing (send as fast as possible)
- `--post-wait-ms N` wait for results after End
- `--log PATH` set log file (txt). Default under `logs/`
- `--no-log` disable file logging
- `--auth` `--policy-id` `--tenant-id` headers
- `--engine` `--client-id` etc. Data map entries

## Notes
- Protobuf stubs are generated from the repo-provided `.proto` files in `protos/`
- Mic mode isn’t included in v1 to keep deps light; file-mode parity with C# is the focus. Can be added with `sounddevice` if needed.

```
# Regenerate protos when proto files change
python gen_protos.py
```
