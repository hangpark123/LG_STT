# Python STT Client (PCM streaming)

LG STT??gRPC ?섑뵆 ?대씪?댁뼵?몄엯?덈떎.  
16-bit 紐⑤끂 PCM(8?칔Hz)??100?칖s(1600B) ?⑥쐞濡??꾩넚?섎ŉ, `write audio / read response / has_result` 濡쒓렇媛 grpcrtgw ?섑뵆怨??숈씪?섍쾶 異쒕젰?⑸땲??

## 以鍮?1. Python 3.10+
2. 媛???섍꼍 + ?섏〈???ㅼ튂
   ```powershell
   python -m venv .venv
   . .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python gen_protos.py
   ```
3. PCM ?뚯씪 以鍮?(?? `ffmpeg -i input.wav -f s16le -ac 1 -ar 8000 output.pcm`)

## ?ㅽ뻾 ??```powershell
python stt_client.py `
    --target-server nlb.aibot-dev.lguplus.co.kr:13000 `
    --source-file 11_12_Test1.pcm `
    --direction tx `
    --mode rt `
    --sample-rate 8000 `
    --chunk-term-ms 100 `
    --log out.txt
```

### SttWeb怨??ㅼ떆媛??곕룞
??UI???쒖떆??**Session ID**瑜??ъ슜??WebSocket?쇰줈 ?대깽?몃? ?꾩넚?섎㈃, 釉뚮씪?곗? ??꾨씪?몄뿉 LG STT 寃곌낵媛 ?ㅼ떆媛꾩쑝濡??섑??⑸땲??

```powershell
python stt_client.py `
    --target-server nlb.aibot-dev.lguplus.co.kr:13000 `
    --source-file 11_12_Test1.pcm `
    --direction tx `
    --mode rt `
    --sample-rate 8000 `
    --chunk-term-ms 100 `
    --ws-url ws://localhost:5000/lg-feed `
    --ws-session <?몄뀡ID>
```

## 二쇱슂 ?듭뀡
| ?듭뀡 | ?ㅻ챸 | 湲곕낯媛?|
|------|------|--------|
| `--target-server` | gRPC ?쒕쾭 host:port | `localhost:13000` |
| `--source-file` | PCM ?뚯씪 寃쎈줈 | `test.pcm` |
| `--direction` | `rx` / `tx` | `rx` |
| `--mode` | `rt`(?ㅼ떆媛? / `nt` | `rt` |
| `--uuid` | 肄?UUID (誘몄??????먮룞 ?앹꽦) | `""` |
| `--sample-rate` | ?섑뵆?덉씠??Hz) | `8000` |
| `--chunk-term-ms` | 泥?겕 媛꾧꺽(ms) | `100` |
| `--metadata-json` | Init data JSON 寃쎈줈 (`<direction>.json` 湲곕낯) | `""` |
| `--log` | 肄섏넄 濡쒓렇 ?뚯씪 | `""` |
| `--ws-url` | WebSocket ?붾뱶?ъ씤??(?? `ws://localhost:5000/lg-feed`) | `""` |
| `--ws-session` | WebSocket ?몄뀡 ID (`--ws-url` ?ъ슜 ???꾩닔) | `""` |

### JSON ?덉떆 (`tx.json`)
```json
{
  "data": {
    "channel_type": "TX",
    "custom_number": "01012345678",
    "user_exten": "7092"
  }
}
```

## 李멸퀬
- Mic 紐⑤뱶???ы븿?섏뼱 ?덉? ?딆뒿?덈떎.
- PCM ???щ㎎? `ffmpeg` ?깆쑝濡?蹂?????ъ슜?섏꽭??
- Proto ?낅뜲?댄듃 ?꾩뿉??`python gen_protos.py`瑜??ㅼ떆 ?ㅽ뻾?섏꽭??

