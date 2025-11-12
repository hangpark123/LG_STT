using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Threading.Channels;
using Google.Protobuf;
using Grpc.Core;
using Grpc.Net.Client;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Rapeech.Asr.V1;
using NAudio.Wave;
using NAudio.Wave.SampleProviders;

var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();

app.UseDefaultFiles();
app.UseStaticFiles();
// Ensure text responses include UTF-8 charset to avoid mojibake
app.Use(async (ctx, next) =>
{
    await next();
    if (ctx.Response.ContentType != null &&
        (ctx.Response.ContentType.StartsWith("text/") || ctx.Response.ContentType.Contains("application/javascript")))
    {
        if (!ctx.Response.ContentType.Contains("charset"))
        {
            ctx.Response.ContentType += "; charset=utf-8";
        }
    }
});
app.UseWebSockets();

app.MapGet("/health", () => Results.Ok(new { status = "ok" }));

app.Map("/ws", async (HttpContext ctx) =>
{
    if (!ctx.WebSockets.IsWebSocketRequest)
    {
        ctx.Response.StatusCode = 400; return;
    }

    int sampleRate = 16000;
    if (int.TryParse(ctx.Request.Query["sr"], out var sr)) sampleRate = sr;
    var fileParam = ctx.Request.Query["file"].ToString();
    var useFile = !string.IsNullOrEmpty(fileParam);

    using var ws = await ctx.WebSockets.AcceptWebSocketAsync();

    var rapeechChannel = Channel.CreateUnbounded<byte[]>(new UnboundedChannelOptions { SingleReader = true, SingleWriter = false });
    var whisperChannel = Channel.CreateUnbounded<byte[]>(new UnboundedChannelOptions { SingleReader = true, SingleWriter = false });

    var cts = new CancellationTokenSource();
    var cancel = cts.Token;

    // Writer: ASR outputs -> client (guard closed sockets)
    var sendLock = new SemaphoreSlim(1, 1);
    async Task SendJsonAsync(object o)
    {
        if (cancel.IsCancellationRequested) return;
        var state = ws.State;
        if (state != WebSocketState.Open && state != WebSocketState.CloseReceived) return;
        var json = JsonSerializer.Serialize(o);
        var seg = new ArraySegment<byte>(Encoding.UTF8.GetBytes(json));
        try
        {
            await sendLock.WaitAsync(cancel);
            state = ws.State;
            if (state != WebSocketState.Open && state != WebSocketState.CloseReceived) return;
            await ws.SendAsync(seg, WebSocketMessageType.Text, true, cancel);
        }
        catch (OperationCanceledException) { }
        catch (WebSocketException) { }
        catch { }
        finally { if (sendLock.CurrentCount == 0) sendLock.Release(); }
    }

    Task feederTask;
    if (!useFile)
    {
        // Reader: client microphone -> channel
        feederTask = Task.Run(async () =>
        {
            try
            {
                var buffer = new byte[8192];
                while (!cancel.IsCancellationRequested && ws.State == WebSocketState.Open)
                {
                    var ms = new MemoryStream();
                    WebSocketReceiveResult result;
                    do
                    {
                        result = await ws.ReceiveAsync(buffer, cancel);
                        if (result.MessageType == WebSocketMessageType.Close)
                        {
                            await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "bye", cancel);
                            cts.Cancel();
                            return;
                        }
                        if (result.MessageType == WebSocketMessageType.Binary)
                        {
                            ms.Write(buffer, 0, result.Count);
                        }
                    } while (!result.EndOfMessage);

                    if (ms.Length > 0)
                    {
                        var chunk = ms.ToArray();
                        await rapeechChannel.Writer.WriteAsync(chunk, cancel);
                        await whisperChannel.Writer.WriteAsync((byte[])chunk.Clone(), cancel);
                    }
                }
            }
            catch { }
            finally
            {
                rapeechChannel.Writer.TryComplete();
                whisperChannel.Writer.TryComplete();
            }
        }, cancel);
    }
    else
    {
        feederTask = Task.Run(async () =>
        {
            string ResolvePath(string candidate)
            {
                if (string.IsNullOrWhiteSpace(candidate)) return candidate;
                return Path.IsPathRooted(candidate) ? candidate : Path.GetFullPath(Path.Combine(app.Environment.ContentRootPath, candidate));
            }

            string pcmPath, wavPath;
            if (string.IsNullOrWhiteSpace(fileParam) || fileParam == "1")
            {
                var sampleDir = Path.Combine(app.Environment.ContentRootPath, "..", "python_client");
                pcmPath = Path.Combine(sampleDir, "11_12_Test1.pcm");
                wavPath = Path.Combine(sampleDir, "11_12_Test1.wav");
            }
            else
            {
                var resolved = ResolvePath(fileParam);
                var ext = Path.GetExtension(resolved).ToLowerInvariant();
                if (ext == ".pcm")
                {
                    pcmPath = resolved;
                    wavPath = Path.ChangeExtension(resolved, ".wav");
                }
                else if (ext == ".wav")
                {
                    wavPath = resolved;
                    pcmPath = Path.ChangeExtension(resolved, ".pcm");
                }
                else
                {
                    pcmPath = resolved + ".pcm";
                    wavPath = resolved + ".wav";
                }
            }

            var lgTask = FeedPcmFileAsync(pcmPath, rapeechChannel.Writer, sampleRate, SendJsonAsync, cancel);
            var whTask = FeedWavFileAsync(wavPath, whisperChannel.Writer, sampleRate, SendJsonAsync, cancel);
            await Task.WhenAll(lgTask, whTask);
        }, cancel);
    }
    // Kick off both backends
    var rapeechTask = RapeechRunner(rapeechChannel.Reader, sampleRate, SendJsonAsync, cancel);
    var whisperTask = WhisperRunner(whisperChannel.Reader, sampleRate, SendJsonAsync, cancel);

    try { await Task.WhenAll(feederTask, rapeechTask, whisperTask); }
    catch { /* swallow if client closed while tasks still running */ }
});

app.Run();

static async Task FeedPcmFileAsync(string path, ChannelWriter<byte[]> writer, int sampleRate, Func<object, Task> send, CancellationToken cancel)
{
    try
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
        {
            await send(new { source = "rapeech", type = "error", error = $"PCM file not found: {path}" });
            return;
        }
        var bytes = await File.ReadAllBytesAsync(path, cancel);
        var chunkMs = 100;
        var bytesPerMs = sampleRate * 2 / 1000;
        var chunkBytes = Math.Max(bytesPerMs, chunkMs * bytesPerMs);
        int cursor = 0;
        while (cursor < bytes.Length && !cancel.IsCancellationRequested)
        {
            var len = Math.Min(chunkBytes, bytes.Length - cursor);
            var chunk = new byte[len];
            Buffer.BlockCopy(bytes, cursor, chunk, 0, len);
            cursor += len;
            await writer.WriteAsync(chunk, cancel);
            await Task.Delay(chunkMs, cancel);
        }
    }
    catch (OperationCanceledException) { }
    catch (Exception ex)
    {
        Console.WriteLine("[PCM feeder] " + ex.Message);
        await send(new { source = "rapeech", type = "error", error = ex.Message });
    }
    finally
    {
        writer.TryComplete();
    }
}

static async Task FeedWavFileAsync(string path, ChannelWriter<byte[]> writer, int sampleRate, Func<object, Task> send, CancellationToken cancel)
{
    try
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
        {
            await send(new { source = "whisper", type = "error", error = $"WAV file not found: {path}" });
            return;
        }
        using var afr = new AudioFileReader(path);
        ISampleProvider sp = afr;
        if (sp.WaveFormat.Channels > 1)
            sp = new StereoToMonoSampleProvider(sp);
        if (sp.WaveFormat.SampleRate != sampleRate)
            sp = new WdlResamplingSampleProvider(sp, sampleRate);
        var wave16 = new SampleToWaveProvider16(sp);
        var chunkMs = 100;
        var bytesPerMs = sampleRate * 2 / 1000;
        var buffer = new byte[chunkMs * bytesPerMs];
        int read;
        while ((read = wave16.Read(buffer, 0, buffer.Length)) > 0 && !cancel.IsCancellationRequested)
        {
            var chunk = buffer.AsSpan(0, read).ToArray();
            await writer.WriteAsync(chunk, cancel);
            await Task.Delay(chunkMs, cancel);
        }
    }
    catch (OperationCanceledException) { }
    catch (Exception ex)
    {
        Console.WriteLine("[WAV feeder] " + ex.Message);
        await send(new { source = "whisper", type = "error", error = ex.Message });
    }
    finally
    {
        writer.TryComplete();
    }
}

static async Task RapeechRunner(ChannelReader<byte[]> audioReader, int sampleRate, Func<object,Task> send, CancellationToken cancel)
{
    var addr = Environment.GetEnvironmentVariable("RAPEECH_ADDRESS") ?? "http://nlb.aibot-dev.lguplus.co.kr:13000";
    using var channel = GrpcChannel.ForAddress(addr);
    var client = new Recognizer.RecognizerClient(channel);

    using var call = client.Recognize(cancellationToken: cancel);

    // Init message
    var init = new RecognitionInitMessage
    {
        Parameters = new RecognitionParameters
        {
            AudioFormat = new AudioFormat { Pcm = new PCM { SampleRateHz = (uint)sampleRate } },
            RecognitionType = RecognitionType.Realtime,
            ResultType = ResultType.ImmutablePartial
        },
        CallId = Guid.NewGuid().ToString(),
        ChannelType = ChannelType.Tx
    };
    await call.RequestStream.WriteAsync(new RecognitionRequest { RecognitionInitMessage = init });

    // Response reader
    var readTask = Task.Run(async () =>
    {
        while (await call.ResponseStream.MoveNext(cancel))
        {
            var resp = call.ResponseStream.Current;
            if (resp.Status != null)
            {
                Console.WriteLine($"[Rapeech][status] {resp.Status.StausCode}: {resp.Status.Message}");
                await send(new { source = "rapeech", type = "status", code = resp.Status.StausCode.ToString(), message = resp.Status.Message });
            }
            if (resp.Result != null)
            {
                var text = resp.Result.Hypotheses.Count > 0 ? resp.Result.Hypotheses[0].Text : string.Empty;
                var isFinal = resp.Result.ResultType == ResultType.Final;
                if (!string.IsNullOrWhiteSpace(text))
                {
                    Console.WriteLine($"[Rapeech][{(isFinal ? "final" : "partial")}] {text}");
                }
                await send(new { source = "rapeech", type = isFinal ? "final" : "partial", text, startMs = resp.Result.AbsStartMs, endMs = resp.Result.AbsEndMs });
            }
        }
    }, cancel);

    try
    {
        await foreach (var chunk in audioReader.ReadAllAsync(cancel))
        {
            await call.RequestStream.WriteAsync(new RecognitionRequest { Audio = ByteString.CopyFrom(chunk) });
        }
        await call.RequestStream.WriteAsync(new RecognitionRequest { RecognitionEndMessage = new RecognitionEndMessage { CallId = init.CallId } });
    }
    catch (Exception ex)
    {
        await send(new { source = "rapeech", type = "error", error = ex.Message });
    }

    await call.RequestStream.CompleteAsync();
    await readTask;
}

static async Task WhisperRunner(ChannelReader<byte[]> audioReader, int sampleRate, Func<object,Task> send, CancellationToken cancel)
{
    var localUrl = Environment.GetEnvironmentVariable("FASTER_WHISPER_URL");
    if (string.IsNullOrEmpty(localUrl))
    {
        try
        {
            using var probe = new HttpClient() { Timeout = TimeSpan.FromMilliseconds(800) };
            var probeResp = await probe.GetAsync("http://127.0.0.1:9000/health", cancel);
            if (probeResp.IsSuccessStatusCode)
            {
                localUrl = "http://127.0.0.1:9000";
            }
        }
        catch { }
    }
    var useLocal = !string.IsNullOrEmpty(localUrl);

    var http = new HttpClient();
    if (!useLocal)
    {
        var apiKey = Environment.GetEnvironmentVariable("OPENAI_API_KEY");
        if (string.IsNullOrEmpty(apiKey))
        {
            await send(new { source = "whisper", type = "error", error = "Set FASTER_WHISPER_URL or OPENAI_API_KEY" });
            await foreach (var _ in audioReader.ReadAllAsync(cancel)) { }
            return;
        }
        http.DefaultRequestHeaders.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", apiKey);
    }

    Console.WriteLine(useLocal ? $"[Whisper] Using local faster-whisper at {localUrl}" : "[Whisper] Using OpenAI whisper-1 API");

    // Rolling buffer (configurable, default 1200ms)
    var targetMs = 1200;
    if (int.TryParse(Environment.GetEnvironmentVariable("WHISPER_CHUNK_MS"), out var cfgMs))
        targetMs = Math.Max(400, Math.Min(4000, cfgMs));
    var bytesPerMs = sampleRate * 2 / 1000; // 16-bit mono
    var buffer = new MemoryStream();
    var lastFlush = DateTime.UtcNow;
    var overlapMs = 250;
    if (int.TryParse(Environment.GetEnvironmentVariable("WHISPER_OVERLAP_MS"), out var cfgOv))
        overlapMs = Math.Max(0, Math.Min(1000, cfgOv));

    long flushSeq = 0;
    async Task FlushAsync()
    {
        if (buffer.Length == 0) return;
        var pcm = buffer.ToArray();
        var wav = BuildWav(pcm, sampleRate, 1, 16);
        try
        {
            var sw = System.Diagnostics.Stopwatch.StartNew();
            var sendBytes = wav.Length;
            if (useLocal)
            {
                using var content = new MultipartFormDataContent();
                content.Add(new ByteArrayContent(wav) { Headers = { ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue("audio/wav") } }, "file", "chunk.wav");
                var resp = await http.PostAsync(new Uri(new Uri(localUrl!), "/transcribe"), content, cancel);
                var json = await resp.Content.ReadAsStringAsync(cancel);
                if (!resp.IsSuccessStatusCode)
                {
                    await send(new { source = "whisper", type = "error", error = $"LOCAL {(int)resp.StatusCode}", detail = json });
                }
                else
                {
                    var doc = JsonDocument.Parse(json);
                    if (doc.RootElement.TryGetProperty("text", out var textEl))
                    {
                        var text = textEl.GetString();
                        if (!string.IsNullOrWhiteSpace(text))
                        {
                            Console.WriteLine($"[Whisper][partial] {text}");
                        }
                        await send(new { source = "whisper", type = "partial", text });
                    }
                }
            }
            else
            {
                using var content = new MultipartFormDataContent();
                content.Add(new StringContent("whisper-1"), "model");
                content.Add(new ByteArrayContent(wav) { Headers = { ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue("audio/wav") } }, "file", "chunk.wav");
                var resp = await http.PostAsync("https://api.openai.com/v1/audio/transcriptions", content, cancel);
                var json = await resp.Content.ReadAsStringAsync(cancel);
                if (!resp.IsSuccessStatusCode)
                {
                    await send(new { source = "whisper", type = "error", error = $"HTTP {(int)resp.StatusCode}", detail = json });
                }
                else
                {
                    var doc = JsonDocument.Parse(json);
                    if (doc.RootElement.TryGetProperty("text", out var textEl))
                    {
                        var text = textEl.GetString();
                        if (!string.IsNullOrWhiteSpace(text))
                        {
                            Console.WriteLine($"[Whisper][partial] {text}");
                        }
                        await send(new { source = "whisper", type = "partial", text });
                    }
                }
            }
            sw.Stop();
            flushSeq++;
            var rtt = (int)sw.ElapsedMilliseconds;
            Console.WriteLine($"[Whisper] Flush {flushSeq} bytes={sendBytes} rtt={rtt}ms");
        }
        catch (Exception ex)
        {
            await send(new { source = "whisper", type = "error", error = ex.Message });
            Console.WriteLine("[Whisper] Error: " + ex);
        }
        finally
        {
            if (overlapMs > 0)
            {
                var keep = Math.Min(pcm.Length, overlapMs * bytesPerMs);
                buffer.SetLength(0);
                if (keep > 0)
                {
                    buffer.Write(pcm, pcm.Length - keep, keep);
                }
            }
            else
            {
                buffer.SetLength(0);
            }
        }
    }

    await foreach (var chunk in audioReader.ReadAllAsync(cancel))
    {
        await buffer.WriteAsync(chunk, 0, chunk.Length, cancel);
        var msSince = (DateTime.UtcNow - lastFlush).TotalMilliseconds;
        if (buffer.Length >= targetMs * bytesPerMs || msSince >= targetMs)
        {
            await FlushAsync();
            lastFlush = DateTime.UtcNow;
        }
    }

    // final flush
    await FlushAsync();
}

static byte[] BuildWav(byte[] pcm16, int sampleRate, short channels, short bitsPerSample)
{
    using var ms = new MemoryStream();
    using var bw = new BinaryWriter(ms);
    int byteRate = sampleRate * channels * bitsPerSample / 8;
    short blockAlign = (short)(channels * bitsPerSample / 8);

    // RIFF header
    bw.Write(Encoding.ASCII.GetBytes("RIFF"));
    bw.Write(36 + pcm16.Length);
    bw.Write(Encoding.ASCII.GetBytes("WAVE"));

    // fmt chunk
    bw.Write(Encoding.ASCII.GetBytes("fmt "));
    bw.Write(16); // PCM chunk size
    bw.Write((short)1); // PCM format
    bw.Write(channels);
    bw.Write(sampleRate);
    bw.Write(byteRate);
    bw.Write(blockAlign);
    bw.Write(bitsPerSample);

    // data chunk
    bw.Write(Encoding.ASCII.GetBytes("data"));
    bw.Write(pcm16.Length);
    bw.Write(pcm16);

    bw.Flush();
    return ms.ToArray();
}
