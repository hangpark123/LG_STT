# Python STT Client (PCM streaming)

이 클라이언트는 raw PCM(16-bit, mono, 8 kHz)을 100 ms(1600바이트) 단위로 gRPC 서버에 보내고, `write audio / read response / has_result` 형태의 로그를 출력합니다. `grpcrtgw_client.py`와 동일한 흐름을 Python 3 환경에서 구동할 수 있도록 정리했습니다.

## 준비
1. Python 3.10+ 설치
2. 가상환경 생성 및 의존성 설치
   ```
   python -m venv .venv
   . .venv/Scripts/activate      # PowerShell
   pip install -r requirements.txt
   python gen_protos.py          # proto → Python stub 생성
   ```
3. PCM 파일 준비 (16-bit, mono, 8 kHz)
   ```
   ffmpeg -i input.wav -f s16le -ac 1 -ar 8000 output.pcm
   ```

## 실행 예
```
python stt_client.py ^
    --target-server nlb.aibot-dev.lguplus.co.kr:13000 ^
    --source-file 11_12_Test1.pcm ^
    --direction tx ^
    --mode rt ^
    --sample-rate 8000 ^
    --chunk-term-ms 100
```

실행하면 아래와 같이 출력됩니다.
```
Channel state: GRPC_CHANNEL_READY
UUID: ...
create_init_request() done
Start streaming Recognize call...
write audio (size=1600, frame=0)
read response
has_status (1)
read response
has_result
hypothesis[0]: ...
...
Finish
```

## 주요 옵션
| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `--target-server` | gRPC 서버 host:port | `localhost:13000` |
| `--source-file` | PCM 파일 경로 | `test.pcm` |
| `--direction` | `rx` 또는 `tx` | `rx` |
| `--mode` | `rt`(real-time) 또는 `nt` | `rt` |
| `--uuid` | 콜 UUID (빈 값이면 자동 생성) | `""` |
| `--sample-rate` | 샘플레이트(Hz) | `8000` |
| `--chunk-term-ms` | 청크 간격(ms) | `100` |
| `--metadata-json` | Init data용 JSON 경로(미지정 시 `<direction>.json`) | `""` |
| `--log` | 콘솔 로그를 함께 저장할 파일 경로 | `""` |

JSON 예시(`tx.json`):
```json
{
  "data": {
    "channel_type": "TX",
    "custom_number": "01012345678",
    "user_exten": "7092"
  }
}
```

## 참고
- Mic 모드는 포함하지 않았습니다.
- PCM 외 포맷을 사용하려면 먼저 변환한 뒤 실행하세요.
- Proto 변경 시 `python gen_protos.py`를 다시 실행하세요.
