using Npgsql;
using VideoForge.Api.Data;
using VideoForge.Api.Models;
using VideoForge.Tests.Infra;
using Xunit;

namespace VideoForge.Tests;

[Collection("postgres")]
public sealed class QueueContentionTests(PostgresFixture fixture)
{
    private readonly string _cs = fixture.MainConnectionString + ";Pooling=false";

    private async Task<(NpgsqlDataSource ds, JobRepository repo, Guid projectId)> NewContextAsync()
    {
        await TestDb.ResetAsync(_cs); // idempotent
        var ds = new NpgsqlDataSourceBuilder(_cs).Build();
        var projectId = await SeedProjectAsync(ds);
        return (ds, new JobRepository(ds), projectId);
    }

    private static async Task<Guid> SeedProjectAsync(NpgsqlDataSource ds)
    {
        var id = Guid.NewGuid();
        await using var conn = await ds.OpenConnectionAsync();
        await using var cmd = new NpgsqlCommand("""
            INSERT INTO projects (id, name, target_duration_ms, transition)
            VALUES ($1, 'p', 5000, 'cut')
            """, conn);
        cmd.Parameters.AddWithValue(id);
        await cmd.ExecuteNonQueryAsync();
        return id;
    }

    [Fact]
    public async Task Parallel_claims_each_take_a_distinct_job()
    {
        var (ds, repo, projectId) = await NewContextAsync();
        const int n = 10;
        for (var i = 0; i < n; i++)
        {
            await repo.CreateIfNotExistsAsync(new Job
            {
                Id = Guid.NewGuid(),
                ProjectId = projectId,
                SubmissionKey = $"contention-{Guid.NewGuid()}"
            });
        }

        var claimedIds = new System.Collections.Concurrent.ConcurrentBag<Guid>();
        var tasks = Enumerable.Range(0, 8).Select(async worker =>
        {
            await using var wds = new NpgsqlDataSourceBuilder(_cs).Build();
            var wrepo = new JobRepository(wds);
            for (var i = 0; i < 3; i++)
            {
                var claim = await wrepo.ClaimAsync($"w{worker}", 45);
                if (claim is not null) claimedIds.Add(claim.Job.Id);
            }
        });
        await Task.WhenAll(tasks);

        Assert.Equal(n, claimedIds.Count);
        Assert.Equal(n, claimedIds.Distinct().Count()); // no job claimed twice
    }

    [Fact]
    public async Task Claim_priority_then_fifo_order()
    {
        var (ds, repo, projectId) = await NewContextAsync();
        var lowId = Guid.NewGuid();
        var highId = Guid.NewGuid();
        await repo.CreateIfNotExistsAsync(new Job { Id = lowId, ProjectId = projectId, SubmissionKey = "low" });
        await Task.Delay(20);
        await repo.CreateIfNotExistsAsync(new Job { Id = highId, ProjectId = projectId, SubmissionKey = "high", Priority = 5 });

        var first = await repo.ClaimAsync("w", 45);
        var second = await repo.ClaimAsync("w", 45);
        Assert.Equal(highId, first!.Job.Id);
        Assert.Equal(lowId, second!.Job.Id);
    }

    [Fact]
    public async Task Claim_creates_incrementing_attempt_rows()
    {
        var (ds, repo, projectId) = await NewContextAsync();
        var jobId = Guid.NewGuid();
        await repo.CreateIfNotExistsAsync(new Job { Id = jobId, ProjectId = projectId, SubmissionKey = "attempts-x" });

        var first = await repo.ClaimAsync("w1", 45);
        Assert.Equal(1, first!.AttemptNo);

        // Simulate failure -> requeue, then second claim.
        await repo.FailAsync(jobId, "w1", 1, "boom", maxAttempts: 3);
        var second = await repo.ClaimAsync("w2", 45);
        Assert.Equal(2, second!.AttemptNo);
    }
}
