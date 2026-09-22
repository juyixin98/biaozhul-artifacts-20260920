using VideoForge.Api.Contracts;

namespace VideoForge.Api.Validation;

public sealed record ValidatedScene(int Position, string Description, string[] Keywords);

public sealed record ValidatedProject(
    string Name, int TargetDurationMs, string Transition, List<ValidatedScene> Scenes);

public static class RequestValidator
{
    public const int MinDurationMs = 5_000;
    public const int MaxDurationMs = 60_000;
    public const int MaxScenes = 20;
    public const int MaxDescriptionLength = 500;
    public const int MaxKeywordLength = 64;
    public const int MaxNameLength = 200;

    /// <summary>
    /// Returns (project, null) or (null, human-readable error with field).
    /// </summary>
    public static (ValidatedProject? Value, string? Error) ValidateProject(CreateProjectRequest? req)
    {
        if (req is null)
            return (null, "Request body is required.");

        var name = req.Name?.Trim().Trim('\0') ?? "";
        if (name.Length == 0) return (null, "name is required.");
        if (name.Length > MaxNameLength) return (null, $"name must be at most {MaxNameLength} characters.");

        if (req.TargetDurationMs is null)
            return (null, "targetDurationMs is required (5000-60000).");
        if (req.TargetDurationMs is < MinDurationMs or > MaxDurationMs)
            return (null, $"targetDurationMs must be between {MinDurationMs} and {MaxDurationMs} ms (5-60 s).");

        var transition = string.IsNullOrWhiteSpace(req.Transition) ? "cut" : req.Transition.Trim().ToLowerInvariant();
        if (transition is not ("cut" or "fade"))
            return (null, "transition must be 'cut' or 'fade'.");

        if (req.Scenes is null || req.Scenes.Count == 0)
            return (null, "scenes must contain at least one scene.");
        if (req.Scenes.Count > MaxScenes)
            return (null, $"a project may contain at most {MaxScenes} scenes.");

        var scenes = new List<ValidatedScene>();
        for (var i = 0; i < req.Scenes.Count; i++)
        {
            var s = req.Scenes[i];
            var desc = s.Description?.Trim() ?? "";
            if (desc.Length == 0)
                return (null, $"scenes[{i}].description is required.");
            if (desc.Length > MaxDescriptionLength)
                return (null, $"scenes[{i}].description must be at most {MaxDescriptionLength} characters.");

            var keywords = (s.Keywords ?? [])
                .Select(k => k.Trim().ToLowerInvariant())
                .Where(k => k.Length > 0)
                .Distinct()
                .ToArray();
            if (keywords.Length == 0)
                return (null, $"scenes[{i}].keywords must contain at least one non-empty keyword.");
            if (keywords.Any(k => k.Length > MaxKeywordLength))
                return (null, $"scenes[{i}].keywords entries must be at most {MaxKeywordLength} characters.");

            scenes.Add(new ValidatedScene(i, desc, keywords));
        }

        return (new ValidatedProject(name, req.TargetDurationMs.Value, transition, scenes), null);
    }

    /// <summary>
    /// Submission keys identify one logical submission for idempotency.
    /// Whitelist charset + length cap prevents injection into logs/paths.
    /// </summary>
    public static (string? Key, string? Error) ValidateSubmissionKey(string? key)
    {
        if (string.IsNullOrWhiteSpace(key))
            return (null, "submissionKey is required and is used to make submissions idempotent.");
        var k = key.Trim();
        if (k.Length > 200)
            return (null, "submissionKey must be at most 200 characters.");
        if (!k.All(c => char.IsLetterOrDigit(c) || c is '-' or '_' or '.' or ':'))
            return (null, "submissionKey may contain only letters, digits and '-', '_', '.', ':'.");
        return (k, null);
    }
}
