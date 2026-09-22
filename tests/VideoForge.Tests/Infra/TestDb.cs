using Dapper;
using Npgsql;
using VideoForge.Api.Data;

namespace VideoForge.Tests.Infra;

/// <summary>Helpers shared by repository/integration tests.</summary>
public static class TestDb
{
    public static NpgsqlDataSource DataSource(string connectionString) =>
        new NpgsqlDataSourceBuilder(connectionString).Build();

    public static async Task<Database> InitializeAsync(string connectionString)
    {
        Dapper.DefaultTypeMap.MatchNamesWithUnderscores = true;
        var ds = DataSource(connectionString);
        var db = new Database(ds);
        await db.InitializeAsync();
        return db;
    }

    /// <summary>
    /// Tests share one database (they run serialized inside one xUnit
    /// collection), so each test starts from an empty data set.
    /// </summary>
    public static async Task ResetAsync(string connectionString)
    {
        await InitializeAsync(connectionString);
        await using var ds = DataSource(connectionString);
        await using var conn = await ds.OpenConnectionAsync();
        await using var cmd = new NpgsqlCommand("""
            TRUNCATE TABLE job_attempts, jobs, scenes, projects, materials
            RESTART IDENTITY CASCADE
            """, conn);
        await cmd.ExecuteNonQueryAsync();
    }
}
