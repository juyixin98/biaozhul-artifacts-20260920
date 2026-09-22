using Npgsql;

namespace VideoForge.Api.Data;

public sealed class Db
{
    private readonly string _connectionString;

    public Db(IConfiguration configuration)
    {
        _connectionString = configuration.GetConnectionString("Postgres")
            ?? "Host=localhost;Port=5432;Username=videoforge;Password=videoforge;Database=videoforge";
    }

    public NpgsqlConnection Open()
    {
        var conn = new NpgsqlConnection(_connectionString);
        conn.Open();
        return conn;
    }

    public async Task<NpgsqlConnection> OpenAsync(CancellationToken ct = default)
    {
        var conn = new NpgsqlConnection(_connectionString);
        await conn.OpenAsync(ct);
        return conn;
    }
}

public static class Schema
{
    public const string Ddl = """
        CREATE TABLE IF NOT EXISTS assets (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            tags TEXT[] NOT NULL DEFAULT '{}',
            stored_path TEXT NOT NULL,
            original_file_name TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size_bytes BIGINT NOT NULL,
            sha256 TEXT NOT NULL,
            duration_seconds DOUBLE PRECISION,
            version INT NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS projects (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            target_duration_seconds DOUBLE PRECISION NOT NULL
                CHECK (target_duration_seconds BETWEEN 5 AND 60),
            transition TEXT NOT NULL DEFAULT 'cut',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS scenes (
            id BIGSERIAL PRIMARY KEY,
            project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            ord INT NOT NULL,
            description TEXT NOT NULL CHECK (char_length(description) <= 500),
            UNIQUE (project_id, ord)
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id BIGSERIAL PRIMARY KEY,
            project_id BIGINT NOT NULL REFERENCES projects(id),
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'queued',
            attempt INT NOT NULL DEFAULT 0,
            max_attempts INT NOT NULL DEFAULT 2,
            output_path TEXT,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status, id);

        CREATE TABLE IF NOT EXISTS job_attempts (
            id BIGSERIAL PRIMARY KEY,
            job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            attempt_no INT NOT NULL,
            status TEXT NOT NULL DEFAULT 'started',
            log TEXT,
            error TEXT,
            asset_versions JSONB,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            UNIQUE (job_id, attempt_no)
        );
        """;

    public static async Task EnsureCreatedAsync(Db db, CancellationToken ct = default)
    {
        await using var conn = await db.OpenAsync(ct);
        await using var cmd = conn.CreateCommand();
        cmd.CommandText = Ddl;
        await cmd.ExecuteNonQueryAsync(ct);
    }
}
