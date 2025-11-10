# LG_STT – gRPC ASR Client (C#/.NET 8)

이 콘솔 앱은 NAudio로 마이크 입력을 캡처해 gRPC 양방향 스트리밍으로 ASR 서버에 전송하고, 인식 결과를 JSON/텍스트로 출력합니다. 핵심 진입점은 `Program.cs:44`의 `Main`입니다.

## 개요
- 샘플레이트: 기본 8kHz (`Program.cs:20`)
- 전송 청크: 200ms (`Program.cs:21-23`)
- 서버 초기화 OK 상태 이후 오디오 전송 시작 (`Program.cs:78-86`, `Program.cs:196-204`)
- 결과(JSON/텍스트) 실시간 표시, `F`(Final 요청) 또는 `Enter`(종료)로 세션 종료

## 구조
- gRPC 서비스/메시지: `Protos/recognizer.proto:14-16` (Recognize, 양방향 스트리밍)
- 요청 oneof: Init/Audio/End (`Protos/recognizer.proto:24-30`)
- 결과 메시지: Result/Hypothesis/Word (`Protos/rapeech/asr/v1/result.proto:15-20`, `Protos/rapeech/asr/v1/result.proto:41-50`)
- 빌드시 `Grpc.Tools`가 C# 클라이언트 코드를 생성합니다 (`SttClient.csproj:18`)

## 동작 흐름
1) gRPC 채널/클라이언트 생성 (`Program.cs:49-56`)
2) 초기화 메시지 전송 (파라미터/CallId/ChannelType/Data) (`Program.cs:107-129`)
3) 마이크 캡처 시작, 큐에 바이트 버퍼 적재 (`Program.cs:231-299`)
4) 200ms 단위로 오디오 청크 전송 및 페이싱 (`Program.cs:172-210`)
5) 응답 수신 루프: Status로 초기화 확인 → Result 수신 시 JSON/텍스트 표시 (`Program.cs:75-99`)
6) 종료: `F`로 Final 요청 또는 `Enter/타임아웃` 후 End 전송+스트림 완료 (`Program.cs:133-146`, `Program.cs:313-317`)

## 주요 설정값
- 서버 주소: `ADDRESS` (`Program.cs:19`)
- 샘플레이트/청크: `TARGET_SR`, `CHUNK_SEC`, `CHUNK_BYTES` (`Program.cs:20-23`)
- 인증/정책/테넌트 헤더: `AUTH_BEARER`, `SESSION_POLICY_ID`, `TENANT_ID` + 적용 (`Program.cs:25-28`, `Program.cs:50-53`)
- Init 데이터 맵: client/custom_number/user_exten/channel_type/engine (`Program.cs:30-35`, `Program.cs:122-126`)
- 결과/인식 유형: `RESULT_TYPE`, `RECOG_TYPE` (`Program.cs:37-38`)

## 오디오 캡처
- 우선 `WaveInEvent`(MME) 사용, 실패 시 `WasapiCapture`로 폴백 (`Program.cs:231-299`)
- 포맷: 모노 16bit PCM, 샘플레이트는 `TARGET_SR` 적용
- WASAPI float 포맷 시 16bit PCM 변환 후 큐 적재 (`Program.cs:268-284`)
- VU 표시: RMS 기반 막대 출력 (`Program.cs:419-433`)

## 결과 처리
- 원본 JSON 출력 (`Program.cs:91-92`)
- 텍스트 추출: 리플렉션 및 대체 키 탐색 (`Program.cs:347-417`)
  - `Text`/`Transcript`/`Alternatives`/`Words`/JSON 키("transcript"/"text"/"utterance") 순차 탐색
  - `Final`/`IsFinal` 추출 시 `(FINAL)` 표시

## 실행 방법
사전 요구사항: Windows, .NET 8 SDK, 마이크 권한

- 패키지 복원 및 실행
  - `dotnet run`
- 서버/보안 설정
  - `ADDRESS`를 실제 gRPC 엔드포인트로 변경 (`http://` 또는 `https://`) (`Program.cs:19`)
  - 필요 시 인증/정책/테넌트 헤더 설정 (`Program.cs:25-28`)
- 조작
  - `F`: Final 요청(서버에 End 메시지 전송)
  - `Enter`: 세션 종료(타임아웃 `RECORD_SECONDS_TIMEOUT`도 존재, 기본 600초)

## 자주 하는 변경
- 16kHz 전환: `TARGET_SR = 16000` (`Program.cs:20`)
- HTTPS 사용: `ADDRESS`를 `https://...`로 변경 후 서버 인증서 신뢰 구성 (`Program.cs:19`)
- 채널 타입 변경: `ChannelType.Rx`/`Tx` 설정 (`Program.cs:118-121`)
- 결과 형태 조절: `RESULT_TYPE`/`RECOG_TYPE` 수정 (`Program.cs:37-38`)
- 파일 입력 모드 추가(옵션): 현재는 마이크만 송출. 필요 시 `test2.wav`를 읽어 `queue`에 공급하는 경로를 추가할 수 있습니다.

## 프로젝트
- 대상 프레임워크: .NET 8 (`SttClient.csproj:5`)
- 의존성: `Google.Protobuf`, `Grpc.Net.Client`, `Grpc.Tools`, `NAudio` (`SttClient.csproj:11-17`)
- 프로토 자동 생성: `Protos/**.proto` → 클라이언트 코드 (`SttClient.csproj:18`)

## 참고
- 콘솔 한글 출력이 깨질 경우, 터미널 인코딩/폰트를 UTF-8로 맞추거나 파일을 UTF-8(BOM)으로 저장해 보세요.

***
문의나 개선 요청(파일 입력 모드, 설정값 스위치, HTTPS 설정 등)이 있으면 이슈로 남겨 주세요.

