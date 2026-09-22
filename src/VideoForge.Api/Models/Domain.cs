namespace VideoForge.Api.Models;

public enum MediaKind
{
    Image,
    Video
}

public enum TransitionKind
{
    Cut,
    Fade
}

public enum JobStatus
{
    Queued,
    Processing,
    Completed,
    Failed,
    Cancelled
}

public sealed class Material
{
    public Guid Id { get; set; }
    public string Filename { get; set; } = "";
    public string StoredPath { get; set; } = "";
    public string ContentType { get; set; } = "";
    public string MediaKind { get; set; } = "image";
    public long SizeBytes { get; set; }
    public int? Width { get; set; }
    public int? Height { get; set; }
    public long? DurationMs { get; set; }
    public string ChecksumSha256 { get; set; } = "";
    public string[] Tags { get; set; } = [];
    public DateTime CreatedAt { get; set; }
}

public sealed class Scene
{
    public Guid ProjectId { get; set; }
    public int Position { get; set; }
    public string Description { get; set; } = "";
    public string[] Keywords { get; set; } = [];
}

public sealed class Project
{
    public Guid Id { get; set; }
    public string Name { get; set; } = "";
    public int TargetDurationMs { get; set; }
    public string Transition { get; set; } = "cut";
    public DateTime CreatedAt { get; set; }
}

public sealed class Job
{
    public Guid Id { get; set; }
    public Guid ProjectId { get; set; }
    public string SubmissionKey { get; set; } = "";
    public string Status { get; set; } = "queued";
    public int Priority { get; set; }
    public bool CancelRequested { get; set; }
    public string? LockedBy { get; set; }
    public DateTime? LockedAt { get; set; }
    public DateTime? HeartbeatAt { get; set; }
    public DateTime? StartedAt { get; set; }
    public DateTime? FinishedAt { get; set; }
    public string? PublishPath { get; set; }
    public long? OutputSize { get; set; }
    public long? OutputDurationMs { get; set; }
    public string? Error { get; set; }
    public DateTime CreatedAt { get; set; }
}

public sealed class JobAttempt
{
    public long Id { get; set; }
    public Guid JobId { get; set; }
    public int AttemptNo { get; set; }
    public string WorkerId { get; set; } = "";
    public DateTime StartedAt { get; set; }
    public DateTime? FinishedAt { get; set; }
    public string? Outcome { get; set; }
    public string? Error { get; set; }
    public string? FfmpegLog { get; set; }
    public string MaterialManifest { get; set; } = "{}";
}
