using DotNet.Testcontainers.Builders;
using Testcontainers.PostgreSql;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.Configuration;
using Npgsql;

namespace VideoForge.Tests.Infra;

/// <summary>
/// Boots one PostgreSQL 16 container for the whole test run. Two databases
/// are created: the main one (background worker disabled) and a dedicated
/// E2E database used by the single test that runs the real render worker,
/// so its worker can never steal queue rows created by other tests.
/// </summary>
public sealed class PostgresFixture : IAsyncLifetime
{
    private readonly PostgreSqlContainer _container =
        new PostgreSqlBuilder()
            .WithImage("postgres:16-alpine")
            .WithDatabase("videoforge_test")
            .WithUsername("videoforge")
            .WithPassword("videoforge")
            .WithCleanUp(true)
            .Build();

    public string MainConnectionString { get; private set; } = "";
    public string E2eConnectionString { get; private set; } = "";

    public async Task InitializeAsync()
    {
        await _container.StartAsync();
        MainConnectionString = _container.GetConnectionString();

        // Create the second database for worker-enabled tests.
        var e2eCs = new NpgsqlConnectionStringBuilder(MainConnectionString) { Database = "videoforge_e2e" };
        E2eConnectionString = e2eCs.ConnectionString;
        await using var admin = new NpgsqlConnection(
            new NpgsqlConnectionStringBuilder(MainConnectionString) { Database = "postgres" }.ConnectionString);
        await admin.OpenAsync();
        await using var cmd = new NpgsqlCommand(
            "SELECT 1 FROM pg_database WHERE datname='videoforge_e2e'", admin);
        if (await cmd.ExecuteScalarAsync() is null)
        {
            await using var create = new NpgsqlCommand("CREATE DATABASE videoforge_e2e", admin);
            await create.ExecuteNonQueryAsync();
        }
    }

    public Task DisposeAsync() => _container.DisposeAsync().AsTask();
}

[CollectionDefinition("postgres")]
public sealed class PostgresCollection : ICollectionFixture<PostgresFixture>;

/// <summary>Test host. workerEnabled=false for all but the end-to-end test.</summary>
public sealed class VideoForgeFactory : WebApplicationFactory<Program>
{
    private readonly string _connectionString;
    private readonly string _dataDir;
    private readonly bool _workerEnabled;
    private readonly long? _maxMaterialBytes;

    public VideoForgeFactory(string connectionString, bool workerEnabled = false, long? maxMaterialBytes = null)
    {
        _connectionString = connectionString;
        _workerEnabled = workerEnabled;
        _maxMaterialBytes = maxMaterialBytes;
        _dataDir = Directory.CreateTempSubdirectory("videoforge-").FullName;
    }

    public string DataDir => _dataDir;

    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        builder.UseSetting("ConnectionStrings:Postgres", _connectionString);
        builder.UseSetting("VideoForge:DataDirectory", _dataDir);
        builder.UseSetting("VideoForge:WorkerEnabled", _workerEnabled ? "true" : "false");
        builder.UseSetting("VideoForge:PollIntervalMs", "100");
        builder.UseSetting("VideoForge:StaleJobSeconds", "5");
        if (_maxMaterialBytes is { } max)
            builder.UseSetting("VideoForge:MaxMaterialBytes", max.ToString());
        builder.ConfigureAppConfiguration((_, _) => { });
    }
}
