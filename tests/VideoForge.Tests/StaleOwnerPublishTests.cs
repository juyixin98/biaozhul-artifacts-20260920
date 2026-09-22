using VideoForge.Api.Configuration;
using VideoForge.Api.Data;
using VideoForge.Api.Storage;
using VideoForge.Tests.Infra;
using Xunit;

namespace VideoForge.Tests;

/// <summary>
/// Simulates the rare stale-owner window: worker A's heartbeat goes stale,
/// the job is requeued and claimed by worker B. When A eventually tries to
/// complete, the conditional transition must refuse it, and — because
/// published paths are attempt-specific — A's file cannot replace B's.
/// </summary>
[Collection("postgres")]
public sealed class StaleOwnerPublishTests(PostgresFixture fixture)
{
    private readonly string _cs = fixture.MainConnectionString + ";Pooling=false";

    [Fact]
    public async Task Stale_worker_completion_is_refused_and_cannot_overwrite_newer_published_file()
    {
        await TestDb.ResetAsync(_cs);
        var storage = new FileStorage(new VideoForgeOptions
        {
            DataDirectory = Directory.CreateTempSubdirectory("vf-publish-").FullName
        });
        storage.EnsureDirectories();

        await using var ds = new Npgsql.NpgsqlDataSourceBuilder(_cs).Build();
        var repo = new JobRepository(ds);

        var projectId = Guid.NewGuid();
        await using (var conn = await ds.OpenConnectionAsync())
        await using (var cmd = new Npgsql.NpgsqlCommand(
            "INSERT INTO projects (id, name, target_duration_ms, transition) VALUES ($1,'p',5000,'cut')", conn))
        {
            cmd.Parameters.AddWithValue(projectId);
            await cmd.ExecuteNonQueryAsync();
        }

        var (job, _) = await repo.CreateIfNotExistsAsync(new VideoForge.Api.Models.Job
        {
            Id = Guid.NewGuid(), ProjectId = projectId, SubmissionKey = "stale-publish-1"
        });

        // Worker A claims attempt 1.
        var claimA = await repo.ClaimAsync("workerA", 45);
        Assert.NotNull(claimA);
        var pathA = storage.AttemptPublishedPath(job.Id, claimA!.AttemptNo);
        await File.WriteAllTextAsync(pathA, "A's render");

        // A goes silent; reaper requeues; worker B claims attempt 2 and finishes first.
        await repo.RequeueStaleAsync(0);
        var claimB = await repo.ClaimAsync("workerB", 45);
        Assert.Equal(2, claimB!.AttemptNo);
        var pathB = storage.AttemptPublishedPath(job.Id, claimB.AttemptNo);
        await File.WriteAllTextAsync(pathB, "B's render");
        var bWon = await repo.TryCompleteAsync(job.Id, "workerB", pathB, 10, 5000);
        Assert.True(bWon);

        // A finally wakes up and tries to complete its stale claim.
        var aWon = await repo.TryCompleteAsync(job.Id, "workerA", pathA, 9, 4999);
        Assert.False(aWon);

        var final = await repo.GetAsync(job.Id);
        Assert.Equal("completed", final!.Status);
        Assert.Equal(pathB, final.PublishPath);
        Assert.Equal("B's render", await File.ReadAllTextAsync(final.PublishPath!));

        // A's orphan still exists on disk (distinct name) but is not the row's result;
        // cleanup only removes superseded files on the winning path. Verify names differ.
        Assert.NotEqual(pathA, pathB);
    }
}
