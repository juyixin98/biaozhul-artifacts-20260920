using VideoForge.Api.Models;
using VideoForge.Api.Services;

namespace VideoForge.Tests;

/// <summary>Asset anomalies: no tag match, missing file on disk, render failures and retries.</summary>
public class AssetAnomalyTests : DbTestBase
{
    public AssetAnomalyTests(PostgresFixture fixture) : base(fixture) { }

    private async Task<(Job job, JobProcessor processor, FakeRenderer renderer)> RunOneJobAsync(
        Action? mutate = null, FakeRenderer? renderer = null)
    {
        renderer ??= new FakeRenderer();
        var processor = TestProcessorFactory.Create(Fixture, Options, renderer);
        mutate?.Invoke();
        var claimed = await Jobs.ClaimNextAsync();
        Assert.NotNull(claimed);
        await processor.ProcessAsync(claimed!);
        return ((await Jobs.GetAsync(claimed!.Id))!, processor, renderer);
    }

    [Fact]
    public async Task No_matching_asset_fails_job_with_specific_reason()
    {
        await TestProcessorFactory.SeedAssetAsync(Fixture, Options, "mountain", "image", "mountain", "snow");
        var project = await TestProcessorFactory.SeedProjectAsync(Fixture, 10, "cut",
            "a mountain in the snow", "underwater coral reef");
        await Jobs.CreateIfAbsentAsync(project.Id, "no-match", maxAttempts: 1);

        var (job, _, _) = await RunOneJobAsync();

        Assert.Equal(JobStatuses.Failed, job.Status);
        Assert.Contains("scene 2", job.Error);
        Assert.Contains("coral", job.Error);

        var attempts = await Jobs.ListAttemptsAsync(job.Id);
        var attempt = Assert.Single(attempts);
        Assert.Equal("failed", attempt.Status);
        Assert.Contains("scene 2", attempt.Error);
    }

    [Fact]
    public async Task Missing_asset_file_on_disk_fails_job()
    {
        var asset = await TestProcessorFactory.SeedAssetAsync(Fixture, Options, "red", "image", "red");
        var project = await TestProcessorFactory.SeedProjectAsync(Fixture, 10, "cut", "a red square");
        await Jobs.CreateIfAbsentAsync(project.Id, "missing-file", maxAttempts: 1);

        File.Delete(Path.Combine(StorageRoot, asset.StoredPath)); // asset row exists, file gone

        var (job, _, _) = await RunOneJobAsync();
        Assert.Equal(JobStatuses.Failed, job.Status);
        Assert.Contains("missing on disk", job.Error);
    }

    [Fact]
    public async Task Failed_attempt_is_retried_then_fails_and_keeps_history()
    {
        await TestProcessorFactory.SeedAssetAsync(Fixture, Options, "red", "image", "red");
        var project = await TestProcessorFactory.SeedProjectAsync(Fixture, 10, "cut", "a red square");
        var (job, _) = await Jobs.CreateIfAbsentAsync(project.Id, "retry", maxAttempts: 2);

        var renderer = new FakeRenderer { ProbedDuration = 999 }; // fails duration validation
        var processor = TestProcessorFactory.Create(Fixture, Options, renderer);

        // Attempt 1: fails -> requeued.
        var first = await Jobs.ClaimNextAsync();
        await processor.ProcessAsync(first!);
        var afterFirst = await Jobs.GetAsync(job.Id);
        Assert.Equal(JobStatuses.Queued, afterFirst!.Status);
        Assert.Equal(1, afterFirst.Attempt);

        // Attempt 2: fails -> terminal failed (max attempts reached).
        var second = await Jobs.ClaimNextAsync();
        Assert.NotNull(second);
        await processor.ProcessAsync(second!);
        var afterSecond = await Jobs.GetAsync(job.Id);
        Assert.Equal(JobStatuses.Failed, afterSecond!.Status);
        Assert.Contains("duration check failed", afterSecond.Error);

        // Every attempt kept its log, error and asset version snapshot.
        var attempts = await Jobs.ListAttemptsAsync(job.Id);
        Assert.Equal(2, attempts.Count);
        Assert.All(attempts, a =>
        {
            Assert.Equal("failed", a.Status);
            Assert.NotNull(a.Error);
            Assert.NotNull(a.AssetVersions);
            Assert.Contains("\"assetId\"", a.AssetVersions);
        });

        // No output was ever published.
        Assert.False(File.Exists(Path.Combine(StorageRoot, "outputs", $"{job.Id}.mp4")));
    }

    [Fact]
    public async Task Retry_after_failure_succeeds_and_publishes()
    {
        await TestProcessorFactory.SeedAssetAsync(Fixture, Options, "red", "image", "red");
        var project = await TestProcessorFactory.SeedProjectAsync(Fixture, 10, "cut", "a red square");
        var (job, _) = await Jobs.CreateIfAbsentAsync(project.Id, "retry-ok", maxAttempts: 2);

        var badRenderer = new FakeRenderer { ProbedDuration = 999 };
        var badProcessor = TestProcessorFactory.Create(Fixture, Options, badRenderer);
        await badProcessor.ProcessAsync((await Jobs.ClaimNextAsync())!);
        Assert.Equal(JobStatuses.Queued, (await Jobs.GetAsync(job.Id))!.Status);

        var goodProcessor = TestProcessorFactory.Create(Fixture, Options, new FakeRenderer { ProbedDuration = 10 });
        await goodProcessor.ProcessAsync((await Jobs.ClaimNextAsync())!);

        var final = await Jobs.GetAsync(job.Id);
        Assert.Equal(JobStatuses.Completed, final!.Status);
        Assert.True(File.Exists(Path.Combine(StorageRoot, final.OutputPath!)));
    }

    [Fact]
    public async Task Published_output_is_never_overwritten_by_late_attempt()
    {
        await TestProcessorFactory.SeedAssetAsync(Fixture, Options, "red", "image", "red");
        var project = await TestProcessorFactory.SeedProjectAsync(Fixture, 10, "cut", "a red square");
        var (job, _) = await Jobs.CreateIfAbsentAsync(project.Id, "no-overwrite", maxAttempts: 3);

        var processor = TestProcessorFactory.Create(Fixture, Options, new FakeRenderer { ProbedDuration = 10 });
        await processor.ProcessAsync((await Jobs.ClaimNextAsync())!);
        var completed = await Jobs.GetAsync(job.Id);
        Assert.Equal(JobStatuses.Completed, completed!.Status);
        var outputPath = Path.Combine(StorageRoot, completed.OutputPath!);
        var originalBytes = await File.ReadAllBytesAsync(outputPath);

        // A stale attempt (e.g. from a crashed node) must not be able to complete again.
        Assert.False(await Jobs.TryCompleteAsync(job.Id, attempt: 1, Path.Combine("outputs", $"{job.Id}.mp4")));
        Assert.False(await Jobs.FailOrRequeueAsync(job.Id, attempt: 1, "late failure") is "queued" or "failed");

        var after = await Jobs.GetAsync(job.Id);
        Assert.Equal(JobStatuses.Completed, after!.Status);
        Assert.Equal(originalBytes, await File.ReadAllBytesAsync(outputPath));
    }
}
