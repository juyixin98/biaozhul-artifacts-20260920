using Dapper;
using Npgsql;
using VideoForge.Api.Models;

namespace VideoForge.Api.Data;

public sealed class ProjectRepository(NpgsqlDataSource dataSource)
{
    public async Task<Project?> GetAsync(Guid id, CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        return await conn.QuerySingleOrDefaultAsync<Project>(
            "SELECT * FROM projects WHERE id = @id", new { id });
    }

    public async Task<(Project Project, List<Scene> Scenes)> CreateAsync(
        Project project, IReadOnlyList<Scene> scenes, CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        await using var tx = await conn.BeginTransactionAsync(ct);

        await conn.ExecuteAsync("""
            INSERT INTO projects (id, name, target_duration_ms, transition)
            VALUES (@Id, @Name, @TargetDurationMs, @Transition)
            """, project, tx);

        foreach (var s in scenes)
        {
            await using var cmd = new NpgsqlCommand("""
                INSERT INTO scenes (project_id, position, description, keywords)
                VALUES ($1, $2, $3, $4)
                """, (NpgsqlConnection)conn, (NpgsqlTransaction)tx);
            cmd.Parameters.AddWithValue(s.ProjectId);
            cmd.Parameters.AddWithValue(s.Position);
            cmd.Parameters.AddWithValue(s.Description);
            cmd.Parameters.AddWithValue(s.Keywords);
            await cmd.ExecuteNonQueryAsync(ct);
        }

        await tx.CommitAsync(ct);
        return (project, scenes.ToList());
    }

    public async Task<IReadOnlyList<Scene>> GetScenesAsync(Guid projectId, CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        var rows = new List<Scene>();
        await using var cmd = new NpgsqlCommand(
            "SELECT project_id, position, description, keywords FROM scenes WHERE project_id = $1 ORDER BY position",
            conn);
        cmd.Parameters.AddWithValue(projectId);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            rows.Add(new Scene
            {
                ProjectId = reader.GetGuid(0),
                Position = reader.GetInt32(1),
                Description = reader.GetString(2),
                Keywords = reader.GetFieldValue<string[]>(3)
            });
        }
        return rows;
    }
}
