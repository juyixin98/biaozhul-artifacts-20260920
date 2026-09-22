using System.ComponentModel.DataAnnotations;

namespace VideoForge.Api.Models;

public sealed class CreateProjectRequest
{
    [Required, StringLength(200, MinimumLength = 1)]
    public string Name { get; set; } = "";

    /// <summary>Target duration of the final video, 5-60 seconds.</summary>
    [Range(5, 60)]
    public double TargetDurationSeconds { get; set; }

    /// <summary>"cut" (default) or "fade".</summary>
    public string Transition { get; set; } = Transitions.Cut;

    [Required, MinLength(1), MaxLength(20)]
    public List<CreateSceneRequest> Scenes { get; set; } = new();
}

public sealed class CreateSceneRequest
{
    [Required, StringLength(500, MinimumLength = 1)]
    public string Description { get; set; } = "";
}

public sealed class SubmitJobRequest
{
    /// <summary>Client-supplied key; resubmitting the same key returns the existing job.</summary>
    [Required, StringLength(128, MinimumLength = 1)]
    public string IdempotencyKey { get; set; } = "";
}

public sealed record AssetDto(
    long Id, string Name, string Kind, string[] Tags, string ContentType,
    long SizeBytes, string Sha256, double? DurationSeconds, int Version, DateTime CreatedAt);

public sealed record ProjectDto(
    long Id, string Name, double TargetDurationSeconds, string Transition,
    DateTime CreatedAt, IReadOnlyList<SceneDto> Scenes);

public sealed record SceneDto(long Id, int Ord, string Description);

public sealed record JobDto(
    long Id, long ProjectId, string IdempotencyKey, string Status, int Attempt,
    string? Error, DateTime CreatedAt, DateTime UpdatedAt,
    string? DownloadUrl, IReadOnlyList<JobAttemptDto>? Attempts = null);

public sealed record JobAttemptDto(
    int AttemptNo, string Status, string? Error, string? Log,
    string? AssetVersions, DateTime StartedAt, DateTime? FinishedAt);

public static class DtoMapping
{
    public static AssetDto ToDto(this Asset a) =>
        new(a.Id, a.Name, a.Kind, a.Tags, a.ContentType, a.SizeBytes, a.Sha256, a.DurationSeconds, a.Version, a.CreatedAt);

    public static ProjectDto ToDto(this Project p) =>
        new(p.Id, p.Name, p.TargetDurationSeconds, p.Transition, p.CreatedAt,
            p.Scenes.OrderBy(s => s.Ord).Select(s => new SceneDto(s.Id, s.Ord, s.Description)).ToList());

    public static JobDto ToDto(this Job j, IEnumerable<JobAttempt>? attempts = null) =>
        new(j.Id, j.ProjectId, j.IdempotencyKey, j.Status, j.Attempt, j.Error, j.CreatedAt, j.UpdatedAt,
            j.Status == JobStatuses.Completed ? $"/api/jobs/{j.Id}/download" : null,
            attempts?.Select(a => new JobAttemptDto(a.AttemptNo, a.Status, a.Error, a.Log, a.AssetVersions, a.StartedAt, a.FinishedAt)).ToList());
}
