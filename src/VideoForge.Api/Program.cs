using Microsoft.AspNetCore.Http.Features;
using Npgsql;
using VideoForge.Api.Configuration;
using VideoForge.Api.Data;
using VideoForge.Api.Endpoints;
using VideoForge.Api.Jobs;
using VideoForge.Api.Matching;
using VideoForge.Api.Media;
using VideoForge.Api.Storage;

var builder = WebApplication.CreateBuilder(args);

// PostgreSQL convention is snake_case; map to PascalCase properties automatically.
Dapper.DefaultTypeMap.MatchNamesWithUnderscores = true;

// ---------- Configuration ----------
var options = builder.Configuration
    .GetSection(VideoForgeOptions.SectionName)
    .Get<VideoForgeOptions>() ?? new VideoForgeOptions();

// Connection string comes from the environment / appsettings; never hardcoded.
var connectionString = builder.Configuration.GetConnectionString("Postgres")
                       ?? Environment.GetEnvironmentVariable("CONNECTIONSTRINGS__POSTGRES")
                       ?? "Host=localhost;Port=5432;Database=videoforge;Username=videoforge;Password=videoforge";

builder.Services.AddSingleton(options);
builder.Services.AddSingleton<FileStorage>();
builder.Services.AddSingleton<MaterialMatcher>();
builder.Services.AddSingleton<MediaInspector>();
builder.Services.AddSingleton<FFmpegRenderer>();

var dataSourceBuilder = new NpgsqlDataSourceBuilder(connectionString);
var dataSource = dataSourceBuilder.Build();
builder.Services.AddSingleton(dataSource);
builder.Services.AddSingleton<Database>();
builder.Services.AddScoped<MaterialRepository>();
builder.Services.AddScoped<ProjectRepository>();
builder.Services.AddScoped<JobRepository>();

if (builder.Configuration.GetValue("VideoForge:WorkerEnabled", true))
{
    builder.Services.AddHostedService<JobWorker>();
    builder.Services.AddSingleton(sp => (JobWorker)sp.GetServices<IHostedService>().First(s => s is JobWorker));
}
else
{
    // Endpoints may still ask for the signaler; an unscheduled worker is harmless.
    builder.Services.AddSingleton(sp => new JobWorker(
        sp.GetRequiredService<IServiceScopeFactory>(),
        sp.GetRequiredService<VideoForgeOptions>(),
        sp.GetRequiredService<FileStorage>(),
        sp.GetRequiredService<MaterialMatcher>(),
        sp.GetRequiredService<ILogger<JobWorker>>()));
}

builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen();

// Allow uploads slightly above the material cap (multipart overhead).
builder.Services.Configure<FormOptions>(o =>
{
    o.MultipartBodyLengthLimit = options.MaxMaterialBytes + 1024 * 1024;
});
builder.WebHost.ConfigureKestrel(k =>
{
    k.Limits.MaxRequestBodySize = options.MaxMaterialBytes + 1024 * 1024;
    k.Limits.KeepAliveTimeout = TimeSpan.FromMinutes(2);
});

builder.Services.AddProblemDetails();

var app = builder.Build();

// ---------- Startup ----------
await WaitForDatabaseAsync(dataSource, app.Logger);
var db = app.Services.GetRequiredService<Database>();
await db.InitializeAsync();

app.UseSwagger();
app.UseSwaggerUI();
app.MapVideoForge();

app.Run();

static async Task WaitForDatabaseAsync(NpgsqlDataSource ds, ILogger logger)
{
    for (var attempt = 1; ; attempt++)
    {
        try
        {
            await using var conn = await ds.OpenConnectionAsync();
            await using var cmd = new NpgsqlCommand("SELECT 1", conn);
            await cmd.ExecuteScalarAsync();
            return;
        }
        catch (Exception ex) when (attempt < 30)
        {
            logger.LogWarning("Waiting for PostgreSQL ({Attempt}/30): {Msg}", attempt, ex.Message);
            await Task.Delay(2000);
        }
    }
}

/// <summary>Exposed for WebApplicationFactory-based integration tests.</summary>
public partial class Program;
