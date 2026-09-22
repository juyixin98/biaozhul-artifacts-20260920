using Dapper;
using Npgsql;
using VideoForge.Api.Models;

namespace VideoForge.Api.Data;

public sealed class MaterialRepository(NpgsqlDataSource dataSource)
{
    public async Task<Material> InsertAsync(Material m, CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand("""
            INSERT INTO materials
                (id, filename, stored_path, content_type, media_kind, size_bytes,
                 width, height, duration_ms, checksum_sha256, tags)
            VALUES
                ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """, conn);
        cmd.Parameters.AddWithValue(m.Id);
        cmd.Parameters.AddWithValue(m.Filename);
        cmd.Parameters.AddWithValue(m.StoredPath);
        cmd.Parameters.AddWithValue(m.ContentType);
        cmd.Parameters.AddWithValue(m.MediaKind);
        cmd.Parameters.AddWithValue(m.SizeBytes);
        cmd.Parameters.AddWithValue(m.Width ?? (object)DBNull.Value);
        cmd.Parameters.AddWithValue(m.Height ?? (object)DBNull.Value);
        cmd.Parameters.AddWithValue(m.DurationMs ?? (object)DBNull.Value);
        cmd.Parameters.AddWithValue(m.ChecksumSha256);
        cmd.Parameters.AddWithValue(m.Tags);
        await cmd.ExecuteNonQueryAsync(ct);
        return m;
    }

    public async Task<Material?> GetAsync(Guid id, CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        return await conn.QuerySingleOrDefaultAsync<Material>(
            "SELECT * FROM materials WHERE id = @id", new { id });
    }

    public async Task<IReadOnlyList<Material>> ListAsync(CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        var rows = await conn.QueryAsync<Material>(
            "SELECT * FROM materials ORDER BY created_at ASC, id ASC");
        return rows.AsList();
    }

    /// <summary>
    /// Fetches materials whose tag array overlaps the keyword set. Scoring is
    /// done in C# (see MaterialMatcher), tags are case-insensitive.
    /// </summary>
    public async Task<IReadOnlyList<Material>> FindByAnyTagAsync(
        IEnumerable<string> keywords, CancellationToken ct = default)
    {
        var list = keywords.Select(k => k.Trim().ToLowerInvariant())
                           .Where(k => k.Length > 0).Distinct().ToArray();
        if (list.Length == 0) return [];

        await using var conn = await dataSource.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "SELECT * FROM materials WHERE tags && $1 ORDER BY created_at ASC, id ASC", conn);
        cmd.Parameters.AddWithValue(list); // maps to text[]
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        return await ReadMaterialsAsync(reader, ct);
    }

    internal static async Task<List<Material>> ReadMaterialsAsync(
        NpgsqlDataReader reader, CancellationToken ct)
    {
        var result = new List<Material>();
        while (await reader.ReadAsync(ct))
        {
            int WidthOrd() => reader.GetOrdinal("width");
            int HeightOrd() => reader.GetOrdinal("height");
            int DurOrd() => reader.GetOrdinal("duration_ms");
            result.Add(new Material
            {
                Id = reader.GetGuid(reader.GetOrdinal("id")),
                Filename = reader.GetString(reader.GetOrdinal("filename")),
                StoredPath = reader.GetString(reader.GetOrdinal("stored_path")),
                ContentType = reader.GetString(reader.GetOrdinal("content_type")),
                MediaKind = reader.GetString(reader.GetOrdinal("media_kind")),
                SizeBytes = reader.GetInt64(reader.GetOrdinal("size_bytes")),
                Width = reader.IsDBNull(WidthOrd()) ? null : reader.GetInt32(WidthOrd()),
                Height = reader.IsDBNull(HeightOrd()) ? null : reader.GetInt32(HeightOrd()),
                DurationMs = reader.IsDBNull(DurOrd()) ? null : reader.GetInt64(DurOrd()),
                ChecksumSha256 = reader.GetString(reader.GetOrdinal("checksum_sha256")),
                Tags = reader.GetFieldValue<string[]>(reader.GetOrdinal("tags")),
                CreatedAt = reader.GetDateTime(reader.GetOrdinal("created_at"))
            });
        }
        return result;
    }
}
