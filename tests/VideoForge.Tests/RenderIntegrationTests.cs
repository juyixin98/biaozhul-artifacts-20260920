using System.Diagnostics;
using System.Text;
using Microsoft.Extensions.Logging.Abstractions;
using VideoForge.Api;
using VideoForge.Api.Models;
using VideoForge.Api.Services;

namespace VideoForge.Tests;

/// <summary>
/// End-to-end render with the real ffmpeg binary: generates sample assets,
/// runs the full JobProcessor pipeline, and verifies the output duration with ffprobe.
/// </summary>
public class RenderIntegrationTests : DbTestBase
{
    public RenderIntegrationTests(PostgresFixture fixture) : base(fixture) { }

    private static bool FfmpegAvailable()
    {
        try
        {
            using var p = Process.Start(new ProcessStartInfo("ffmpeg", "-version")
            { RedirectStandardOutput = true, UseShellExecute = false });
            p!.WaitForExit(10_000);
            return p.ExitCode == 0;
        }
        catch { return false; }
    }

    private async Task<Asset> ImportGeneratedAsync(string name, string kind, string color, int seconds, params string[] tags)
    {
        var ext = kind == "image" ? ".png" : ".mp4";
        var rel = Path.Combine("assets", $"{Guid.NewGuid():N}{ext}");
        var full = Path.Combine(StorageRoot, rel);
        Directory.CreateDirectory(Path.GetDirectoryName(full)!);

        var args = kind == "image"
            ? $"-f lavfi -i color=c={color}:s=640x360:d=1 -frames:v 1 -y \"{full}\""
            : $"-f lavfi -i color=c={color}:s=640x360:r=30:d={seconds} -c:v libx264 -pix_fmt yuv420p -y \"{full}\"";
        var psi = new ProcessStartInfo("ffmpeg", args) { RedirectStandardError = true, UseShellExecute = false };
        using (var p = Process.Start(psi)!) { await p.WaitForExitAsync(); Assert.Equal(0, p.ExitCode); }

        var renderer = new FFmpegRenderer(Opts(Options), NullLogger<FFmpegRenderer>.Instance);
        return await Assets.InsertAsync(new Asset
        {
            Name = name,
            Kind = kind,
            Tags = tags,
            StoredPath = rel,
            OriginalFileName = name,
            ContentType = kind == "image" ? "image/png" : "video/mp4",
            SizeBytes = new FileInfo(full).Length,
            Sha256 = new string('b', 64),
            DurationSeconds = kind == "video"
                ? await renderer.ProbeDurationSecondsAsync(full, CancellationToken.None)
                : null,
        });
    }

    private async Task<double> RenderAndProbeAsync(string transition, double target)
    {
        await ImportGeneratedAsync("sunset", "image", "red", 1, "sunset", "sky");
        await ImportGeneratedAsync("ocean", "video", "blue", 30, "ocean", "waves");
        await ImportGeneratedAsync("forest", "image", "green", 1, "forest", "trees");

        var project = await TestProcessorFactory.SeedProjectAsync(Fixture, target, transition,
            "a sunset over the sky", "ocean waves rolling", "deep forest trees");
        var (job, _) = await Jobs.CreateIfAbsentAsync(project.Id, $"render-{transition}-{target}", 2);

        var processor = TestProcessorFactory.Create(Fixture, Options,
            new FFmpegRenderer(Opts(Options), NullLogger<FFmpegRenderer>.Instance));
        var claimed = await Jobs.ClaimNextAsync();
        await processor.ProcessAsync(claimed!);

        var final = await Jobs.GetAsync(job.Id);
        Assert.Equal(JobStatuses.Completed, final!.Status);

        var output = Path.Combine(StorageRoot, final.OutputPath!);
        Assert.True(File.Exists(output));
        var probe = new FFmpegRenderer(Opts(Options), NullLogger<FFmpegRenderer>.Instance);
        var duration = await probe.ProbeDurationSecondsAsync(output, CancellationToken.None);
        Assert.NotNull(duration);
        return duration!.Value;
    }

    [Fact]
    public async Task Cut_render_matches_target_duration()
    {
        if (!FfmpegAvailable()) return; // environment without ffmpeg: skip gracefully
        var duration = await RenderAndProbeAsync("cut", 12);
        Assert.InRange(duration, 12 - 0.75, 12 + 0.75);
    }

    [Fact]
    public async Task Fade_render_matches_target_duration()
    {
        if (!FfmpegAvailable()) return;
        var duration = await RenderAndProbeAsync("fade", 15);
        Assert.InRange(duration, 15 - 0.75, 15 + 0.75);
    }
}
