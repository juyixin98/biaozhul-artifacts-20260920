using Npgsql;
using VideoForge.Api.Data;
using VideoForge.Api.Models;
using VideoForge.Tests.Infra;
using Xunit;

namespace VideoForge.Tests;

[Collection("postgres")]
public sealed class DuplicateSubmissionTests(PostgresFixture fixture)
{
    private readonly string _cs = fixture.MainConnectionString + ";Pooling=false";

    [Fact]
    public async Task Same_submission_key_never_creates_two_jobs_even_under_racing_requests()
    {
        await TestDb.ResetAsync(_cs);
        var projectId = Guid.NewGuid();
        await using (var ds0 = new NpgsqlDataSourceBuilder(_cs).Build())
        await using (var conn = await ds0.OpenConnectionAsync())
        await using (var cmd = new NpgsqlCommand(
            "INSERT INTO projects (id, name, target_duration_ms, transition) VALUES ($1,'p',5000,'cut')", conn))
        {
            cmd.Parameters.AddWithValue(projectId);
            await cmd.ExecuteNonQueryAsync();
        }

        const string key = "idempotent-key-001";
        var results = new System.Collections.Concurrent.ConcurrentBag<(Guid Id, bool Created)>();

        var tasks = Enumerable.Range(0, 12).Select(_ => Task.Run(async () =>
        {
            await using var ds = new NpgsqlDataSourceBuilder(_cs).Build();
            var repo = new JobRepository(ds);
            var r = await repo.CreateIfNotExistsAsync(new Job
            {
                Id = Guid.NewGuid(), ProjectId = projectId, SubmissionKey = key
            });
            results.Add((r.Job.Id, r.Created));
        }));
        await Task.WhenAll(tasks);

        Assert.Single(results.Select(r => r.Id).Distinct());
        Assert.Single(results.Where(r => r.Created));
        Assert.Equal(12, results.Count);

        await using var verifyDs = new NpgsqlDataSourceBuilder(_cs).Build();
        await using var vconn = await verifyDs.OpenConnectionAsync();
        await using var count = new NpgsqlCommand(
            "SELECT count(*) FROM jobs WHERE submission_key = $1", vconn);
        count.Parameters.AddWithValue(key);
        Assert.Equal(1L, await count.ExecuteScalarAsync());
    }

    [Fact]
    public async Task Replayed_key_returns_original_terminal_job()
    {
        await TestDb.ResetAsync(_cs);
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

        var (first, created1) = await repo.CreateIfNotExistsAsync(
            new Job { Id = Guid.NewGuid(), ProjectId = projectId, SubmissionKey = "replay-me" });
        var (second, created2) = await repo.CreateIfNotExistsAsync(
            new Job { Id = Guid.NewGuid(), ProjectId = projectId, SubmissionKey = "replay-me" });

        Assert.True(created1);
        Assert.False(created2);
        Assert.Equal(first.Id, second.Id);
    }
}
