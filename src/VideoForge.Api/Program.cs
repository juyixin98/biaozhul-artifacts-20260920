using Microsoft.AspNetCore.Http.Features;
using Microsoft.Extensions.Options;
using VideoForge.Api;
using VideoForge.Api.Data;
using VideoForge.Api.Services;

Dapper.DefaultTypeMap.MatchNamesWithUnderscores = true;

var builder = WebApplication.CreateBuilder(args);

builder.Services.Configure<VideoForgeOptions>(builder.Configuration.GetSection(VideoForgeOptions.SectionName));
var options = builder.Configuration.GetSection(VideoForgeOptions.SectionName).Get<VideoForgeOptions>()
              ?? new VideoForgeOptions();

builder.Services.Configure<FormOptions>(o => o.MultipartBodyLengthLimit = options.MaxAssetBytes + 1024 * 1024);
builder.WebHost.ConfigureKestrel(k => k.Limits.MaxRequestBodySize = options.MaxAssetBytes + 1024 * 1024);

builder.Services.AddControllers();
builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen();

builder.Services.AddSingleton<Db>();
builder.Services.AddSingleton<AssetRepository>();
builder.Services.AddSingleton<ProjectRepository>();
builder.Services.AddSingleton<JobRepository>();
builder.Services.AddSingleton<AssetMatcher>();
builder.Services.AddSingleton<JobCancellationRegistry>();
builder.Services.AddSingleton<IVideoRenderer, FFmpegRenderer>();
builder.Services.AddSingleton<JobProcessor>();
builder.Services.AddSingleton<JobRecovery>();
if (!options.DisableWorker)
    builder.Services.AddHostedService<RenderWorker>();

var app = builder.Build();

// Wait for the database (compose startup, cold postgres) before migrating.
for (var attempt = 1; ; attempt++)
{
    try
    {
        await Schema.EnsureCreatedAsync(app.Services.GetRequiredService<Db>());
        break;
    }
    catch (Exception) when (attempt < 15)
    {
        app.Logger.LogWarning("database not ready (attempt {Attempt}/15), retrying in 2s", attempt);
        await Task.Delay(2000);
    }
}
Directory.CreateDirectory(options.AssetsDir);
Directory.CreateDirectory(options.OutputsDir);
Directory.CreateDirectory(options.TmpDir);

if (app.Environment.IsDevelopment())
{
    app.UseSwagger();
    app.UseSwaggerUI();
}

app.MapControllers();
app.MapGet("/healthz", () => Results.Ok(new { status = "ok" }));

app.Run();

public partial class Program { }
