using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.Configuration;

namespace VideoForge.Tests;

public sealed class ApiFactory : WebApplicationFactory<Program>
{
    public string StorageRoot { get; } =
        Path.Combine(Path.GetTempPath(), "videoforge-api-tests", Guid.NewGuid().ToString("N"));

    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        builder.ConfigureAppConfiguration((_, config) => config.AddInMemoryCollection(
            new Dictionary<string, string?>
            {
                ["ConnectionStrings:Postgres"] = PostgresFixture.DefaultConnectionString,
                ["VideoForge:StorageRoot"] = StorageRoot,
                ["VideoForge:DisableWorker"] = "true",
                ["VideoForge:MaxAssetBytes"] = "102400", // 100 KB for tests
            }));
    }
}

// Same collection as the other DB tests: xUnit never runs tests of one
// collection in parallel, so the shared test database stays consistent.
[Collection(PostgresCollection.Name)]
public class ApiTests : IClassFixture<ApiFactory>, IAsyncLifetime
{
    private readonly ApiFactory _factory;
    private readonly PostgresFixture _fixture;
    private readonly HttpClient _client;

    public ApiTests(ApiFactory factory, PostgresFixture fixture)
    {
        _factory = factory;
        _fixture = fixture;
        _client = factory.CreateClient();
    }

    public Task InitializeAsync() => _fixture.ResetAsync();

    public Task DisposeAsync() => Task.CompletedTask;

    private static readonly byte[] PngHeader = { 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A };

    private static MultipartFormDataContent AssetUpload(byte[] content, string fileName = "shot.png",
        string name = "shot", string tags = "sunset,sky")
    {
        var form = new MultipartFormDataContent();
        form.Add(new ByteArrayContent(content), "file", fileName);
        form.Add(new StringContent(name), "name");
        form.Add(new StringContent(tags), "tags");
        return form;
    }

    [Fact]
    public async Task Import_valid_png_succeeds()
    {
        var resp = await _client.PostAsync("/api/assets", AssetUpload(PngHeader.Concat(new byte[100]).ToArray()));
        Assert.Equal(HttpStatusCode.Created, resp.StatusCode);
        var json = await resp.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal("image", json.GetProperty("kind").GetString());
        Assert.Equal("image/png", json.GetProperty("contentType").GetString());
    }

    [Fact]
    public async Task Import_spoofed_extension_is_rejected()
    {
        var notAnImage = Encoding.UTF8.GetBytes("plain text renamed to .png");
        var resp = await _client.PostAsync("/api/assets", AssetUpload(notAnImage));
        Assert.Equal(HttpStatusCode.UnprocessableEntity, resp.StatusCode);
    }

    [Fact]
    public async Task Import_oversized_file_is_rejected()
    {
        var big = PngHeader.Concat(new byte[200 * 1024]).ToArray(); // limit is 100 KB in tests
        var resp = await _client.PostAsync("/api/assets", AssetUpload(big));
        Assert.Equal(HttpStatusCode.UnprocessableEntity, resp.StatusCode);
    }

    [Fact]
    public async Task Import_without_tags_is_rejected()
    {
        var resp = await _client.PostAsync("/api/assets",
            AssetUpload(PngHeader.Concat(new byte[10]).ToArray(), tags: ""));
        Assert.Equal(HttpStatusCode.BadRequest, resp.StatusCode);
    }

    [Fact]
    public async Task Traversal_file_name_does_not_escape_storage()
    {
        var resp = await _client.PostAsync("/api/assets",
            AssetUpload(PngHeader.Concat(new byte[10]).ToArray(), fileName: "../../evil.png"));
        Assert.Equal(HttpStatusCode.Created, resp.StatusCode);
        // Stored under a generated name inside the assets dir; nothing written outside.
        Assert.False(File.Exists(Path.Combine(_factory.StorageRoot, "evil.png")));
        Assert.False(Directory.EnumerateFiles(_factory.StorageRoot, "*.png", SearchOption.AllDirectories)
            .Any(p => p.Contains("evil")));
    }

    [Theory]
    [InlineData(3, 2)]   // duration too short
    [InlineData(61, 2)]  // duration too long
    [InlineData(10, 0)]  // no scenes
    [InlineData(10, 21)] // too many scenes
    public async Task Invalid_project_is_rejected(double duration, int sceneCount)
    {
        var resp = await _client.PostAsJsonAsync("/api/projects", new
        {
            name = "bad",
            targetDurationSeconds = duration,
            transition = "cut",
            scenes = Enumerable.Range(1, sceneCount).Select(i => new { description = $"scene {i}" }).ToArray(),
        });
        Assert.Equal(HttpStatusCode.BadRequest, resp.StatusCode);
    }

    [Fact]
    public async Task Description_over_500_chars_is_rejected()
    {
        var resp = await _client.PostAsJsonAsync("/api/projects", new
        {
            name = "bad",
            targetDurationSeconds = 10,
            scenes = new[] { new { description = new string('x', 501) } },
        });
        Assert.Equal(HttpStatusCode.BadRequest, resp.StatusCode);
    }

    [Fact]
    public async Task Full_submit_flow_with_idempotency_and_cancel()
    {
        var projectResp = await _client.PostAsJsonAsync("/api/projects", new
        {
            name = "demo",
            targetDurationSeconds = 10,
            transition = "fade",
            scenes = new[] { new { description = "a sunset" }, new { description = "the ocean" } },
        });
        Assert.Equal(HttpStatusCode.Created, projectResp.StatusCode);
        var project = await projectResp.Content.ReadFromJsonAsync<JsonElement>();
        var projectId = project.GetProperty("id").GetInt64();

        // Submit twice with the same key: one job, first 202 then 200.
        var submit1 = await _client.PostAsJsonAsync($"/api/projects/{projectId}/jobs",
            new { idempotencyKey = "client-key-1" });
        var submit2 = await _client.PostAsJsonAsync($"/api/projects/{projectId}/jobs",
            new { idempotencyKey = "client-key-1" });
        Assert.Equal(HttpStatusCode.Accepted, submit1.StatusCode);
        Assert.Equal(HttpStatusCode.OK, submit2.StatusCode);
        var job1 = await submit1.Content.ReadFromJsonAsync<JsonElement>();
        var job2 = await submit2.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal(job1.GetProperty("id").GetInt64(), job2.GetProperty("id").GetInt64());
        var jobId = job1.GetProperty("id").GetInt64();

        // Download before completion -> 409.
        var early = await _client.GetAsync($"/api/jobs/{jobId}/download");
        Assert.Equal(HttpStatusCode.Conflict, early.StatusCode);

        // Cancel -> 200; cancel again -> 409 (single terminal state).
        var cancel = await _client.PostAsync($"/api/jobs/{jobId}/cancel", null);
        Assert.Equal(HttpStatusCode.OK, cancel.StatusCode);
        var cancelAgain = await _client.PostAsync($"/api/jobs/{jobId}/cancel", null);
        Assert.Equal(HttpStatusCode.Conflict, cancelAgain.StatusCode);

        var status = await _client.GetFromJsonAsync<JsonElement>($"/api/jobs/{jobId}");
        Assert.Equal("cancelled", status.GetProperty("status").GetString());
    }
}
