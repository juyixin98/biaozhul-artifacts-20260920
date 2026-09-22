using System.Diagnostics;
using System.Text;
using VideoForge.Api.Configuration;
using VideoForge.Api.Matching;
using VideoForge.Api.Storage;

namespace VideoForge.Api.Media;

public sealed record RenderPlanItem(
    int Position,
    string SourcePath,
    string SourceKind,
    long DurationMs,
    long? SourceDurationMs);

public sealed record RenderResult(string OutputPath, long SizeBytes, long DurationMs, string FFmpegLog);

public sealed class RenderCancelledException : Exception;

public sealed class FFmpegRenderer(
    VideoForgeOptions options,
    FileStorage storage,
    ILogger<FFmpegRenderer> logger)
{
    /// <summary>
    /// Renders every scene to a normalized clip, concatenates them, and
    /// writes to the attempt temp path. No file is published here.
    /// Cancellation is observed between (and during) ffmpeg invocations.
    /// </summary>
    public async Task<RenderResult> RenderAsync(
        Guid jobId,
        int attemptNo,
        IReadOnlyList<RenderPlanItem> items,
        string transition,
        CancellationToken cancellationToken)
    {
        var tempOutput = storage.TempOutputPath(jobId, attemptNo);
        storage.TryDelete(tempOutput);

        var log = new StringBuilder();
        var clipPaths = new List<string>();

        var fade = transition == "fade";
        // Fade duration adapts to clip length but never dominates it.
        const long maxFadeMs = 500;

        foreach (var item in items)
        {
            cancellationToken.ThrowIfCancellationRequested();

            var clip = storage.TempClipPath(jobId, attemptNo, item.Position);
            storage.TryDelete(clip);
            clipPaths.Add(clip);

            var fadeMs = fade ? Math.Min(maxFadeMs, item.DurationMs / 3) : 0;
            var args = BuildClipArgs(item, clip, fadeMs);
            // Generous ceiling: encode time scales with duration but is bounded,
            // so a wedged demuxer/decoder cannot hang a worker forever.
            var stepTimeout = TimeSpan.FromSeconds(
                Math.Min(120, 30 + item.DurationMs / 1000.0 * 10));
            await RunFFmpegAsync(args, log, cancellationToken, stepTimeout);

            // The source asset may have been deleted/corrupted between matching and render.
            if (!File.Exists(clip) || new FileInfo(clip).Length == 0)
                throw new InvalidOperationException($"ffmpeg produced no clip for scene {item.Position + 1}.");
        }

        cancellationToken.ThrowIfCancellationRequested();

        // concat demuxer list; paths escaped per ffmpeg rules.
        var listPath = storage.ConcatListPath(jobId, attemptNo);
        var sb = new StringBuilder();
        foreach (var cp in clipPaths)
        {
            var escaped = cp.Replace("'", "'\\''");
            sb.Append("file '").Append(escaped).Append("'\n");
        }
        await File.WriteAllTextAsync(listPath, sb.ToString(), cancellationToken);

        var concatArgs = new List<string>
        {
            "-y", "-f", "concat", "-safe", "0", "-i", listPath,
            "-c", "copy",
            "-movflags", "+faststart",
            tempOutput
        };
        await RunFFmpegAsync(concatArgs, log, cancellationToken, TimeSpan.FromSeconds(60));

        if (!File.Exists(tempOutput) || new FileInfo(tempOutput).Length == 0)
            throw new InvalidOperationException("ffmpeg concat produced no output file.");

        var durationMs = await ProbeDurationMsAsync(tempOutput, cancellationToken);
        return new RenderResult(tempOutput, new FileInfo(tempOutput).Length, durationMs,
            log.Length > 20_000 ? log.ToString(log.Length - 20_000, 20_000) : log.ToString());
    }

    private List<string> BuildClipArgs(RenderPlanItem item, string outPath, long fadeMs)
    {
        var seconds = item.DurationMs / 1000.0;
        var fadeInEnd = fadeMs / 1000.0;
        var fadeOutStart = seconds - fadeInEnd;

        var args = new List<string> { "-y", "-xerror", "-nostdin" };
        if (item.SourceKind == "image")
        {
            // Loop a still image for exactly the scene duration.
            args.AddRange(["-loop", "1", "-framerate", "30", "-t", FormatSeconds(seconds), "-i", item.SourcePath]);
        }
        else
        {
            args.AddRange(["-stream_loop", "-1", "-t", FormatSeconds(seconds), "-i", item.SourcePath]);
        }

        var vf = new StringBuilder();
        vf.Append($"scale={options.OutputWidth}:{options.OutputHeight}:force_original_aspect_ratio=decrease");
        vf.Append($",pad={options.OutputWidth}:{options.OutputHeight}:(ow-iw)/2:(oh-ih)/2:black");
        vf.Append(",setsar=1,fps=30,format=yuv420p");
        if (fadeMs > 0)
            vf.Append(FadeFilter(fadeInEnd, fadeOutStart));

        // Second input: silent audio of the same duration so all clips share
        // an identical stream layout and stream-copy concat always succeeds.
        args.AddRange(["-f", "lavfi", "-t", FormatSeconds(seconds), "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000"]);

        args.AddRange(["-vf", vf.ToString(), "-t", FormatSeconds(seconds)]);
        args.AddRange(["-map", "0:v:0", "-map", "1:a:0"]);
        args.AddRange(["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]);
        args.AddRange(["-c:a", "aac", "-b:a", "96k", "-shortest"]);
        args.AddRange(["-movflags", "+faststart", outPath]);
        return args;
    }

    private static string FadeFilter(double fadeInEnd, double fadeOutStart) =>
        $",fade=t=in:st=0:d={fadeInEnd:0.###},fade=t=out:st={fadeOutStart:0.###}:d={fadeInEnd:0.###}";

    private static string FormatSeconds(double seconds) =>
        seconds.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture);

    private async Task RunFFmpegAsync(List<string> arguments, StringBuilder log,
        CancellationToken ct, TimeSpan timeout)
    {
        var psi = new ProcessStartInfo
        {
            FileName = options.FFmpegPath,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        foreach (var a in arguments) psi.ArgumentList.Add(a);

        using var proc = Process.Start(psi)
            ?? throw new InvalidOperationException($"Cannot start {options.FFmpegPath}");

        log.AppendLine($"$ {options.FFmpegPath} {string.Join(' ', arguments.Select(Quote))}");

        var stderrTask = Task.Run(async () =>
        {
            string? line;
            while ((line = await proc.StandardError.ReadLineAsync(ct)) != null)
            {
                lock (log) log.AppendLine(line);
            }
        }, ct);

        using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeoutCts.CancelAfter(timeout);

        try
        {
            await proc.WaitForExitAsync(timeoutCts.Token);
            await stderrTask;
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            TryKill(proc);
            throw new RenderCancelledException();
        }
        catch (OperationCanceledException)
        {
            TryKill(proc);
            await SafeDrainAsync(stderrTask);
            throw new InvalidOperationException(
                $"ffmpeg timed out after {timeout.TotalSeconds:0}s: {string.Join(' ', arguments.Take(4))}");
        }

        if (proc.ExitCode != 0)
        {
            var tail = log.ToString();
            tail = tail.Length > 4000 ? tail[^4000..] : tail;
            throw new InvalidOperationException($"ffmpeg exited with code {proc.ExitCode}.{Environment.NewLine}{tail}");
        }
    }

    private static async Task SafeDrainAsync(Task t)
    {
        try { await t; }
        catch { /* process killed while reading stderr — expected */ }
    }

    private static void TryKill(Process proc)
    {
        try { if (!proc.HasExited) proc.Kill(entireProcessTree: true); }
        catch { /* best effort */ }
    }

    private static string Quote(string arg) =>
        arg.Contains(' ') || arg.Contains('\'') ? $"'{arg}'" : arg;

    public async Task<long> ProbeDurationMsAsync(string path, CancellationToken ct = default)
    {
        var psi = new ProcessStartInfo
        {
            FileName = options.FFprobePath,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        psi.ArgumentList.Add("-v");
        psi.ArgumentList.Add("error");
        psi.ArgumentList.Add("-show_entries");
        psi.ArgumentList.Add("format=duration");
        psi.ArgumentList.Add("-of");
        psi.ArgumentList.Add("default=noprint_wrappers=1:nokey=1");
        psi.ArgumentList.Add(path);

        using var proc = Process.Start(psi)!;
        var stdout = await proc.StandardOutput.ReadToEndAsync(ct);
        await proc.WaitForExitAsync(ct);
        if (proc.ExitCode != 0 || !double.TryParse(stdout.Trim(),
                System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture,
                out var seconds) || seconds <= 0)
        {
            throw new InvalidOperationException("ffprobe could not determine a valid duration for the rendered file.");
        }
        return (long)Math.Round(seconds * 1000);
    }
}
