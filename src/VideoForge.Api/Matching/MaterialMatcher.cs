using VideoForge.Api.Models;

namespace VideoForge.Api.Matching;

/// <summary>A scene's chosen material and the deterministic reason behind it.</summary>
public sealed record SceneSelection(
    int Position,
    string Description,
    string[] Keywords,
    long AllocatedDurationMs,
    Material? Material,
    int? Score,
    string? Reason)
{
    public bool Matched => Material is not null;
}

/// <summary>
/// Keyword -> local-tag matching. Score = number of distinct scene keywords
/// that occur in a material's tags. Ties are broken by a completely fixed
/// order (earliest created_at, then id) so the same inputs always produce
/// the same selection.
/// </summary>
public sealed class MaterialMatcher
{
    public List<SceneSelection> Match(IEnumerable<Scene> scenes, IReadOnlyList<Material> catalog,
        int targetDurationMs)
    {
        var sceneList = scenes.OrderBy(s => s.Position).ToList();
        var selections = new List<SceneSelection>();

        foreach (var scene in sceneList)
        {
            var keywords = NormalizeKeywords(scene.Keywords);
            var allocations = AllocateDurations(sceneList.Count, targetDurationMs);
            var alloc = allocations[scene.Position];

            if (keywords.Length == 0)
            {
                selections.Add(new SceneSelection(scene.Position, scene.Description, scene.Keywords,
                    alloc, null, null,
                    "Scene has no keywords; at least one tag keyword is required to match a local material."));
                continue;
            }

            var scored = new List<(Material M, int Score)>();
            foreach (var m in catalog)
            {
                var tagSet = new HashSet<string>(
                    m.Tags.Select(t => t.Trim().ToLowerInvariant()), StringComparer.Ordinal);
                var hits = keywords.Count(k => tagSet.Contains(k));
                if (hits > 0) scored.Add((m, hits));
            }

            if (scored.Count == 0)
            {
                selections.Add(new SceneSelection(scene.Position, scene.Description, scene.Keywords,
                    alloc, null, null,
                    $"No local material carries any of the keywords [{string.Join(", ", keywords)}]."));
                continue;
            }

            // Primary: score DESC. Fixed tie-break: created_at ASC, id ASC.
            var best = scored
                .OrderByDescending(x => x.Score)
                .ThenBy(x => x.M.CreatedAt)
                .ThenBy(x => x.M.Id)
                .First();

            // Report the score gap context so callers can see why ties resolved this way.
            selections.Add(new SceneSelection(scene.Position, scene.Description, scene.Keywords,
                alloc, best.M, best.Score, null));
        }

        return selections;
    }

    /// <summary>
    /// Evenly splits the target duration across scenes, distributing the
    /// remainder onto the earliest scenes. Sum always equals target.
    /// </summary>
    public static long[] AllocateDurations(int sceneCount, int targetDurationMs)
    {
        if (sceneCount <= 0) return [];
        var baseDur = targetDurationMs / sceneCount;
        var remainder = targetDurationMs - baseDur * sceneCount;
        var result = new long[sceneCount];
        for (var i = 0; i < sceneCount; i++)
            result[i] = baseDur + (i < remainder ? 1 : 0);
        return result;
    }

    private static string[] NormalizeKeywords(IEnumerable<string> kws) =>
        kws.Select(k => k.Trim().ToLowerInvariant())
           .Where(k => k.Length > 0)
           .Distinct()
           .ToArray();
}
