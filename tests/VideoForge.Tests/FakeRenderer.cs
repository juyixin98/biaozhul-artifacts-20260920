using System.Collections.Concurrent;
using System.Text;
using Microsoft.Extensions.Logging.Abstractions;
using VideoForge.Api;
using VideoForge.Api.Data;
using VideoForge.Api.Models;
using VideoForge.Api.Services;

namespace VideoForge.Tests;

/// <summary>
/// Controllable fake renderer: writes a small file instead of invoking ffmpeg,
/// records concurrency, and can be paused to orchestrate races.
/// </summary>
public sealed class FakeRenderer : IVideoRenderer
{
    private readonly TimeSpan _renderDelay;
    private int _current;
    public int MaxConcurrency { get; private set; }
    public ConcurrentBag<long> RenderedJobIds { get; } = new();
    public Action<long>? OnJobStarted { get; set; }
    public double ProbedDuration { get; set; } = 10.0;

    /// <summary>When set, RenderAsync waits until this task completes (or ct fires).</summary>
    public TaskCompletionSource? Gate { get; set; }

    public FakeRenderer(TimeSpan? renderDelay = null) => _renderDelay = renderDelay ?? TimeSpan.FromMilliseconds(50);

    public async Task<RenderResult> RenderAsync(RenderPlan plan, IReadOnlyList<string> inputPaths, string outputPath,
        StringBuilder log, CancellationToken ct)
    {
        var now = Interlocked.Increment(ref _current);
        lock (this) { MaxConcurrency = Math.Max(MaxConcurrency, now); }
        try
        {
            // Recover the job id from the temp file name ("{jobId}-{attempt}-{guid}.mp4").
            var fileName = Path.GetFileNameWithoutExtension(outputPath);
            if (long.TryParse(fileName.Split('-')[0], out var jobId))
            {
                RenderedJobIds.Add(jobId);
                OnJobStarted?.Invoke(jobId);
            }
            if (Gate is not null)
                await Gate.Task.WaitAsync(ct);
            await Task.Delay(_renderDelay, ct);
            await File.WriteAllBytesAsync(outputPath, new byte[256], ct);
            return new RenderResult(true, null);
        }
        finally
        {
            Interlocked.Decrement(ref _current);
        }
    }

    public Task<double?> ProbeDurationSecondsAsync(string path, CancellationToken ct) =>
        Task.FromResult<double?>(ProbedDuration);
}

public static class TestProcessorFactory
{
    public static JobProcessor Create(PostgresFixture fixture, VideoForgeOptions options, IVideoRenderer renderer,
        JobCancellationRegistry? registry = null) =>
        new(new JobRepository(fixture.Db), new ProjectRepository(fixture.Db), new AssetRepository(fixture.Db),
            new AssetMatcher(), renderer, registry ?? new JobCancellationRegistry(),
            Microsoft.Extensions.Options.Options.Create(options),
            NullLogger<JobProcessor>.Instance);

    public static async Task<Project> SeedProjectAsync(PostgresFixture fixture, double targetSeconds = 10,
        string transition = "cut", params string[] descriptions)
    {
        var repo = new ProjectRepository(fixture.Db);
        return await repo.InsertAsync(new Project
        {
            Name = "test project",
            TargetDurationSeconds = targetSeconds,
            Transition = transition,
            Scenes = descriptions.Select((d, i) => new Scene { Ord = i + 1, Description = d }).ToList(),
        });
    }

    public static async Task<Asset> SeedAssetAsync(PostgresFixture fixture, VideoForgeOptions options,
        string name, string kind, params string[] tags)
    {
        var rel = Path.Combine("assets", $"{Guid.NewGuid():N}.{(kind == "image" ? "png" : "mp4")}");
        var full = Path.Combine(options.StorageRoot, rel);
        Directory.CreateDirectory(Path.GetDirectoryName(full)!);
        await File.WriteAllBytesAsync(full, new byte[128]);
        var repo = new AssetRepository(fixture.Db);
        return await repo.InsertAsync(new Asset
        {
            Name = name,
            Kind = kind,
            Tags = tags,
            StoredPath = rel,
            OriginalFileName = name,
            ContentType = kind == "image" ? "image/png" : "video/mp4",
            SizeBytes = 128,
            Sha256 = new string('a', 64),
            DurationSeconds = kind == "video" ? 30 : null,
        });
    }
}
