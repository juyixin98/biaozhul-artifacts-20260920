using System.Text;
using System.Text.Json;
using VideoForge.Api.Models;

namespace VideoForge.Api.Services;

public sealed class NoAssetMatchException : Exception
{
    public NoAssetMatchException(string message) : base(message) { }
}

public sealed record RenderSegment(Asset Asset, double DurationSeconds);

public sealed class RenderPlan
{
    public required Project Project { get; init; }
    public required IReadOnlyList<RenderSegment> Segments { get; init; }
    public required double FadeSeconds { get; init; }
    public double TargetSeconds => Project.TargetDurationSeconds;

    public string AssetVersionsJson => JsonSerializer.Serialize(
        Segments.Select((s, i) => new
        {
            scene = i + 1,
            assetId = s.Asset.Id,
            version = s.Asset.Version,
            sha256 = s.Asset.Sha256,
        }));
}

/// <summary>
/// Matches scene descriptions to local assets by tag keywords.
/// Score = number of asset tags appearing in the description (case-insensitive).
/// Ties break by ascending asset id, so results are deterministic.
/// </summary>
public sealed class AssetMatcher
{
    public const double FadeDurationSeconds = 0.5;

    public RenderPlan BuildPlan(Project project, IReadOnlyList<Asset> assets)
    {
        var scenes = project.Scenes.OrderBy(s => s.Ord).ToList();
        var n = scenes.Count;
        var fade = project.Transition == Transitions.Fade && n > 1 ? FadeDurationSeconds : 0.0;
        // xfade overlaps eat (n-1)*fade of total runtime; stretch each segment to compensate.
        var perScene = (project.TargetDurationSeconds + (n - 1) * fade) / n;

        var segments = new List<RenderSegment>();
        var missing = new List<string>();
        for (var i = 0; i < n; i++)
        {
            var description = scenes[i].Description;
            var best = assets
                .Select(a => (asset: a, score: Score(description, a.Tags)))
                .Where(x => x.score > 0)
                .OrderByDescending(x => x.score)
                .ThenBy(x => x.asset.Id) // fixed deterministic tie-break
                .FirstOrDefault();

            if (best.asset is null)
            {
                missing.Add($"scene {i + 1} (\"{Truncate(description, 60)}\"): no asset tag matched; " +
                            $"scene keywords: [{string.Join(", ", Keywords(description))}], " +
                            $"available tags: [{string.Join(", ", assets.SelectMany(a => a.Tags).Distinct().OrderBy(t => t))}]");
                continue;
            }
            segments.Add(new RenderSegment(best.asset, perScene));
        }

        if (missing.Count > 0)
            throw new NoAssetMatchException(string.Join("; ", missing));

        return new RenderPlan { Project = project, Segments = segments, FadeSeconds = fade };
    }

    public static int Score(string description, IEnumerable<string> tags) =>
        tags.Count(t => !string.IsNullOrWhiteSpace(t) &&
                        description.Contains(t.Trim(), StringComparison.OrdinalIgnoreCase));

    public static IReadOnlyList<string> Keywords(string description)
    {
        var tokens = new List<string>();
        var current = new StringBuilder();
        foreach (var c in description)
        {
            if (char.IsLetterOrDigit(c)) current.Append(c);
            else if (current.Length > 0) { tokens.Add(current.ToString()); current.Clear(); }
        }
        if (current.Length > 0) tokens.Add(current.ToString());
        return tokens.Distinct(StringComparer.OrdinalIgnoreCase).Take(10).ToList();
    }

    private static string Truncate(string s, int max) => s.Length <= max ? s : s[..max] + "…";
}
