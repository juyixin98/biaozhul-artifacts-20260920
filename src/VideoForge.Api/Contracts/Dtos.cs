using System.Text.Json;

namespace VideoForge.Api.Contracts;

public record SceneInput(string? Description, List<string>? Keywords);

public record CreateProjectRequest(
    string? Name,
    int? TargetDurationMs,
    string? Transition,
    List<SceneInput>? Scenes);

public record SubmitJobRequest(string? SubmissionKey, int? Priority);

public record MaterialResponse(
    Guid Id,
    string Filename,
    string MediaKind,
    string ContentType,
    long SizeBytes,
    int? Width,
    int? Height,
    long? DurationMs,
    string[] Tags,
    DateTime CreatedAt);

public record SceneMatch(
    int Position,
    string Description,
    string[] Keywords,
    long AllocatedDurationMs,
    bool Matched,
    Guid? MaterialId,
    string? MaterialFilename,
    int? Score,
    string? Reason);

public record ProjectMatchReport(
    Guid ProjectId,
    string Name,
    int TargetDurationMs,
    string Transition,
    bool Ready,
    List<SceneMatch> Scenes);

public record ProjectResponse(
    Guid Id,
    string Name,
    int TargetDurationMs,
    string Transition,
    DateTime CreatedAt,
    List<SceneResponse> Scenes);

public record SceneResponse(int Position, string Description, string[] Keywords);

public record JobResponse(
    Guid Id,
    Guid ProjectId,
    string SubmissionKey,
    string Status,
    bool CancelRequested,
    int? AttemptCount,
    long? OutputDurationMs,
    long? OutputSize,
    string? Error,
    DateTime CreatedAt,
    DateTime? StartedAt,
    DateTime? FinishedAt);

public record AttemptResponse(
    int AttemptNo,
    string WorkerId,
    string? Outcome,
    string? Error,
    DateTime StartedAt,
    DateTime? FinishedAt,
    JsonElement? MaterialManifest,
    string? FfmpegLogTail);

public record ApiError(string Code, string Message, object? Details = null);
