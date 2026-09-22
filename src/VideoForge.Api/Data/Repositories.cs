using Dapper;
using VideoForge.Api.Models;

namespace VideoForge.Api.Data;

public sealed class AssetRepository
{
    private readonly Db _db;
    public AssetRepository(Db db) => _db = db;

    public async Task<Asset> InsertAsync(Asset asset, CancellationToken ct = default)
    {
        const string sql = """
            INSERT INTO assets (name, kind, tags, stored_path, original_file_name, content_type, size_bytes, sha256, duration_seconds)
            VALUES (@Name, @Kind, @Tags, @StoredPath, @OriginalFileName, @ContentType, @SizeBytes, @Sha256, @DurationSeconds)
            RETURNING *;
            """;
        await using var conn = await _db.OpenAsync(ct);
        return await conn.QuerySingleAsync<Asset>(new CommandDefinition(sql, asset, cancellationToken: ct));
    }

    public async Task<IReadOnlyList<Asset>> ListAsync(CancellationToken ct = default)
    {
        await using var conn = await _db.OpenAsync(ct);
        var rows = await conn.QueryAsync<Asset>(new CommandDefinition("SELECT * FROM assets ORDER BY id", cancellationToken: ct));
        return rows.AsList();
    }

    public async Task<Asset?> GetAsync(long id, CancellationToken ct = default)
    {
        await using var conn = await _db.OpenAsync(ct);
        return await conn.QuerySingleOrDefaultAsync<Asset>(
            new CommandDefinition("SELECT * FROM assets WHERE id = @id", new { id }, cancellationToken: ct));
    }
}

public sealed class ProjectRepository
{
    private readonly Db _db;
    public ProjectRepository(Db db) => _db = db;

    public async Task<Project> InsertAsync(Project project, CancellationToken ct = default)
    {
        await using var conn = await _db.OpenAsync(ct);
        await using var tx = await conn.BeginTransactionAsync(ct);
        var created = await conn.QuerySingleAsync<Project>(new CommandDefinition("""
            INSERT INTO projects (name, target_duration_seconds, transition)
            VALUES (@Name, @TargetDurationSeconds, @Transition)
            RETURNING *;
            """, project, tx, cancellationToken: ct));

        foreach (var scene in project.Scenes)
        {
            scene.ProjectId = created.Id;
            scene.Id = await conn.QuerySingleAsync<long>(new CommandDefinition("""
                INSERT INTO scenes (project_id, ord, description)
                VALUES (@ProjectId, @Ord, @Description)
                RETURNING id;
                """, scene, tx, cancellationToken: ct));
        }
        await tx.CommitAsync(ct);
        created.Scenes = project.Scenes;
        return created;
    }

    public async Task<Project?> GetAsync(long id, CancellationToken ct = default)
    {
        await using var conn = await _db.OpenAsync(ct);
        var project = await conn.QuerySingleOrDefaultAsync<Project>(
            new CommandDefinition("SELECT * FROM projects WHERE id = @id", new { id }, cancellationToken: ct));
        if (project is null) return null;
        var scenes = await conn.QueryAsync<Scene>(new CommandDefinition(
            "SELECT * FROM scenes WHERE project_id = @id ORDER BY ord", new { id }, cancellationToken: ct));
        project.Scenes = scenes.AsList();
        return project;
    }
}
