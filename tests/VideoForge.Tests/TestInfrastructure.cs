using Dapper;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using VideoForge.Api;
using VideoForge.Api.Data;

namespace VideoForge.Tests;

/// <summary>
/// Shared PostgreSQL fixture. Requires a reachable database (default:
/// videoforge_test on localhost). Override with VIDEOFORGE_TEST_CONN.
/// </summary>
public sealed class PostgresFixture : IAsyncLifetime
{
    public const string DefaultConnectionString =
        "Host=localhost;Port=5432;Username=videoforge;Password=videoforge;Database=videoforge_test";

    public string ConnectionString { get; }
    public Db Db { get; }

    public PostgresFixture()
    {
        Dapper.DefaultTypeMap.MatchNamesWithUnderscores = true;
        ConnectionString = Environment.GetEnvironmentVariable("VIDEOFORGE_TEST_CONN") ?? DefaultConnectionString;
        Db = new Db(new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?> { ["ConnectionStrings:Postgres"] = ConnectionString })
            .Build());
    }

    public async Task InitializeAsync() => await Schema.EnsureCreatedAsync(Db);

    public async Task ResetAsync()
    {
        await using var conn = await Db.OpenAsync();
        await conn.ExecuteAsync("TRUNCATE job_attempts, jobs, scenes, projects, assets RESTART IDENTITY CASCADE");
    }

    public Task DisposeAsync() => Task.CompletedTask;
}

[CollectionDefinition(Name)]
public sealed class PostgresCollection : ICollectionFixture<PostgresFixture>
{
    public const string Name = "postgres";
}

/// <summary>Base class: resets the database and provides a fresh storage dir per test.</summary>
[Collection(PostgresCollection.Name)]
public abstract class DbTestBase : IAsyncLifetime
{
    protected PostgresFixture Fixture { get; }
    protected string StorageRoot { get; }
    protected VideoForgeOptions Options { get; }

    protected DbTestBase(PostgresFixture fixture)
    {
        Fixture = fixture;
        StorageRoot = Path.Combine(Path.GetTempPath(), "videoforge-tests", Guid.NewGuid().ToString("N"));
        Options = new VideoForgeOptions
        {
            StorageRoot = StorageRoot,
            MaxParallelJobs = 3,
            MaxAttempts = 2,
            MaxAssetBytes = 1024 * 1024,
        };
        Directory.CreateDirectory(Options.AssetsDir);
        Directory.CreateDirectory(Options.OutputsDir);
        Directory.CreateDirectory(Options.TmpDir);
    }

    protected AssetRepository Assets => new(Fixture.Db);
    protected ProjectRepository Projects => new(Fixture.Db);
    protected JobRepository Jobs => new(Fixture.Db);

    public async Task InitializeAsync() => await Fixture.ResetAsync();

    public async Task DisposeAsync()
    {
        try { Directory.Delete(StorageRoot, recursive: true); } catch { /* best effort */ }
        await Task.CompletedTask;
    }

    protected static IOptions<VideoForgeOptions> Opts(VideoForgeOptions o) => Microsoft.Extensions.Options.Options.Create(o);
}
