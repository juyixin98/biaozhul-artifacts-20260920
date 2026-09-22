namespace VideoForge.Api.Models;

public static class AssetKinds
{
    public const string Image = "image";
    public const string Video = "video";
}

public static class Transitions
{
    public const string Cut = "cut";
    public const string Fade = "fade";

    public static bool IsValid(string? value) =>
        value is Cut or Fade;
}

public static class JobStatuses
{
    public const string Queued = "queued";
    public const string Processing = "processing";
    public const string Completed = "completed";
    public const string Failed = "failed";
    public const string Cancelled = "cancelled";

    public static bool IsTerminal(string status) =>
        status is Completed or Failed or Cancelled;
}

public sealed class Asset
{
    public long Id { get; set; }
    public string Name { get; set; } = "";
    public string Kind { get; set; } = "";
    public string[] Tags { get; set; } = Array.Empty<string>();
    public string StoredPath { get; set; } = "";
    public string OriginalFileName { get; set; } = "";
    public string ContentType { get; set; } = "";
    public long SizeBytes { get; set; }
    public string Sha256 { get; set; } = "";
    public double? DurationSeconds { get; set; }
    public int Version { get; set; }
    public DateTime CreatedAt { get; set; }
}

public sealed class Project
{
    public long Id { get; set; }
    public string Name { get; set; } = "";
    public double TargetDurationSeconds { get; set; }
    public string Transition { get; set; } = Transitions.Cut;
    public DateTime CreatedAt { get; set; }
    public List<Scene> Scenes { get; set; } = new();
}

public sealed class Scene
{
    public long Id { get; set; }
    public long ProjectId { get; set; }
    public int Ord { get; set; }
    public string Description { get; set; } = "";
}

public class Job
{
    public long Id { get; set; }
    public long ProjectId { get; set; }
    public string IdempotencyKey { get; set; } = "";
    public string Status { get; set; } = JobStatuses.Queued;
    public int Attempt { get; set; }
    public int MaxAttempts { get; set; }
    public string? OutputPath { get; set; }
    public string? Error { get; set; }
    public DateTime CreatedAt { get; set; }
    public DateTime UpdatedAt { get; set; }
}

public sealed class JobAttempt
{
    public long Id { get; set; }
    public long JobId { get; set; }
    public int AttemptNo { get; set; }
    public string Status { get; set; } = "started";
    public string? Log { get; set; }
    public string? Error { get; set; }
    public string? AssetVersions { get; set; }
    public DateTime StartedAt { get; set; }
    public DateTime? FinishedAt { get; set; }
}
