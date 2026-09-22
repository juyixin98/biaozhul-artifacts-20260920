using System.Diagnostics;
using System.Globalization;
using System.Text;
using Microsoft.Extensions.Options;
using VideoForge.Api.Models;

namespace VideoForge.Api.Services;

public sealed record RenderResult(bool Success, string? Error);

public interface IVideoRenderer
{
    Task<RenderResult> RenderAsync(RenderPlan plan, IReadOnlyList<string> inputPaths, string outputPath,
        StringBuilder log, CancellationToken ct);

    Task<double?> ProbeDurationSecondsAsync(string path, CancellationToken ct);
}

/// <summary>Builds the ffmpeg argument string for a render plan.</summary>
public static class RenderPlanBuilder
{
    private static string F(double v) => v.ToString("F3", CultureInfo.InvariantCulture);

    public static string BuildArguments(RenderPlan plan, IReadOnlyList<string> inputPaths, string outputPath)
    {
        var inputs = new StringBuilder();
        var filter = new StringBuilder();
        var segments = plan.Segments;

        for (var i = 0; i < segments.Count; i++)
        {
            var seg = segments[i];
            var path = inputPaths[i];
            if (seg.Asset.Kind == AssetKinds.Image)
            {
                inputs.Append(CultureInfo.InvariantCulture, $"-loop 1 -framerate 30 -t {F(seg.DurationSeconds)} -i \"{path}\" ");
            }
            else
            {
                // Loop short clips so they always cover the segment duration.
                if (seg.Asset.DurationSeconds is { } d && d < seg.DurationSeconds + 0.05)
                    inputs.Append("-stream_loop -1 ");
                inputs.Append(CultureInfo.InvariantCulture, $"-i \"{path}\" ");
            }
            filter.Append(CultureInfo.InvariantCulture,
                $"[{i}:v]scale=1280:720:force_original_aspect_ratio=decrease," +
                $"pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p," +
                $"trim=duration={F(seg.DurationSeconds)},setpts=PTS-STARTPTS[v{i}];");
        }

        string last;
        if (segments.Count == 1)
        {
            last = "v0";
        }
        else if (plan.FadeSeconds > 0)
        {
            var offset = 0.0;
            var prev = "v0";
            for (var k = 1; k < segments.Count; k++)
            {
                offset += segments[k - 1].DurationSeconds - (k > 1 ? plan.FadeSeconds : 0);
                var xfadeOffset = offset - plan.FadeSeconds;
                var label = k == segments.Count - 1 ? "vout" : $"x{k}";
                filter.Append(CultureInfo.InvariantCulture,
                    $"[{prev}][v{k}]xfade=transition=fade:duration={F(plan.FadeSeconds)}:offset={F(xfadeOffset)}[{label}];");
                prev = label;
            }
            last = prev;
        }
        else
        {
            filter.Append(CultureInfo.InvariantCulture,
                $"{string.Join("", Enumerable.Range(0, segments.Count).Select(i => $"[v{i}]"))}" +
                $"concat=n={segments.Count}:v=1:a=0[vout];");
            last = "vout";
        }

        return inputs +
               $"-filter_complex \"{filter}\" " +
               $"-map \"[{last}]\" -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p " +
               $"-movflags +faststart -an -t {F(plan.TargetSeconds + 1)} -y \"{outputPath}\"";
    }
}

public sealed class FFmpegRenderer : IVideoRenderer
{
    private readonly VideoForgeOptions _options;
    private readonly ILogger<FFmpegRenderer> _logger;

    public FFmpegRenderer(IOptions<VideoForgeOptions> options, ILogger<FFmpegRenderer> logger)
    {
        _options = options.Value;
        _logger = logger;
    }

    public async Task<RenderResult> RenderAsync(RenderPlan plan, IReadOnlyList<string> inputPaths, string outputPath,
        StringBuilder log, CancellationToken ct)
    {
        var args = RenderPlanBuilder.BuildArguments(plan, inputPaths, outputPath);
        log.AppendLine("ffmpeg " + args);
        var (exitCode, stderr) = await RunProcessAsync("ffmpeg", args,
            TimeSpan.FromSeconds(_options.RenderTimeoutSeconds), ct);
        AppendCapped(log, stderr);
        if (exitCode != 0)
            return new RenderResult(false, $"ffmpeg exited with code {exitCode}: {Tail(stderr, 500)}");
        if (!File.Exists(outputPath))
            return new RenderResult(false, "ffmpeg reported success but produced no output file");
        return new RenderResult(true, null);
    }

    public async Task<double?> ProbeDurationSecondsAsync(string path, CancellationToken ct)
    {
        var (exitCode, stdout) = await RunProcessAsync("ffprobe",
            $"-v error -show_entries format=duration -of csv=p=0 \"{path}\"",
            TimeSpan.FromSeconds(30), ct, captureStdOut: true);
        if (exitCode != 0) return null;
        return double.TryParse(stdout.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out var d) ? d : null;
    }

    private async Task<(int ExitCode, string Output)> RunProcessAsync(
        string fileName, string arguments, TimeSpan timeout, CancellationToken ct, bool captureStdOut = false)
    {
        var psi = new ProcessStartInfo
        {
            FileName = fileName,
            Arguments = arguments,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        using var process = new Process { StartInfo = psi };
        var stdout = new StringBuilder();
        var stderr = new StringBuilder();
        process.OutputDataReceived += (_, e) => { if (e.Data is not null) stdout.AppendLine(e.Data); };
        process.ErrorDataReceived += (_, e) => { if (e.Data is not null) stderr.AppendLine(e.Data); };

        process.Start();
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();

        using var timeoutCts = new CancellationTokenSource(timeout);
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(ct, timeoutCts.Token);
        try
        {
            await process.WaitForExitAsync(linked.Token);
        }
        catch (OperationCanceledException)
        {
            TryKill(process);
            if (ct.IsCancellationRequested) throw;
            return (-1, $"process timed out after {timeout.TotalSeconds}s\n{stderr}");
        }
        return (process.ExitCode, captureStdOut ? stdout.ToString() : stderr.ToString());
    }

    private static void TryKill(Process process)
    {
        try { process.Kill(entireProcessTree: true); } catch { /* already exited */ }
    }

    private static void AppendCapped(StringBuilder sb, string text, int maxChars = 8000)
    {
        if (text.Length <= maxChars) sb.Append(text);
        else sb.Append(text[^maxChars..]);
    }

    private static string Tail(string s, int max) => s.Length <= max ? s : s[^max..];
}
