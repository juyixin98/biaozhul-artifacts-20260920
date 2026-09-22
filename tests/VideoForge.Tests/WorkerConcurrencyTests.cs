using System.Diagnostics;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using Npgsql;
using VideoForge.Tests.Infra;
using Xunit;

namespace VideoForge.Tests;

/// <summary>
/// Worker-level concurrency behavior against the real background service:
/// at-most-3 parallelism, live cancel of a running render, and recovery of
/// a job whose worker "died" mid-render (heartbeat frozen in the past).
/// </summary>
[Collection("postgres")]
public sealed class WorkerConcurrencyTests(PostgresFixture fixture) : IAsyncLifetime
{
    private VideoForgeFactory _factory = null!;
    private HttpClient _client = null!;
    private string _cs = null!;

    public async Task InitializeAsync()
    {
        await TestDb.ResetAsync(fixture.E2eConnectionString);
        _factory = new VideoForgeFactory(fixture.E2eConnectionString, workerEnabled: true);
        _client = _factory.CreateClient();
        _cs = fixture.E2eConnectionString;
    }

    public async Task DisposeAsync() => await _factory.DisposeAsync();

    private async Task<Guid> ImportStillAsync(string tag, string color)
    {
        var dir = Path.Combine(_factory.DataDir, "samples");
        var png = SampleMedia.EnsurePng(dir, $"{tag}-{color}.png", color);
        var form = new MultipartFormDataContent();
        var file = new ByteArrayContent(await File.ReadAllBytesAsync(png));
        file.Headers.ContentType = new MediaTypeHeaderValue("image/png");
        form.Add(file, "file", $"{tag}.png");
        form.Add(new StringContent(tag), "tags");
        var resp = await _client.PostAsync("/api/materials", form);
        resp.EnsureSuccessStatusCode();
        var body = await resp.Content.ReadFromJsonAsync<JsonElement>();
        return body.GetProperty("id").GetGuid();
    }

    private async Task<Guid> CreateProjectAsync(string tag, int targetMs, string name)
    {
        var resp = await _client.PostAsJsonAsync("/api/projects", new
        {
            name,
            targetDurationMs = targetMs,
            transition = "cut",
            scenes = new[] { new { description = name, keywords = new[] { tag } } }
        });
        resp.EnsureSuccessStatusCode();
        return (await resp.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("id").GetGuid();
    }

    private async Task<JsonElement> SubmitAndWaitStateAsync(Guid projectId, string key,
        string[] anyOf, TimeSpan timeout)
    {
        var submit = await _client.PostAsJsonAsync($"/api/projects/{projectId}/jobs",
            new { submissionKey = key });
        var job = await submit.Content.ReadFromJsonAsync<JsonElement>();
        var id = job.GetProperty("id").GetGuid();
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            var current = await (await _client.GetAsync($"/api/jobs/{id}"))
                .Content.ReadFromJsonAsync<JsonElement>();
            if (anyOf.Contains(current.GetProperty("status").GetString())) return current;
            await Task.Delay(100);
        }
        throw new TimeoutException($"job {id} did not reach {string.Join("/", anyOf)}");
    }

    [Fact]
    public async Task At_most_three_jobs_render_concurrently()
    {
        // Submit 5 jobs over 5 unique tags; observe no more than 3 processing at once.
        var tags = new[] { "wcolor1", "wcolor2", "wcolor3", "wcolor4", "wcolor5" };
        var colors = new[] { "red", "green", "blue", "yellow", "purple" };
        var jobIds = new List<Guid>();
        for (var i = 0; i < 5; i++)
        {
            await ImportStillAsync(tags[i], colors[i]);
            var pid = await CreateProjectAsync(tags[i], 5000, $"job-{i}");
            var resp = await _client.PostAsJsonAsync($"/api/projects/{pid}/jobs",
                new { submissionKey = $"parallel-{i}" });
            var body = await resp.Content.ReadFromJsonAsync<JsonElement>();
            jobIds.Add(body.GetProperty("id").GetGuid());
        }

        var maxSeen = 0;
        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(20);
        var states = new Dictionary<Guid, string>();
        while (DateTime.UtcNow < deadline)
        {
            var processing = 0;
            foreach (var id in jobIds)
            {
                var body = await (await _client.GetAsync($"/api/jobs/{id}"))
                    .Content.ReadFromJsonAsync<JsonElement>();
                var s = body.GetProperty("status").GetString()!;
                states[id] = s;
                if (s == "processing") processing++;
            }
            maxSeen = Math.Max(maxSeen, processing);
            if (states.Values.All(s => s is "completed" or "failed" or "cancelled")) break;
            await Task.Delay(40);
        }

        Assert.True(maxSeen <= 3, $"observed {maxSeen} simultaneous processing jobs");
        Assert.Equal(5, states.Count(kv => kv.Value == "completed"));
    }

    [Fact]
    public async Task Cancelling_a_running_job_lands_cancelled_and_no_file_is_published()
    {
        await ImportStillAsync("cancelme", "navy");
        var pid = await CreateProjectAsync("cancelme", 5000, "to-cancel");
        var submit = await _client.PostAsJsonAsync($"/api/projects/{pid}/jobs",
            new { submissionKey = "cancel-live-1" });
        var jobId = (await submit.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("id").GetGuid();

        // Wait until it is actually processing (ffmpeg running), then cancel.
        await WaitForStatusAsync(jobId, "processing", TimeSpan.FromSeconds(10));
        var cancelResp = await _client.PostAsync($"/api/jobs/{jobId}/cancel", null);
        Assert.Equal(HttpStatusCode.Accepted, cancelResp.StatusCode);

        var final = await WaitForStatusAsync(jobId, "cancelled", TimeSpan.FromSeconds(20));
        Assert.True(final.GetProperty("cancelRequested").GetBoolean());

        var download = await _client.GetAsync($"/api/jobs/{jobId}/download");
        Assert.Equal(HttpStatusCode.Conflict, download.StatusCode);
        Assert.Empty(Directory.GetFiles(Path.Combine(_factory.DataDir, "published"),
            $"videoforge-{jobId:N}*.mp4"));
    }

    [Fact]
    public async Task Job_interrupted_by_a_worker_death_is_reclaimed_on_restart_and_completes()
    {
        await ImportStillAsync("recoverlive", "teal");
        var pid = await CreateProjectAsync("recoverlive", 5000, "recover-live");

        // Stop the whole host so its in-flight claim is frozen at 'processing'.
        var submit = await _client.PostAsJsonAsync($"/api/projects/{pid}/jobs",
            new { submissionKey = "crash-restart-1" });
        var jobId = (await submit.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("id").GetGuid();
        await WaitForStatusAsync(jobId, "processing", TimeSpan.FromSeconds(10));

        // Simulate kill -9 of the whole process: the row is left 'processing'
        // with no heartbeat owner, and the in-flight attempt is still open
        // (outcome NULL) — exactly the on-disk state after a hard crash. The
        // live in-process render is discarded together with its host.
        await using (var ds = new NpgsqlDataSourceBuilder(_cs).Build())
        await using (var conn = await ds.OpenConnectionAsync())
        {
            await using var c1 = new NpgsqlCommand(
                "UPDATE jobs SET status='processing', heartbeat_at = NULL, locked_by = 'crashed-worker', cancel_requested = false WHERE id = $1", conn);
            c1.Parameters.AddWithValue(jobId);
            await c1.ExecuteNonQueryAsync();
            await using var c2 = new NpgsqlCommand(
                "UPDATE job_attempts SET outcome = NULL, finished_at = NULL, error = NULL WHERE job_id = $1 AND outcome IS NOT NULL", conn);
            c2.Parameters.AddWithValue(jobId);
            await c2.ExecuteNonQueryAsync();
        }
        await _factory.DisposeAsync();

        // New instance starts: startup recovery requeues, the worker re-renders.
        _factory = new VideoForgeFactory(fixture.E2eConnectionString, workerEnabled: true);
        _client = _factory.CreateClient();

        var terminal = await WaitForAnyTerminalAsync(jobId, TimeSpan.FromSeconds(30));
        Assert.Equal("completed", terminal.GetProperty("status").GetString());

        // First attempt is recorded as interrupted; second succeeded.
        var attempts = await (await _client.GetAsync($"/api/jobs/{jobId}/attempts"))
            .Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal("interrupted", attempts[0].GetProperty("outcome").GetString());
        Assert.Equal("succeeded", attempts[attempts.GetArrayLength() - 1].GetProperty("outcome").GetString());

        var download = await _client.GetAsync($"/api/jobs/{jobId}/download");
        Assert.Equal(HttpStatusCode.OK, download.StatusCode);
    }

    private async Task<JsonElement> WaitForStatusAsync(Guid id, string status, TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            var body = await (await _client.GetAsync($"/api/jobs/{id}"))
                .Content.ReadFromJsonAsync<JsonElement>();
            if (body.GetProperty("status").GetString() == status) return body;
            await Task.Delay(80);
        }
        throw new TimeoutException($"job {id} never reached {status}");
    }

    private async Task<JsonElement> WaitForAnyTerminalAsync(Guid id, TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        JsonElement body = default;
        while (DateTime.UtcNow < deadline)
        {
            body = await (await _client.GetAsync($"/api/jobs/{id}"))
                .Content.ReadFromJsonAsync<JsonElement>();
            var s = body.GetProperty("status").GetString();
            if (s is "completed" or "failed" or "cancelled") return body;
            await Task.Delay(100);
        }
        throw new TimeoutException(
            $"job {id} stuck in {body.GetProperty("status")} error={body.GetProperty("error").GetString()}");
    }
}
