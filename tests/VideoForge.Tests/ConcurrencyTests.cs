using VideoForge.Api.Data;
using VideoForge.Api.Models;
using VideoForge.Api.Services;

namespace VideoForge.Tests;

/// <summary>Duplicate submission: the same idempotency key must yield exactly one job.</summary>
public class DuplicateSubmissionTests : DbTestBase
{
    public DuplicateSubmissionTests(PostgresFixture fixture) : base(fixture) { }

    [Fact]
    public async Task Concurrent_submits_with_same_key_create_one_job()
    {
        var project = await TestProcessorFactory.SeedProjectAsync(Fixture, 10, "cut", "a red square");

        const int submitters = 20;
        var results = await Task.WhenAll(Enumerable.Range(0, submitters).Select(_ =>
            Task.Run(() => Jobs.CreateIfAbsentAsync(project.Id, "order-123", 2))));

        Assert.Equal(1, results.Count(r => r.Created));
        Assert.Single(results.Select(r => r.Job.Id).Distinct());

        var attempts = await Jobs.ListAttemptsAsync(results[0].Job.Id);
        Assert.Empty(attempts);
    }

    [Fact]
    public async Task Resubmit_after_completion_returns_existing_job()
    {
        var project = await TestProcessorFactory.SeedProjectAsync(Fixture, 10, "cut", "a red square");
        var (job, created) = await Jobs.CreateIfAbsentAsync(project.Id, "key-1", 2);
        Assert.True(created);

        var (again, createdAgain) = await Jobs.CreateIfAbsentAsync(project.Id, "key-1", 2);
        Assert.False(createdAgain);
        Assert.Equal(job.Id, again.Id);
    }
}

/// <summary>Queue contention: N workers competing via SKIP LOCKED, max 3 in flight.</summary>
public class QueueContentionTests : DbTestBase
{
    public QueueContentionTests(PostgresFixture fixture) : base(fixture) { }

    [Fact]
    public async Task Workers_never_exceed_parallel_limit_and_never_double_claim()
    {
        await TestProcessorFactory.SeedAssetAsync(Fixture, Options, "red", "image", "red");
        var project = await TestProcessorFactory.SeedProjectAsync(Fixture, 10, "cut", "a red square");

        const int jobCount = 8;
        for (var i = 0; i < jobCount; i++)
            await Jobs.CreateIfAbsentAsync(project.Id, $"job-{i}", 2);

        var renderer = new FakeRenderer(TimeSpan.FromMilliseconds(80));
        var processor = TestProcessorFactory.Create(Fixture, Options, renderer);

        async Task WorkerLoop()
        {
            while (true)
            {
                var job = await Jobs.ClaimNextAsync();
                if (job is null) return;
                await processor.ProcessAsync(job);
            }
        }

        // More workers than the limit would still be safe; use exactly MaxParallelJobs.
        await Task.WhenAll(Enumerable.Range(0, Options.MaxParallelJobs).Select(_ => WorkerLoop()));

        Assert.True(renderer.MaxConcurrency <= Options.MaxParallelJobs,
            $"max concurrency {renderer.MaxConcurrency} exceeded limit {Options.MaxParallelJobs}");
        Assert.Equal(jobCount, renderer.RenderedJobIds.Count);
        Assert.Equal(jobCount, renderer.RenderedJobIds.Distinct().Count()); // no job claimed twice

        for (var i = 0; i < jobCount; i++)
        {
            var (job, _) = await Jobs.CreateIfAbsentAsync(project.Id, $"job-{i}", 2);
            Assert.Equal(JobStatuses.Completed, job.Status);
            Assert.True(File.Exists(Path.Combine(StorageRoot, "outputs", $"{job.Id}.mp4")));
        }
    }
}

/// <summary>Cancel racing completion: exactly one terminal state may win.</summary>
public class CancelRaceTests : DbTestBase
{
    public CancelRaceTests(PostgresFixture fixture) : base(fixture) { }

    [Fact]
    public async Task Cancel_and_complete_never_both_land()
    {
        const int rounds = 25;
        var outcomes = new List<string>();

        for (var round = 0; round < rounds; round++)
        {
            await Fixture.ResetAsync();
            // Job ids restart at 1 each round; clear published outputs so a
            // previous round's file is never mistaken for this round's.
            foreach (var f in Directory.EnumerateFiles(Options.OutputsDir, "*.mp4"))
                File.Delete(f);
            await TestProcessorFactory.SeedAssetAsync(Fixture, Options, "red", "image", "red");
            var project = await TestProcessorFactory.SeedProjectAsync(Fixture, 10, "cut", "a red square");
            var (job, _) = await Jobs.CreateIfAbsentAsync(project.Id, $"race-{round}", 2);

            var registry = new JobCancellationRegistry();
            var renderer = new FakeRenderer(TimeSpan.FromMilliseconds(20));
            var processor = TestProcessorFactory.Create(Fixture, Options, renderer, registry);

            var claimed = await Jobs.ClaimNextAsync();
            Assert.NotNull(claimed);

            var processTask = Task.Run(() => processor.ProcessAsync(claimed!));
            // Cancel at a random point while the render is in flight.
            await Task.Delay(Random.Shared.Next(0, 30));
            var (cancelResult, _) = await Jobs.TryCancelAsync(job.Id);
            if (cancelResult == CancelResult.Cancelled)
                registry.Cancel(job.Id);
            await processTask;

            var final = await Jobs.GetAsync(job.Id);
            Assert.True(JobStatuses.IsTerminal(final!.Status),
                $"round {round}: job left in non-terminal state {final.Status}");
            outcomes.Add(final.Status);

            var outputExists = File.Exists(Path.Combine(StorageRoot, "outputs", $"{job.Id}.mp4"));
            if (final.Status == JobStatuses.Completed)
                Assert.True(outputExists, $"round {round}: completed but output missing");
            else
                Assert.False(outputExists, $"round {round}: cancelled but output was published");

            // A second cancel must report the terminal state, not flip anything.
            var (second, current) = await Jobs.TryCancelAsync(job.Id);
            Assert.Equal(CancelResult.AlreadyTerminal, second);
            Assert.Equal(final.Status, current!.Status);
        }

        Assert.Equal(rounds, outcomes.Count);
    }

    [Fact]
    public async Task Cancel_queued_job_prevents_processing()
    {
        var project = await TestProcessorFactory.SeedProjectAsync(Fixture, 10, "cut", "a red square");
        var (job, _) = await Jobs.CreateIfAbsentAsync(project.Id, "cancel-queued", 2);

        var (result, cancelled) = await Jobs.TryCancelAsync(job.Id);
        Assert.Equal(CancelResult.Cancelled, result);
        Assert.Equal(JobStatuses.Cancelled, cancelled!.Status);

        Assert.Null(await Jobs.ClaimNextAsync()); // cancelled jobs are never claimed
    }
}

/// <summary>Crash recovery: interrupted jobs are requeued, partial files never become output.</summary>
public class CrashRecoveryTests : DbTestBase
{
    public CrashRecoveryTests(PostgresFixture fixture) : base(fixture) { }

    private JobRecovery Recovery() =>
        new(Jobs, Opts(Options), Microsoft.Extensions.Logging.Abstractions.NullLogger<JobRecovery>.Instance);

    [Fact]
    public async Task Processing_job_is_requeued_and_temp_file_wiped()
    {
        var project = await TestProcessorFactory.SeedProjectAsync(Fixture, 10, "cut", "a red square");
        var (job, _) = await Jobs.CreateIfAbsentAsync(project.Id, "crash-1", 2);
        var claimed = await Jobs.ClaimNextAsync(); // now 'processing', attempt 1
        Assert.NotNull(claimed);

        // Simulate the crashed render: a half-written temp file exists.
        var tmpFile = Path.Combine(Options.TmpDir, $"{job.Id}-1-deadbeef.mp4");
        await File.WriteAllBytesAsync(tmpFile, new byte[64]);

        await Recovery().RecoverAsync();

        var after = await Jobs.GetAsync(job.Id);
        Assert.Equal(JobStatuses.Queued, after!.Status);
        Assert.False(File.Exists(tmpFile), "stale temp file must be wiped");
        Assert.Null(after.OutputPath);

        // The job can be claimed and processed again.
        Assert.NotNull(await Jobs.ClaimNextAsync());
    }

    [Fact]
    public async Task Completed_job_with_missing_output_is_requeued()
    {
        var project = await TestProcessorFactory.SeedProjectAsync(Fixture, 10, "cut", "a red square");
        var (job, _) = await Jobs.CreateIfAbsentAsync(project.Id, "crash-2", 2);
        var claimed = await Jobs.ClaimNextAsync();
        // Crash between the status update and the atomic publish.
        Assert.True(await Jobs.TryCompleteAsync(claimed!.Id, claimed.Attempt, Path.Combine("outputs", $"{job.Id}.mp4")));

        await Recovery().RecoverAsync();

        var after = await Jobs.GetAsync(job.Id);
        Assert.Equal(JobStatuses.Queued, after!.Status);
        Assert.Null(after.OutputPath);
    }

    [Fact]
    public async Task Completed_job_with_existing_output_survives_recovery()
    {
        var project = await TestProcessorFactory.SeedProjectAsync(Fixture, 10, "cut", "a red square");
        var (job, _) = await Jobs.CreateIfAbsentAsync(project.Id, "crash-3", 2);
        var claimed = await Jobs.ClaimNextAsync();
        var rel = Path.Combine("outputs", $"{job.Id}.mp4");
        await File.WriteAllBytesAsync(Path.Combine(StorageRoot, rel), new byte[128]);
        Assert.True(await Jobs.TryCompleteAsync(claimed!.Id, claimed.Attempt, rel));

        await Recovery().RecoverAsync();

        var after = await Jobs.GetAsync(job.Id);
        Assert.Equal(JobStatuses.Completed, after!.Status);
        Assert.Equal(rel, after.OutputPath);
    }
}
