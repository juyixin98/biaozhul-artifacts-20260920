using System.Diagnostics;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using VideoForge.Tests.Infra;
using Xunit;
using Xunit.Abstractions;

namespace VideoForge.Tests;

/// <summary>
/// Full end-to-end run against the real background worker and real ffmpeg:
/// import -> create -> submit -> poll -> download -> ffprobe the MP4 and
/// assert its duration matches the requested target.
/// </summary>
[Collection("postgres")]
public sealed class EndToEndRenderTests(PostgresFixture fixture, ITestOutputHelper output) : IAsyncLifetime
{
    private readonly VideoForgeFactory _factory = new(fixture.E2eConnectionString, workerEnabled: true);
    private HttpClient Client => _factory.CreateClient();

    public async Task InitializeAsync() => await TestDb.ResetAsync(fixture.E2eConnectionString);
    public async Task DisposeAsync() => await _factory.DisposeAsync();

    private static MultipartFormDataContent MaterialUpload(byte[] bytes, string fileName, string tags)
    {
        var form = new MultipartFormDataContent();
        var file = new ByteArrayContent(bytes);
        file.Headers.ContentType = new MediaTypeHeaderValue("image/png");
        form.Add(file, "file", fileName);
        form.Add(new StringContent(tags), "tags");
        return form;
    }

    [Fact]
    public async Task Renders_downloadable_mp4_with_correct_duration_and_idempotent_submit()
    {
        var dir = Path.Combine(_factory.DataDir, "samples");
        var ocean = await File.ReadAllBytesAsync(SampleMedia.EnsurePng(dir, "ocean.png", "blue"));
        var sunset = await File.ReadAllBytesAsync(SampleMedia.EnsurePng(dir, "sunset.png", "orange"));

        var m1 = await PostMaterial(ocean, "ocean.png", "ocean,sea,blue");
        var m2 = await PostMaterial(sunset, "sunset.png", "sunset,sky,red");
        output.WriteLine("materials: {0} {1}", m1, m2);

        const int targetMs = 6000;
        var created = await Client.PostAsJsonAsync("/api/projects", new
        {
            name = "e2e-fade",
            targetDurationMs = targetMs,
            transition = "fade",
            scenes = new[]
            {
                new { description = "Ocean scene", keywords = new[] { "ocean" } },
                new { description = "Sunset scene", keywords = new[] { "sunset" } }
            }
        });
        Assert.Equal(HttpStatusCode.Created, created.StatusCode);
        var project = await created.Content.ReadFromJsonAsync<JsonElement>();
        var projectId = project.GetProperty("id").GetGuid();

        // Duplicate submit with the same key returns the same job.
        var submit1 = await Client.PostAsJsonAsync($"/api/projects/{projectId}/jobs",
            new { submissionKey = "e2e-key-1" });
        Assert.Equal(HttpStatusCode.Created, submit1.StatusCode);
        var submit2 = await Client.PostAsJsonAsync($"/api/projects/{projectId}/jobs",
            new { submissionKey = "e2e-key-1" });
        Assert.Equal(HttpStatusCode.OK, submit2.StatusCode);
        var job1 = await submit1.Content.ReadFromJsonAsync<JsonElement>();
        var job2 = await submit2.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal(job1.GetProperty("id").GetGuid(), job2.GetProperty("id").GetGuid());
        var jobId = job1.GetProperty("id").GetGuid();

        var final = await PollJobUntilTerminalAsync(jobId, TimeSpan.FromSeconds(60));
        output.WriteLine("final job: {0}", final);
        Assert.Equal("completed", final.GetProperty("status").GetString());

        // Attempt history was recorded.
        var attemptsResp = await Client.GetAsync($"/api/jobs/{jobId}/attempts");
        var attempts = await attemptsResp.Content.ReadFromJsonAsync<JsonElement>();
        Assert.True(attempts.GetArrayLength() >= 1);
        var manifest = attempts[0].GetProperty("materialManifest");
        Assert.Equal(JsonValueKind.Array, manifest.ValueKind);
        Assert.Equal(2, manifest.EnumerateArray().Count());
        // Material versions (checksums) are snapshotted per attempt.
        Assert.All(manifest.EnumerateArray(),
            m => Assert.False(string.IsNullOrEmpty(m.GetProperty("checksumSha256").GetString())));

        var download = await Client.GetAsync($"/api/jobs/{jobId}/download");
        Assert.Equal(HttpStatusCode.OK, download.StatusCode);
        Assert.Equal("video/mp4", download.Content.Headers.ContentType!.MediaType);
        var mp4Path = Path.Combine(_factory.DataDir, "result.mp4");
        await File.WriteAllBytesAsync(mp4Path, await download.Content.ReadAsByteArrayAsync());

        var probed = ProbeDurationMs(mp4Path);
        output.WriteLine("requested={0}ms actual={1}ms", targetMs, probed);
        Assert.InRange(probed, targetMs - 700, targetMs + 700);
    }

    [Fact]
    public async Task Corrupt_asset_fails_the_job_with_concrete_errors_on_every_attempt()
    {
        // The asset exists in the catalog but its stored bytes are garbage:
        // every attempt must fail with a concrete ffmpeg error, exhaust the
        // retry budget and settle to 'failed' — a half file must never be
        // published.
        var dir = Path.Combine(_factory.DataDir, "samples");
        var bytes = await File.ReadAllBytesAsync(SampleMedia.EnsurePng(dir, "forest.png", "green"));
        var matId = await PostMaterial(bytes, "forest.png", "forest");

        const int targetMs = 5000;
        var projectId = await CreateProjectAsync(targetMs, "cut", ("Forest shot", new[] { "forest" }));

        // Corrupt the on-disk asset before the job runs (still detected in catalog).
        var assetPath = Directory.GetFiles(Path.Combine(_factory.DataDir, "assets"), matId.ToString("N") + "*").Single();
        await File.WriteAllBytesAsync(assetPath, System.Text.Encoding.UTF8.GetBytes("CORRUPTED"));

        var jobId = await SubmitAsync(projectId, "e2e-bad-asset");
        var failedState = await PollJobUntilTerminalAsync(jobId, TimeSpan.FromSeconds(120));
        Assert.Equal("failed", failedState.GetProperty("status").GetString());
        var err = failedState.GetProperty("error").GetString();
        Assert.False(string.IsNullOrEmpty(err));
        Assert.Contains("ffmpeg", err, StringComparison.OrdinalIgnoreCase);

        // No published artifact exists for a failed job.
        var download = await Client.GetAsync($"/api/jobs/{jobId}/download");
        Assert.Equal(HttpStatusCode.Conflict, download.StatusCode);

        // Every attempt logged its own failure and was bounded by MaxAttempts.
        var attempts = await (await Client.GetAsync($"/api/jobs/{jobId}/attempts"))
            .Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal(3, attempts.GetArrayLength());
        Assert.All(attempts.EnumerateArray(), a => Assert.Equal("failed", a.GetProperty("outcome").GetString()));
    }

    private async Task<Guid> PostMaterial(byte[] bytes, string fileName, string tags)
    {
        var resp = await Client.PostAsync("/api/materials", MaterialUpload(bytes, fileName, tags));
        resp.EnsureSuccessStatusCode();
        var body = await resp.Content.ReadFromJsonAsync<JsonElement>();
        return body.GetProperty("id").GetGuid();
    }

    private async Task<Guid> CreateProjectAsync(int targetMs, string transition,
        params (string Description, string[] Keywords)[] scenes)
    {
        var resp = await Client.PostAsJsonAsync("/api/projects", new
        {
            name = "p",
            targetDurationMs = targetMs,
            transition,
            scenes = scenes.Select(s => new { description = s.Description, keywords = s.Keywords })
        });
        resp.EnsureSuccessStatusCode();
        var body = await resp.Content.ReadFromJsonAsync<JsonElement>();
        return body.GetProperty("id").GetGuid();
    }

    private async Task<Guid> SubmitAsync(Guid projectId, string key)
    {
        var resp = await Client.PostAsJsonAsync($"/api/projects/{projectId}/jobs",
            new { submissionKey = key });
        resp.EnsureSuccessStatusCode();
        var body = await resp.Content.ReadFromJsonAsync<JsonElement>();
        return body.GetProperty("id").GetGuid();
    }

    private async Task<JsonElement> PollJobUntilTerminalAsync(Guid jobId, TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            var resp = await Client.GetAsync($"/api/jobs/{jobId}");
            var body = await resp.Content.ReadFromJsonAsync<JsonElement>();
            var status = body.GetProperty("status").GetString();
            if (status is "completed" or "failed" or "cancelled") return body;
            await Task.Delay(300);
        }
        throw new TimeoutException($"Job {jobId} did not reach a terminal state within {timeout}");
    }

    private static long ProbeDurationMs(string path)
    {
        var psi = new ProcessStartInfo("ffprobe")
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false
        };
        foreach (var a in new[]
                 {
                     "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", path
                 })
            psi.ArgumentList.Add(a);
        using var proc = Process.Start(psi)!;
        var stdout = proc.StandardOutput.ReadToEnd().Trim();
        proc.WaitForExit(10_000);
        return (long)Math.Round(double.Parse(stdout,
            System.Globalization.CultureInfo.InvariantCulture) * 1000);
    }
}
