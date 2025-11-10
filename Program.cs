using System;
using System.IO;
using System.Threading.Tasks;
using System.Collections.Concurrent;
using System.Threading;
using Grpc.Core;
using Grpc.Net.Client;
using Google.Protobuf;
using Rapeech.Asr.V1;
using RpcStatusCode = Rapeech.Asr.V1.StatusCode;
using GrpcStatusCode = Grpc.Core.StatusCode;

using NAudio.Wave;
using NAudio.CoreAudioApi;
using NAudio.Wave.SampleProviders;

class Program
{
    // ===== 손대기 쉽게 모아둔 설정 =====
    const string ADDRESS = "http://nlb.aibot-dev.lguplus.co.kr:13000"; // TLS면 https://
    const int TARGET_SR = 8000;            // ★ 먼저 8000으로 테스트 → 필요시 16000으로 변경
    const double CHUNK_SEC = 0.2;          // 200ms
    static int CHUNK_BYTES => (int)(TARGET_SR * CHUNK_SEC * 2);
    const int RECORD_SECONDS_TIMEOUT = 600;

    // (필요 시) 헤더
    const string AUTH_BEARER = "";         // "Bearer eyJ..."
    const string SESSION_POLICY_ID = "";   // "rt-policy-01"
    const string TENANT_ID = "";           // "lguplus-b2b-dev"

    // Init data(map) — 등록된 값과 반드시 동일하게
    const string DATA_client        = "IRLink-Ivo";
    const string DATA_custom_number = "3002";
    const string DATA_user_exten    = "2825";
    const string DATA_channel_type  = "TX";   // ★ 로컬 마이크면 TX가 일반적
    const string DATA_engine        = "IxiRecognizer";

    static readonly ResultType RESULT_TYPE = ResultType.ImmutablePartial;
    static readonly RecognitionType RECOG_TYPE = RecognitionType.Realtime;

    // 상태 플래그
    static volatile bool _initOk = false;
    static DateTime _lastResultAt = DateTime.UtcNow;

    static async Task Main(string[]? args)
    {
        Console.WriteLine("[Info] 실시간 마이크 스트리밍 시작. (F=Final 강제, Enter=종료)");
        Console.WriteLine($"[Info] SAMPLE_RATE={TARGET_SR}Hz, CHUNK={CHUNK_BYTES} bytes");

        // File input mode: --file <path> or -f <path>
        string? filePath = null;
        if (args != null)
        {
            for (int i = 0; i < args.Length; i++)
            {
                var a = args[i];
                if (string.Equals(a, "--file", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(a, "-f", StringComparison.OrdinalIgnoreCase))
                {
                    if (i + 1 < args.Length) filePath = args[i + 1];
                    break;
                }
            }
        }

        using var channel = GrpcChannel.ForAddress(ADDRESS);
        var headers = new Metadata();
        if (!string.IsNullOrWhiteSpace(AUTH_BEARER))       headers.Add("authorization", AUTH_BEARER);
        if (!string.IsNullOrWhiteSpace(SESSION_POLICY_ID)) headers.Add("x-session-policy-id", SESSION_POLICY_ID);
        if (!string.IsNullOrWhiteSpace(TENANT_ID))         headers.Add("x-tenant-id", TENANT_ID);

        var client = new Recognizer.RecognizerClient(channel);
        using var call = client.Recognize(headers, deadline: DateTime.UtcNow.AddMinutes(15));

        string callId = Guid.NewGuid().ToString();

        // 수신(Task)
        var receiverTask = Task.Run(async () =>
        {
            try
            {
                try
                {
                    var respHeaders = await call.ResponseHeadersAsync;
                    Console.WriteLine($"[Headers] {respHeaders}");
                }
                catch (Exception e)
                {
                    Console.WriteLine($"[Headers error] {e.Message}");
                }

                while (await call.ResponseStream.MoveNext(CancellationToken.None))
                {
                    var resp = call.ResponseStream.Current;
                    if (resp.Status != null)
                    {
                        Console.WriteLine($"[Status] code={resp.Status.StausCode} msg={resp.Status.Message} details={resp.Status.Details}");
                        if (resp.Status.StausCode == RpcStatusCode.Ok)
                        {
                            _initOk = true;
                            _lastResultAt = DateTime.UtcNow; // 워치독 기준점
                        }
                        continue;
                    }
                    if (resp.Result != null)
                    {
                        _lastResultAt = DateTime.UtcNow;
                        var json = Google.Protobuf.JsonFormatter.Default.Format(resp.Result);
                        Console.WriteLine($"[Result JSON]\n{json}");

                        var text = TryExtractText(resp.Result, out bool? isFinal);
                        Console.WriteLine($"[Result TEXT]{(isFinal == true ? " (FINAL)" : "")}: {text ?? "(no text)"}");
                        continue;
                    }
                    Console.WriteLine("[Resp] (empty oneof?)");
                }
            }
            catch (RpcException ex)
            {
                Console.WriteLine($"[gRPC Error] {ex.Status} - {ex.Message}");
            }
        });

        // INIT
        var initReq = new RecognitionRequest
        {
            RecognitionInitMessage = new RecognitionInitMessage
            {
                Parameters = new RecognitionParameters
                {
                    AudioFormat     = new AudioFormat { Pcm = new PCM { SampleRateHz = (uint)TARGET_SR } },
                    RecognitionType = RECOG_TYPE,
                    ResultType      = RESULT_TYPE
                },
                CallId = callId,
                ChannelType = ChannelType.Tx // ★ TX로 고정 (마이크)
            }
        };
        initReq.RecognitionInitMessage.Data.Add("client",        DATA_client);
        initReq.RecognitionInitMessage.Data.Add("custom_number", DATA_custom_number);
        initReq.RecognitionInitMessage.Data.Add("user_exten",    DATA_user_exten);
        initReq.RecognitionInitMessage.Data.Add("channel_type",  DATA_channel_type);
        initReq.RecognitionInitMessage.Data.Add("engine",        DATA_engine);

        await call.RequestStream.WriteAsync(initReq);
        Console.WriteLine($"[Init] sent. callId={callId}");

        var queue = new BlockingCollection<byte[]>(boundedCapacity: 200);

        // 키 입력: F=Final 강제, Enter=종료
        var keyTask = Task.Run(() =>
        {
            while (true)
            {
                var key = Console.ReadKey(true);
                if (key.Key == ConsoleKey.Enter) break;
                if (key.Key == ConsoleKey.F)
                {
                    _ = SendEndAsync(call, callId);
                    Console.WriteLine("[Key] Final 요청(F) 전송.");
                }
            }
        });

        // 워치독(10초간 결과 없으면 힌트)
        var watchdogCts = new CancellationTokenSource();
        var watchdogTask = Task.Run(async () =>
        {
            while (!watchdogCts.IsCancellationRequested)
            {
                await Task.Delay(2000, watchdogCts.Token);
                var idle = (DateTime.UtcNow - _lastResultAt).TotalSeconds;
                if (_initOk && idle > 10)
                {
                    Console.WriteLine("[Warn] 10초간 인식 결과 없음 → RX/TX, 샘플레이트(8k/16k), policy/engine 값 점검. " +
                                      "몇 초 이상 또박또박 말하고, 필요하면 F 키로 Final 강제 후 결과 확인.");
                    _lastResultAt = DateTime.UtcNow; // 중복 경고 억제
                }
            }
        }, watchdogCts.Token);

        // 송신(Task)
        var senderTask = Task.Run(async () =>
        {
            using var pending = new MemoryStream();
            long sentBytes = 0;
            var nextTick = DateTime.UtcNow;

            foreach (var buf in queue.GetConsumingEnumerable())
            {
                pending.Write(buf, 0, buf.Length);
                PrintVu(buf);

                while (pending.Length >= CHUNK_BYTES)
                {
                    var chunk = new byte[CHUNK_BYTES];
                    pending.Position = 0;
                    _ = pending.Read(chunk, 0, CHUNK_BYTES);

                    var left = pending.Length - pending.Position;
                    if (left > 0)
                    {
                        var rest = new byte[left];
                        _ = pending.Read(rest, 0, (int)left);
                        pending.SetLength(0);
                        pending.Write(rest, 0, rest.Length);
                    }
                    else
                    {
                        pending.SetLength(0);
                    }

                    if (_initOk)
                    {
                        await call.RequestStream.WriteAsync(new RecognitionRequest
                        {
                            Audio = ByteString.CopyFrom(chunk)
                        });
                        sentBytes += chunk.Length;
                        Console.WriteLine($"[Tx] +{chunk.Length} bytes (total {sentBytes} bytes)");

                        // 200ms pacing
                        nextTick = nextTick.AddMilliseconds(CHUNK_SEC * 1000);
                        var delay = nextTick - DateTime.UtcNow;
                        if (delay.TotalMilliseconds > 0) await Task.Delay(delay);
                    }
                }
            }

            if (pending.Length > 0 && _initOk)
            {
                var last = pending.ToArray();
                await call.RequestStream.WriteAsync(new RecognitionRequest
                {
                    Audio = ByteString.CopyFrom(last)
                });
                sentBytes += last.Length;
            }

            Console.WriteLine($"[Tx] total={(sentBytes/1024.0):F1} KB");
        });

        // If file input mode is specified, stream file -> queue, then send End and finalize.
        if (!string.IsNullOrWhiteSpace(filePath))
        {
            Console.WriteLine($"[File] input mode: {filePath}");
            try
            {
                // Wait for server init OK to avoid dropping audio before _initOk is set
                var waitStart = DateTime.UtcNow;
                while (!_initOk && (DateTime.UtcNow - waitStart).TotalSeconds < 10)
                {
                    await Task.Delay(50);
                }

                using var afr = new AudioFileReader(filePath);
                ISampleProvider provider = afr; // 32-bit float samples
                if (provider.WaveFormat.SampleRate != TARGET_SR)
                    provider = new WdlResamplingSampleProvider(provider, TARGET_SR);
                if (provider.WaveFormat.Channels == 2)
                    provider = new StereoToMonoSampleProvider(provider) { LeftVolume = 0.5f, RightVolume = 0.5f };

                var waveProvider = new SampleToWaveProvider16(provider); // mono 16-bit
                var buf = new byte[CHUNK_BYTES];
                int read;
                while ((read = waveProvider.Read(buf, 0, buf.Length)) > 0)
                {
                    var outBuf = new byte[read];
                    Buffer.BlockCopy(buf, 0, outBuf, 0, read);
                    if (!queue.IsAddingCompleted)
                    {
                        try { queue.Add(outBuf); } catch { }
                    }
                }
                Console.WriteLine("[File] enqueued all audio from file.");
            }
            finally
            {
                queue.CompleteAdding();
            }

            // Wait for sender to flush everything before sending End
            await senderTask;

            // Send End and complete request stream
            await SendEndAsync(call, callId);
            await call.RequestStream.CompleteAsync();
            Console.WriteLine("[End] sent & request stream completed.");

            // Stop watchdog and wait receiver
            watchdogCts.Cancel();
            try { await watchdogTask; } catch { }
            await receiverTask;

            var st2 = call.GetStatus();
            Console.WriteLine($"[FinalStatus] {st2.StatusCode} - {st2.Detail}");
            var tr2 = call.GetTrailers();
            Console.WriteLine($"[Trailers] {tr2}");
            Console.WriteLine("[Done]");
            return;
        }

        // ===== 입력 장치 열기: MME → 실패 시 WASAPI =====
        IDisposable? capture = null;
        WaveInEvent? waveIn = null;
        WasapiCapture? wasapi = null;

        try
        {
            try
            {
                waveIn = new WaveInEvent
                {
                    WaveFormat = new WaveFormat(TARGET_SR, 16, 1),
                    BufferMilliseconds = (int)(CHUNK_SEC * 1000)
                };
                waveIn.DataAvailable += (s, e) =>
                {
                    if (!_initOk) return;
                    var buf = new byte[e.BytesRecorded];
                    Buffer.BlockCopy(e.Buffer, 0, buf, 0, e.BytesRecorded);
                    if (!queue.IsAddingCompleted)
                    {
                        try { queue.Add(buf); } catch { }
                    }
                };
                waveIn.StartRecording();
                capture = waveIn;
                Console.WriteLine("[Mic] MME 캡처 시작.");
            }
            catch
            {
                var enumerator = new MMDeviceEnumerator();
                MMDevice? mm = null;
                try { mm = enumerator.GetDefaultAudioEndpoint(DataFlow.Capture, Role.Communications); } catch { }
                mm ??= enumerator.GetDefaultAudioEndpoint(DataFlow.Capture, Role.Multimedia);
                if (mm == null) throw new InvalidOperationException("녹음 장치를 찾을 수 없습니다.");

                wasapi = new WasapiCapture(mm, true);
                wasapi.WaveFormat = new WaveFormat(TARGET_SR, 16, 1);
                wasapi.DataAvailable += (s, e) =>
                {
                    if (!_initOk) return;

                    if (wasapi.WaveFormat.Encoding == WaveFormatEncoding.IeeeFloat)
                    {
                        int samples = e.BytesRecorded / 4;
                        var outBuf = new byte[samples * 2];
                        for (int i = 0; i < samples; i++)
                        {
                            float f = BitConverter.ToSingle(e.Buffer, i * 4);
                            int s16 = (int)(f * 32767f);
                            if (s16 < short.MinValue) s16 = short.MinValue;
                            if (s16 > short.MaxValue) s16 = short.MaxValue;
                            outBuf[i * 2] = (byte)(s16 & 0xFF);
                            outBuf[i * 2 + 1] = (byte)((s16 >> 8) & 0xFF);
                        }
                        if (!queue.IsAddingCompleted)
                        {
                            try { queue.Add(outBuf); } catch { }
                        }
                    }
                    else
                    {
                        var buf = new byte[e.BytesRecorded];
                        Buffer.BlockCopy(e.Buffer, 0, buf, 0, e.BytesRecorded);
                        if (!queue.IsAddingCompleted)
                        {
                            try { queue.Add(buf); } catch { }
                        }
                    }
                };
                wasapi.StartRecording();
                capture = wasapi;
                Console.WriteLine("[Mic] WASAPI 캡처 시작.");
            }

            Console.WriteLine("F(파이널 강제) 또는 Enter(종료)를 누르세요.");
            var timeoutTask = Task.Delay(TimeSpan.FromSeconds(RECORD_SECONDS_TIMEOUT));
            await Task.WhenAny(keyTask, timeoutTask);
        }
        finally
        {
            try { waveIn?.StopRecording(); } catch { }
            try { wasapi?.StopRecording(); } catch { }
            queue.CompleteAdding();
            capture?.Dispose();
        }

        // 정상 종료(End) — 이미 F로 보냈다면 중복 전송돼도 문제 없음(서버가 무시)
        await SendEndAsync(call, callId);
        await call.RequestStream.CompleteAsync();
        Console.WriteLine("[End] sent & request stream completed.");

        // 워치독 종료
        watchdogCts.Cancel();
        try { await watchdogTask; } catch { }

        await Task.WhenAll(senderTask, receiverTask);

        var st = call.GetStatus();
        Console.WriteLine($"[FinalStatus] {st.StatusCode} - {st.Detail}");
        var tr = call.GetTrailers();
        Console.WriteLine($"[Trailers] {tr}");

        Console.WriteLine("[Done]");
    }

    static async Task SendEndAsync(AsyncDuplexStreamingCall<RecognitionRequest, RecognitionResponse> call, string callId)
    {
        try
        {
            await call.RequestStream.WriteAsync(new RecognitionRequest
            {
                RecognitionEndMessage = new RecognitionEndMessage { CallId = callId }
            });
        }
        catch
        {
            // 이미 보냈거나 스트림 종료 상태일 수 있음 — 무시
        }
    }

    // 결과 텍스트 뽑기 (리플렉션·JSON 혼합)
    static string? TryExtractText(Google.Protobuf.IMessage resultMsg, out bool? isFinal)
    {
        isFinal = null;
        try
        {
            var t = resultMsg.GetType();
            isFinal = (bool?)t.GetProperty("Final")?.GetValue(resultMsg)
                   ?? (bool?)t.GetProperty("IsFinal")?.GetValue(resultMsg);
            var directText = t.GetProperty("Text")?.GetValue(resultMsg)?.ToString()
                          ?? t.GetProperty("Transcript")?.GetValue(resultMsg)?.ToString();
            if (!string.IsNullOrWhiteSpace(directText)) return directText;
        } catch {}

        try
        {
            var t = resultMsg.GetType();
            var altsProp = t.GetProperty("Alternatives");
            if (altsProp != null)
            {
                var alts = altsProp.GetValue(resultMsg) as System.Collections.IEnumerable;
                if (alts != null)
                {
                    foreach (var alt in alts)
                    {
                        var txt = alt?.GetType().GetProperty("Transcript")?.GetValue(alt)?.ToString()
                               ?? alt?.GetType().GetProperty("Text")?.GetValue(alt)?.ToString();
                        if (!string.IsNullOrWhiteSpace(txt)) return txt;
                        break;
                    }
                }
            }
        } catch {}

        try
        {
            var t = resultMsg.GetType();
            var wordsProp = t.GetProperty("Words");
            var words = wordsProp?.GetValue(resultMsg) as System.Collections.IEnumerable;
            if (words != null)
            {
                var sb = new System.Text.StringBuilder();
                foreach (var w in words)
                {
                    var token = w?.GetType().GetProperty("Text")?.GetValue(w)?.ToString()
                             ?? w?.GetType().GetProperty("Word")?.GetValue(w)?.ToString();
                    if (!string.IsNullOrWhiteSpace(token)) sb.Append(token).Append(' ');
                }
                var joined = sb.ToString().Trim();
                if (joined.Length > 0) return joined;
            }
        } catch {}

        try
        {
            var json = Google.Protobuf.JsonFormatter.Default.Format(resultMsg);
            string[] keys = { "\"transcript\":\"", "\"text\":\"", "\"utterance\":\"" };
            foreach (var k in keys)
            {
                var idx = json.IndexOf(k, StringComparison.OrdinalIgnoreCase);
                if (idx >= 0)
                {
                    var s = idx + k.Length;
                    var e = json.IndexOf('"', s);
                    if (e > s) return json.Substring(s, e - s);
                }
            }
        } catch {}

        return null;
    }

    // 간단 VU 미터
    static void PrintVu(byte[] buf)
    {
        int samples = buf.Length / 2;
        if (samples == 0) return;
        long sum = 0;
        for (int i = 0; i < samples; i++)
        {
            short s = (short)(buf[2*i] | (buf[2*i+1] << 8));
            sum += (long)s * s;
        }
        double rms = Math.Sqrt(sum / (double)samples) / 32768.0; // 0..1
        int bars = (int)Math.Round(rms * 30);
        Console.WriteLine($"[VU] {new string('#', Math.Min(bars, 30))}");
    }
}
