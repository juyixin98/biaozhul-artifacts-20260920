using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using VideoForge.Tests.Infra;
using Xunit;

namespace VideoForge.Tests;

[Collection("postgres")]
public sealed class MaterialApiTests(PostgresFixture fixture) : IAsyncLifetime
{
    private readonly VideoForgeFactory _factory = new(fixture.MainConnectionString, maxMaterialBytes: 50 * 1024 * 1024);
    private HttpClient Client => _factory.CreateClient();

    public Task InitializeAsync() => TestDb.ResetAsync(fixture.MainConnectionString);
    public Task DisposeAsync() => Task.CompletedTask;

    private static MultipartFormDataContent Upload(byte[] bytes, string fileName, string tags = "ocean",
        string contentType = "application/octet-stream")
    {
        var form = new MultipartFormDataContent();
        var file = new ByteArrayContent(bytes);
        file.Headers.ContentType = new MediaTypeHeaderValue(contentType);
        form.Add(file, "file", fileName);
        form.Add(new StringContent(tags), "tags");
        return form;
    }

    [Fact]
    public async Task Accepts_real_png_and_detects_kind()
    {
        var dir = Path.Combine(_factory.DataDir, "samples");
        var png = SampleMedia.EnsurePng(dir, "beach.png", "blue");
        var resp = await Client.PostAsync("/api/materials", Upload(await File.ReadAllBytesAsync(png), "beach.png"));
        Assert.Equal(HttpStatusCode.Created, resp.StatusCode);
        var body = await resp.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal("image", body.GetProperty("mediaKind").GetString());
        Assert.Equal("beach.png", body.GetProperty("filename").GetString());
        Assert.Contains("ocean", body.GetProperty("tags").EnumerateArray().Select(t => t.GetString()));
    }

    [Fact]
    public async Task Accepts_real_mp4_video()
    {
        var dir = Path.Combine(_factory.DataDir, "samples");
        var mp4 = SampleMedia.EnsureMp4(dir, "wave.mp4", "cyan", 1);
        var resp = await Client.PostAsync("/api/materials",
            Upload(await File.ReadAllBytesAsync(mp4), "wave.mp4", "wave,video"));
        Assert.Equal(HttpStatusCode.Created, resp.StatusCode);
        var body = await resp.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal("video", body.GetProperty("mediaKind").GetString());
    }

    [Fact]
    public async Task Rejects_exe_renamed_to_png_by_real_content_check()
    {
        var bytes = System.Text.Encoding.ASCII.GetBytes("MZ" + new string('a', 100));
        var resp = await Client.PostAsync("/api/materials", Upload(bytes, "evil.png", "ocean", "image/png"));
        Assert.Equal(HttpStatusCode.UnprocessableEntity, resp.StatusCode);
    }

    [Fact]
    public async Task Rejects_text_file_with_image_extension()
    {
        var bytes = System.Text.Encoding.UTF8.GetBytes("not really an image");
        var resp = await Client.PostAsync("/api/materials", Upload(bytes, "fake.jpg", "ocean", "image/jpeg"));
        Assert.Equal(HttpStatusCode.UnprocessableEntity, resp.StatusCode);
    }

    [Fact]
    public async Task Path_traversal_filename_is_stripped_to_basename()
    {
        var dir = Path.Combine(_factory.DataDir, "samples");
        var png = SampleMedia.EnsurePng(dir, "ok.png", "red");
        var form = new MultipartFormDataContent();
        var file = new ByteArrayContent(await File.ReadAllBytesAsync(png));
        file.Headers.ContentType = new MediaTypeHeaderValue("image/png");
        form.Add(file, "file", "../../../../etc/cron.d/evil.png");
        form.Add(new StringContent("safe"), "tags");

        var resp = await Client.PostAsync("/api/materials", form);
        Assert.Equal(HttpStatusCode.Created, resp.StatusCode);
        var body = await resp.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal("evil.png", body.GetProperty("filename").GetString());
        Assert.False(File.Exists("/etc/cron.d/evil.png"));
        Assert.Single(Directory.GetFiles(Path.Combine(_factory.DataDir, "assets")));
    }

    [Fact]
    public async Task Rejects_upload_without_tags()
    {
        var bytes = SampleMedia.MinimalPng();
        var resp = await Client.PostAsync("/api/materials", Upload(bytes, "x.png", tags: ""));
        Assert.Equal(HttpStatusCode.BadRequest, resp.StatusCode);
    }

    [Fact]
    public async Task Rejects_upload_over_50mb()
    {
        // Use a 1-byte factory cap instead of pushing 50MB over the loop.
        await using var tinyFactory = new VideoForgeFactory(fixture.MainConnectionString, maxMaterialBytes: 10);
        var client = tinyFactory.CreateClient();
        var resp = await client.PostAsync("/api/materials",
            Upload(new byte[1024], "x.png", tags: "ocean", contentType: "image/png"));
        Assert.Equal(HttpStatusCode.RequestEntityTooLarge, resp.StatusCode);

        // And nothing was left staged on disk.
        Assert.Empty(Directory.GetFiles(Path.Combine(tinyFactory.DataDir, "tmp"), "incoming-*"));
    }
}
