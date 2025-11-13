# LG STT vs Whisper Playground

LG STT gRPC 실시간 게이트웨이와 Whisper(STT) 엔진을 나란히 비교하기 위한 데모 프로젝트입니다.  
브라우저에서 버튼 한 번으로 PCM 파일/마이크 입력을 두 엔진에 동시에 흘려보내고, 실시간 로그를 확인할 수 있습니다.

## 구성

| 폴더 | 설명 |
|------|------|
| `compare_web/` | Python(aiohttp) 기반 비교 대시보드. 서버가 Whisper + LG STT gRPC를 동시에 호출하고 WebSocket으로 브라우저에 결과를 송출 |
| `SttWeb/` | C# ASP.NET Core 앱. 마이크/파일 입력을 두 엔진에 보내 비교 |
| `python_client/` | LG STT 공식 Python 클라이언트 (단독 실행용) |
| `WhisperLocalServer/` | faster-whisper HTTP 서버 (`POST /transcribe`) |
| `Protos/` | gRPC proto 정의 |

---

## 1. Whisper 로컬 서버

```powershell
cd WhisperLocalServer
py -3.11 -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt

set WHISPER_MODEL=large-v3
set WHISPER_DEVICE=cpu    # cuda 사용 시 cuda / compute_type=float16
set WHISPER_LANG=ko       # auto 가능

python app.py   # 기본 포트 9000
```

상태 확인: `http://127.0.0.1:9000/health`

---

## 2. compare_web (권장 UI)

```powershell
cd compare_web
py -3.11 -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python server.py   # 기본 포트 5000
```

1. `http://localhost:5000` 접속 → PCM 샘플과 샘플레이트 선택 후 **재생 시작**
2. 서버가 Whisper와 LG STT gRPC를 동시에 호출하여 결과를 WebSocket으로 브라우저에 전송
3. **중지** 버튼으로 즉시 종료, 샘플 변경 후 다시 **재생 시작**
4. Session ID / python 명령은 필요 시 CLI 연동을 위해 표시 (별도 클라이언트 실행 없이도 웹에서 바로 확인 가능)

환경 변수:

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `WHISPER_URL` | Whisper HTTP 엔드포인트 | `http://127.0.0.1:9000/transcribe` |
| `LG_GRPC_ADDRESS` | LG STT gRPC host:port | `nlb.aibot-dev.lguplus.co.kr:13000` |
| `PCM_FLUSH_MS` | Whisper 청크 누적 시간(ms) | `3200` |
| `PCM_OVERLAP_MS` | Whisper 청크 겹침(ms) | `400` |

---

## 3. SttWeb (C# 비교 앱)

```powershell
dotnet run --project SttWeb
```

- 브라우저 `http://localhost:5000`
- **마이크 시작** 또는 **테스트 파일 실행** 버튼으로 두 엔진 결과를 비교
- 환경 변수: `FASTER_WHISPER_URL`, `RAPEECH_ADDRESS`, `WHISPER_CHUNK_MS`, `WHISPER_OVERLAP_MS` 등

---

## 4. python_client (옵션)

별도 CLI 실행이 필요할 때만 사용합니다.

```powershell
cd python_client
py -3.11 -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python gen_protos.py   # proto 수정 후

python stt_client.py --target-server ... --source-file test.pcm ...
```

Web UI와 연동하려면 `--ws-url ws://localhost:5000/lg-feed --ws-session <복사한 세션 ID>` 옵션을 추가하세요.

---

## FAQ

- **버튼을 누르지 않았는데 Whisper가 실행된다?**  
  현재 버전은 재생 버튼을 눌러야만 Whisper/ LG STT 스트림이 시작됩니다. UI에서 “재생 시작”을 누르기 전에는 호출이 발생하지 않습니다.

- **LG STT 로그가 안 보인다**  
  compare_web 서버가 gRPC 클라이언트를 직접 돌립니다. 샘플레이트를 8 kHz로 바꾸거나, 서버 콘솔 로그에서 주소/인증 오류가 없는지 확인하세요.

- **Whisper 문장이 너무 짧다**  
  `PCM_FLUSH_MS`와 `PCM_OVERLAP_MS` 환경 변수를 늘리면 더 긴 문장이 한 번에 나옵니다 (예: 4000 / 500).

필요한 기능이나 문제가 있으면 `compare_web/server.py` 또는 `SttWeb/Program.cs` 설정을 조정하거나 issue로 알려 주세요.
