using Npgsql;
using VideoForge.Api.Data;
using VideoForge.Api.Models;
using VideoForge.Tests.Infra;
using Xunit;

namespace VideoForge.Tests;

[Collection("postgres")]
public sealed class CancelRaceTests(PostgresFixture fixture)
{
    private readonly string _cs = fixture.MainConnectionString + ";Pooling=false";

    private async Task<(NpgsqlDataSource ds, JobRepository repo, Job job)> SetupJobAsync(string key)
    {
        await TestDb.ResetAsync(_cs);
        var ds = new NpgsqlDataSourceBuilder(_cs).Build();
        var repo = new JobRepository(ds);
        var projectId = Guid.NewGuid();
        await using (var conn = await ds.OpenConnectionAsync())
        await using (var cmd = new NpgsqlCommand(
            "INSERT INTO projects (id, name, target_duration_ms, transition) VALUES ($1,'p',5000,'cut')", conn))
        {
            cmd.Parameters.AddWithValue(projectId);
            await cmd.ExecuteNonQueryAsync();
        }
        var (job, _) = await repo.CreateIfNotExistsAsync(
            new Job { Id = Guid.NewGuid(), ProjectId = projectId, SubmissionKey = key });
        return (ds, repo, job);
    }

    [Fact]
    public async Task Cancelling_queued_job_lands_cancelled_immediately()
    {
        var (_, repo, job) = await SetupJobAsync("cancel-queued");
        var (status, found, changed) = await repo.RequestCancelAsync(job.Id);
        Assert.True(found);
        Assert.True(changed);
        Assert.Equal("cancelled", status);

        // And the worker never picks it up.
        Assert.Null(await repo.ClaimAsync("w", 45));
    }

    [Fact]
    public async Task Cancel_after_claim_but_before_complete_yields_cancelled_only()
    {
        var (_, repo, job) = await SetupJobAsync("cancel-race-1");
        var claim = await repo.ClaimAsync("w1", 45);
        Assert.NotNull(claim);

        await repo.RequestCancelAsync(job.Id);

        var completed = await repo.TryCompleteAsync(job.Id, "w1", "/tmp/x.mp4", 100, 5000);
        var cancelled = await repo.TryCancelAsync(job.Id, "w1");

        Assert.False(completed, "completion must be refused once cancel was requested");
        Assert.True(cancelled);

        var refreshed = await repo.GetAsync(job.Id);
        Assert.Equal("cancelled", refreshed!.Status);
    }

    [Fact]
    public async Task Complete_after_claim_blocks_late_cancel()
    {
        var (_, repo, job) = await SetupJobAsync("cancel-race-2");
        var claim = await repo.ClaimAsync("w1", 45);
        Assert.NotNull(claim);

        var completed = await repo.TryCompleteAsync(job.Id, "w1", "/tmp/x.mp4", 100, 5000);
        Assert.True(completed);

        // A cancel request arriving after completion must not move the state.
        var (_, found, changed) = await repo.RequestCancelAsync(job.Id);
        Assert.True(found);
        Assert.False(changed);
        var cancelled = await repo.TryCancelAsync(job.Id, "w1");
        Assert.False(cancelled);

        Assert.Equal("completed", (await repo.GetAsync(job.Id))!.Status);
    }

    [Fact]
    public async Task Interleaved_complete_and_cancel_always_settles_to_exactly_one_terminal_state()
    {
        // Hammer the boundary: many workers alternate terminal transitions;
        // the row must end in one valid terminal state with one winner.
        var results = new System.Collections.Concurrent.ConcurrentBag<string>();
        for (var iter = 0; iter < 20; iter++)
        {
            await using var ds = new NpgsqlDataSourceBuilder(_cs).Build();
            var repo = new JobRepository(ds);
            var projectId = Guid.NewGuid();
            await using (var conn = await ds.OpenConnectionAsync())
            await using (var cmd = new NpgsqlCommand(
                "INSERT INTO projects (id, name, target_duration_ms, transition) VALUES ($1,'p',5000,'cut')", conn))
            {
                cmd.Parameters.AddWithValue(projectId);
                await cmd.ExecuteNonQueryAsync();
            }
            var (job, _) = await repo.CreateIfNotExistsAsync(
                new Job { Id = Guid.NewGuid(), ProjectId = projectId, SubmissionKey = $"hammer-{iter}" });
            var claim = await repo.ClaimAsync("w", 45);

            var cancelTask = Task.Run(async () =>
            {
                await repo.RequestCancelAsync(job.Id);
                if (await repo.TryCancelAsync(job.Id, "w")) results.Add("cancelled");
            });
            var completeTask = Task.Run(async () =>
            {
                if (await repo.TryCompleteAsync(job.Id, "w", "/tmp/x.mp4", 1, 5000))
                    results.Add("completed");
            });
            await Task.WhenAll(cancelTask, completeTask);

            var final = await repo.GetAsync(job.Id);
            Assert.Contains(final!.Status, new[] { "completed", "cancelled" });
        }

        Assert.Equal(20, results.Count); // exactly one terminal transition per iteration
    }
}
