using System.Net;
using System.Net.Http.Json;
using VideoForge.Tests.Infra;
using Xunit;

namespace VideoForge.Tests;

[Collection("postgres")]
public sealed class ProjectApiTests(PostgresFixture fixture) : IAsyncLifetime
{
    private readonly VideoForgeFactory _factory = new(fixture.MainConnectionString);
    private HttpClient Client => _factory.CreateClient();

    public Task InitializeAsync() => TestDb.ResetAsync(fixture.MainConnectionString);
    public Task DisposeAsync() => Task.CompletedTask;

    private static object ValidBody(string name = "demo") => new
    {
        name,
        targetDurationMs = 10_000,
        transition = "cut",
        scenes = new[]
        {
            new { description = "Ocean opening shot", keywords = new[] { "ocean" } },
            new { description = "Sunset closing shot", keywords = new[] { "sunset" } }
        }
    };

    [Fact]
    public async Task Creates_project_and_normalizes_keywords_to_lowercase()
    {
        var resp = await Client.PostAsJsonAsync("/api/projects", ValidBody());
        Assert.Equal(HttpStatusCode.Created, resp.StatusCode);
        var body = await resp.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal(10000, body.GetProperty("targetDurationMs").GetInt32());
        var kw = body.GetProperty("scenes")[0].GetProperty("keywords")[0].GetString();
        Assert.Equal("ocean", kw);
    }

    [Theory]
    [InlineData(4999)]
    [InlineData(60001)]
    public async Task Rejects_bad_durations(int ms)
    {
        var body = new
        {
            name = "x", targetDurationMs = ms, transition = "cut",
            scenes = new[] { new { description = "d", keywords = new[] { "ocean" } } }
        };
        Assert.Equal(HttpStatusCode.BadRequest,
            (await Client.PostAsJsonAsync("/api/projects", body)).StatusCode);
    }

    [Fact]
    public async Task Match_report_explains_missing_materials_concretely()
    {
        var created = await Client.PostAsJsonAsync("/api/projects", ValidBody());
        var project = await created.Content.ReadFromJsonAsync<JsonElement>();
        var id = project.GetProperty("id").GetGuid();

        var reportResp = await Client.GetAsync($"/api/projects/{id}/match-report");
        var report = await reportResp.Content.ReadFromJsonAsync<JsonElement>();
        Assert.False(report.GetProperty("ready").GetBoolean());
        var reasons = report.GetProperty("scenes").EnumerateArray()
            .Select(s => s.GetProperty("reason").GetString()).ToList();
        Assert.All(reasons, Assert.NotNull);
        Assert.Contains(reasons, r => r!.Contains("ocean", StringComparison.OrdinalIgnoreCase));
    }

    [Fact]
    public async Task Submitting_with_matching_missing_fails_422_with_per_scene_reasons()
    {
        var created = await Client.PostAsJsonAsync("/api/projects", ValidBody());
        var project = await created.Content.ReadFromJsonAsync<JsonElement>();
        var id = project.GetProperty("id").GetGuid();

        var resp = await Client.PostAsJsonAsync($"/api/projects/{id}/jobs",
            new { submissionKey = "k-001", priority = 0 });
        Assert.Equal(HttpStatusCode.UnprocessableEntity, resp.StatusCode);
        var body = await resp.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal("materials_missing", body.GetProperty("code").GetString());
        Assert.Equal(2, body.GetProperty("details").GetArrayLength());
    }
}
