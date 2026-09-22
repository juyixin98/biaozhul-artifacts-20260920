using Npgsql;
using VideoForge.Api.Data;
using VideoForge.Api.Models;
using VideoForge.Tests.Infra;
using Xunit;

namespace VideoForge.Tests;

[Collection("postgres")]
public sealed class RecoveryTests(PostgresFixture fixture)
{
    private readonly string _cs = fixture.MainConnectionString + ";Pooling=false";

    private async Task<(NpgsqlDataSource ds, JobRepository repo, Job job, int attemptNo)> ClaimedJobAsync(string key)
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
        var claim = await repo.ClaimAsync("dead-worker", 45);
        return (ds, repo, job, claim!.AttemptNo);
    }

    [Fact]
    public async Task Stale_processing_job_is_requeued_and_its_attempt_marked_interrupted()
    {
        var (ds, repo, job, attemptNo) = await ClaimedJobAsync("recover-1");

        // Simulate the worker dying 2 minutes ago (heartbeat frozen in the past).
        await using (var conn = await ds.OpenConnectionAsync())
        await using (var cmd = new NpgsqlCommand(
            "UPDATE jobs SET heartbeat_at = now() - interval '2 minutes' WHERE id = $1", conn))
        {
            cmd.Parameters.AddWithValue(job.Id);
            await cmd.ExecuteNonQueryAsync();
        }

        var requeued = await repo.RequeueStaleAsync(staleSeconds: 45);
        Assert.Equal(1, requeued);

        var row = await repo.GetAsync(job.Id);
        Assert.Equal("queued", row!.Status);
        Assert.Null(row.LockedBy);

        var attempts = await repo.GetAttemptsAsync(job.Id);
        Assert.Equal("interrupted", attempts.Single(a => a.AttemptNo == attemptNo).Outcome);

        // Another worker can now claim it, opening attempt #2.
        var reclaim = await repo.ClaimAsync("new-worker", 45);
        Assert.NotNull(reclaim);
        Assert.Equal(2, reclaim!.AttemptNo);
    }

    [Fact]
    public async Task Fresh_heartbeat_is_not_reclaimed()
    {
        var (_, repo, job, _) = await ClaimedJobAsync("recover-2");
        Assert.Equal(0, await repo.RequeueStaleAsync(staleSeconds: 45));
        Assert.Equal("processing", (await repo.GetAsync(job.Id))!.Status);
    }

    [Fact]
    public async Task Requeued_failed_job_is_retried_until_max_attempts_then_fails_terminally()
    {
        var (_, repo, job, attempt1) = await ClaimedJobAsync("recover-3");

        await repo.FailAsync(job.Id, "dead-worker", attempt1, "ffmpeg crash", maxAttempts: 3);
        Assert.Equal("queued", (await repo.GetAsync(job.Id))!.Status);

        var claim2 = await repo.ClaimAsync("w2", 45);
        await repo.FailAsync(job.Id, "w2", claim2!.AttemptNo, "again", maxAttempts: 3);
        Assert.Equal("queued", (await repo.GetAsync(job.Id))!.Status);

        var claim3 = await repo.ClaimAsync("w3", 45);
        await repo.FailAsync(job.Id, "w3", claim3!.AttemptNo, "final", maxAttempts: 3);

        var terminal = await repo.GetAsync(job.Id);
        Assert.Equal("failed", terminal!.Status);
        Assert.Equal("final", terminal.Error);
    }

    [Fact]
    public async Task Failure_after_cancel_request_settles_cancelled_instead_of_requeue()
    {
        var (_, repo, job, attemptNo) = await ClaimedJobAsync("recover-4");
        await repo.RequestCancelAsync(job.Id);
        await repo.FailAsync(job.Id, "dead-worker", attemptNo, "render aborted", maxAttempts: 3);

        Assert.Equal("cancelled", (await repo.GetAsync(job.Id))!.Status);
    }

    [Fact]
    public async Task Every_attempt_keeps_its_own_error_and_log_history()
    {
        var (_, repo, job, a1) = await ClaimedJobAsync("recover-5");
        await repo.RecordAttemptOutcomeAsync(job.Id, a1, "failed", error: "first error",
            ffmpegLog: "log-one");
        await repo.FailAsync(job.Id, "dead-worker", a1, "first error", maxAttempts: 3);

        var claim2 = await repo.ClaimAsync("w2", 45);
        await repo.RecordAttemptOutcomeAsync(job.Id, claim2!.AttemptNo, "failed", error: "second error",
            ffmpegLog: "log-two");
        await repo.FailAsync(job.Id, "w2", claim2.AttemptNo, "second error", maxAttempts: 3);

        var attempts = (await repo.GetAttemptsAsync(job.Id)).OrderBy(a => a.AttemptNo).ToList();
        Assert.Equal(2, attempts.Count);
        Assert.Equal("first error", attempts[0].Error);
        Assert.Equal("log-one", attempts[0].FfmpegLog);
        Assert.Equal("second error", attempts[1].Error);
        Assert.Equal("log-two", attempts[1].FfmpegLog);
    }
}
