using System.Text.Json;
using VideoForge.Api.Contracts;
using VideoForge.Api.Data;
using VideoForge.Api.Jobs;
using VideoForge.Api.Matching;
using VideoForge.Api.Media;
using VideoForge.Api.Models;
using VideoForge.Api.Storage;
using VideoForge.Api.Validation;

namespace VideoForge.Api.Endpoints;

public static class ApiEndpoints
{
    public static void MapVideoForge(this IEndpointRouteBuilder app)
    {
        var api = app.MapGroup("/api");

        // ---------- Materials ----------
        api.MapPost("/materials", ImportMaterial)
           .WithName("ImportMaterial").DisableAntiforgery();
        api.MapGet("/materials", ListMaterials).WithName("ListMaterials");
        api.MapGet("/materials/{id:guid}", GetMaterial).WithName("GetMaterial");

        // ---------- Projects ----------
        api.MapPost("/projects", CreateProject).WithName("CreateProject");
        api.MapGet("/projects/{id:guid}", GetProject).WithName("GetProject");
        api.MapGet("/projects/{id:guid}/match-report", GetMatchReport).WithName("GetMatchReport");

        // ---------- Jobs ----------
        api.MapPost("/projects/{id:guid}/jobs", SubmitJob).WithName("SubmitJob");
        api.MapGet("/jobs/{id:guid}", GetJob).WithName("GetJob");
        api.MapGet("/jobs/{id:guid}/attempts", GetAttempts).WithName("GetAttempts");
        api.MapPost("/jobs/{id:guid}/cancel", CancelJob).WithName("CancelJob");
        api.MapGet("/jobs/{id:guid}/download", DownloadVideo).WithName("DownloadVideo");

        api.MapGet("/health", () => Results.Ok(new { status = "ok" }));
    }

    private static IResult Error(int status, string code, string message, object? details = null)
        => Results.Json(new ApiError(code, message, details), statusCode: status);

    // ---------------- Materials ----------------

    private static async Task<IResult> ImportMaterial(
        HttpRequest request,
        MaterialRepository repo,
        MediaInspector inspector,
        FileStorage storage,
        CancellationToken ct)
    {
        if (!request.HasFormContentType)
            return Error(400, "invalid_form", "Request must be multipart/form-data.");

        var form = await request.ReadFormAsync(ct);
        if (form.Files.Count != 1)
            return Error(400, "invalid_form", "Exactly one file must be uploaded under the 'file' field.");
        var upload = form.Files[0];
        if (upload.Length == 0)
            return Error(400, "empty_file", "Uploaded file is empty.");

        var rawTags = form["tags"].ToString();
        var tags = ParseTags(rawTags);
        if (tags.Count == 0)
            return Error(400, "invalid_tags",
                "At least one 'tags' value is required (comma-separated form field, e.g. \"ocean,sunset\").");

        // Filename is stored for display only; never used in a filesystem path.
        var displayName = SanitizeDisplayName(upload.FileName);
        if (displayName.Length == 0 || displayName.Length > 255)
            return Error(400, "invalid_filename", "A filename of 1-255 characters is required.");

        var id = Guid.NewGuid();

        string savedPath;
        long length;
        string sha256;
        try
        {
            await using var stream = upload.OpenReadStream();
            (savedPath, length, sha256) =
                await storage.SaveUploadAsync(stream, storage.IncomingUploadPath(id), ct);
        }
        catch (AssetTooLargeException ex)
        {
            return Error(413, "file_too_large", ex.Message, new { limitBytes = ex.LimitBytes });
        }

        MediaProbe probe;
        try
        {
            probe = await inspector.InspectAsync(savedPath, ct);
        }
        catch (InvalidMediaException ex)
        {
            storage.TryDelete(savedPath);
            return Error(422, "invalid_media", ex.Message);
        }
        catch (Exception ex)
        {
            storage.TryDelete(savedPath);
            return Error(500, "probe_failed", $"Media inspection failed: {ex.Message}");
        }

        // Re-store under an extension derived from the verified content type.
        var finalPath = storage.AssetPath(id, probe.Extension);
        File.Move(savedPath, finalPath);

        var material = new Material
        {
            Id = id,
            Filename = displayName,
            StoredPath = finalPath,
            ContentType = upload.ContentType ?? "application/octet-stream",
            MediaKind = probe.Kind,
            SizeBytes = length,
            Width = probe.Width,
            Height = probe.Height,
            DurationMs = probe.DurationMs,
            ChecksumSha256 = sha256,
            Tags = tags.ToArray()
        };
        await repo.InsertAsync(material, ct);

        return Results.Json(ToResponse(material), statusCode: 201);
    }

    private static async Task<IResult> ListMaterials(MaterialRepository repo, CancellationToken ct)
    {
        var rows = await repo.ListAsync(ct);
        return Results.Ok(rows.Select(ToResponse));
    }

    private static async Task<IResult> GetMaterial(Guid id, MaterialRepository repo, CancellationToken ct)
    {
        var m = await repo.GetAsync(id, ct);
        return m is null ? Error(404, "not_found", $"Material {id} not found.") : Results.Ok(ToResponse(m));
    }

    private static MaterialResponse ToResponse(Material m) =>
        new(m.Id, m.Filename, m.MediaKind, m.ContentType, m.SizeBytes, m.Width, m.Height,
            m.DurationMs, m.Tags, m.CreatedAt);

    // ---------------- Projects ----------------

    private static async Task<IResult> CreateProject(
        CreateProjectRequest? body,
        ProjectRepository repo,
        CancellationToken ct)
    {
        var (validated, validationError) = RequestValidator.ValidateProject(body);
        if (validated is null) return Error(400, "validation_failed", validationError!);

        var id = Guid.NewGuid();
        var project = new Project
        {
            Id = id,
            Name = validated.Name,
            TargetDurationMs = validated.TargetDurationMs,
            Transition = validated.Transition
        };
        var scenes = validated.Scenes.Select(s => new Scene
        {
            ProjectId = id,
            Position = s.Position,
            Description = s.Description,
            Keywords = s.Keywords
        }).ToList();

        await repo.CreateAsync(project, scenes, ct);
        return Results.Json(new ProjectResponse(
            project.Id, project.Name, project.TargetDurationMs, project.Transition,
            project.CreatedAt,
            scenes.Select(s => new SceneResponse(s.Position, s.Description, s.Keywords)).ToList()),
            statusCode: 201);
    }

    private static async Task<IResult> GetProject(Guid id, ProjectRepository repo, CancellationToken ct)
    {
        var p = await repo.GetAsync(id, ct);
        if (p is null) return Error(404, "not_found", $"Project {id} not found.");
        var scenes = await repo.GetScenesAsync(id, ct);
        return Results.Ok(new ProjectResponse(p.Id, p.Name, p.TargetDurationMs, p.Transition, p.CreatedAt,
            scenes.Select(s => new SceneResponse(s.Position, s.Description, s.Keywords)).ToList()));
    }

    /// <summary>
    /// Dry-run of matching: shows every scene's chosen material, score and
    /// duration allocation, including the concrete reason for any miss.
    /// </summary>
    private static async Task<IResult> GetMatchReport(
        Guid id,
        ProjectRepository projects,
        MaterialRepository materials,
        MaterialMatcher matcher,
        CancellationToken ct)
    {
        var project = await projects.GetAsync(id, ct);
        if (project is null) return Error(404, "not_found", $"Project {id} not found.");
        var scenes = await projects.GetScenesAsync(id, ct);
        var catalog = await materials.ListAsync(ct);
        var selections = matcher.Match(scenes, catalog, project.TargetDurationMs);

        var dto = selections.Select(s => new SceneMatch(
            s.Position,
            s.Description,
            s.Keywords,
            s.AllocatedDurationMs,
            s.Matched,
            s.Material?.Id,
            s.Material?.Filename,
            s.Score,
            s.Reason)).ToList();

        return Results.Ok(new ProjectMatchReport(
            project.Id, project.Name, project.TargetDurationMs, project.Transition,
            dto.All(s => s.Matched), dto));
    }

    // ---------------- Jobs ----------------

    private static async Task<IResult> SubmitJob(
        Guid id,
        SubmitJobRequest? body,
        ProjectRepository projects,
        MaterialRepository materials,
        JobRepository jobs,
        MaterialMatcher matcher,
        CancellationToken ct)
    {
        var project = await projects.GetAsync(id, ct);
        if (project is null) return Error(404, "not_found", $"Project {id} not found.");

        var (key, keyError) = RequestValidator.ValidateSubmissionKey(body?.SubmissionKey);
        if (key is null) return Error(400, "validation_failed", keyError!);

        // Fail fast with concrete per-scene reasons instead of queueing a doomed job.
        var scenes = await projects.GetScenesAsync(id, ct);
        var catalog = await materials.ListAsync(ct);
        var selections = matcher.Match(scenes, catalog, project.TargetDurationMs);
        var missing = selections.Where(s => !s.Matched).ToList();
        if (missing.Count > 0)
        {
            return Error(422, "materials_missing",
                $"Project cannot be rendered: {missing.Count} scene(s) have no matching material.",
                missing.Select(m => new { scene = m.Position + 1, reason = m.Reason, keywords = m.Keywords }));
        }

        var priority = Math.Clamp(body?.Priority ?? 0, -100, 100);
        var (job, created) = await jobs.CreateIfNotExistsAsync(new Job
        {
            Id = Guid.NewGuid(),
            ProjectId = id,
            SubmissionKey = key,
            Priority = priority
        }, ct);

        var attemptCount = (await jobs.GetAttemptsAsync(job.Id, ct)).Count;
        var response = ToJobResponse(job, attemptCount);
        return Results.Json(response, statusCode: created ? 201 : 200);
    }

    private static async Task<IResult> GetJob(Guid id, JobRepository jobs, CancellationToken ct)
    {
        var job = await jobs.GetAsync(id, ct);
        if (job is null) return Error(404, "not_found", $"Job {id} not found.");
        var attempts = await jobs.GetAttemptsAsync(id, ct);
        return Results.Ok(ToJobResponse(job, attempts.Count));
    }

    private static async Task<IResult> GetAttempts(Guid id, JobRepository jobs, CancellationToken ct)
    {
        var job = await jobs.GetAsync(id, ct);
        if (job is null) return Error(404, "not_found", $"Job {id} not found.");
        var attempts = await jobs.GetAttemptsAsync(id, ct);
        return Results.Ok(attempts.Select(a =>
        {
            JsonElement? manifest = null;
            if (!string.IsNullOrWhiteSpace(a.MaterialManifest))
            {
                try { manifest = JsonDocument.Parse(a.MaterialManifest).RootElement; }
                catch { manifest = JsonDocument.Parse("{}").RootElement; }
            }
            return new AttemptResponse(
                a.AttemptNo, a.WorkerId, a.Outcome, a.Error, a.StartedAt, a.FinishedAt,
                manifest,
                a.FfmpegLog?.Length > 4000 ? a.FfmpegLog[^4000..] : a.FfmpegLog);
        }));
    }

    private static async Task<IResult> CancelJob(
        Guid id,
        JobRepository jobs,
        JobWorker worker,
        CancellationToken ct)
    {
        // Cooperative signal to a render running inside this process, then
        // the durable DB transition. The worker loop polls cancel_requested
        // even if the in-process signal races.
        worker.SignalLocal(id);
        var (status, found, changed) = await jobs.RequestCancelAsync(id, ct);
        if (!found) return Error(404, "not_found", $"Job {id} not found.");
        if (!changed)
            return Error(409, "terminal_state",
                $"Job is already in terminal state '{status}' and cannot be cancelled.");
        return Results.Json(new { jobId = id, status }, statusCode: 202);
    }

    private static async Task<IResult> DownloadVideo(Guid id, JobRepository jobs, FileStorage storage, CancellationToken ct)
    {
        var job = await jobs.GetAsync(id, ct);
        if (job is null) return Error(404, "not_found", $"Job {id} not found.");
        if (job.Status != "completed" || string.IsNullOrEmpty(job.PublishPath))
            return Error(409, "not_ready",
                $"Video is not available for download; job status is '{job.Status}'.",
                new { error = job.Error });

        // The stored path is always server-generated; still verify it resolves
        // inside the published directory before streaming it.
        var path = job.PublishPath!;
        if (!storage.IsWithinPublished(path) || !File.Exists(path))
            return Error(410, "missing", "Completed job has no published file on disk.");

        return Results.File(path, "video/mp4", enableRangeProcessing: true,
            fileDownloadName: $"videoforge-{id:N}.mp4");
    }

    private static JobResponse ToJobResponse(Job j, int attemptCount) =>
        new(j.Id, j.ProjectId, j.SubmissionKey, j.Status, j.CancelRequested,
            attemptCount == 0 ? null : attemptCount,
            j.OutputDurationMs, j.OutputSize, j.Error, j.CreatedAt, j.StartedAt, j.FinishedAt);

    // ---------------- helpers ----------------

    private static List<string> ParseTags(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return [];
        return raw.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                  .Select(t => t.ToLowerInvariant())
                  .Where(t => t.Length is > 0 and <= 64 &&
                              t.All(c => char.IsLetterOrDigit(c) || c is '-' or '_'))
                  .Distinct()
                  .ToList();
    }

    private static string SanitizeDisplayName(string fileName)
    {
        // Strip any directory component (defense-in-depth: display name only).
        var name = Path.GetFileName(fileName.Replace('\\', '/'));
        foreach (var bad in Path.GetInvalidFileNameChars())
            name = name.Replace(bad, '_');
        return name.Trim();
    }
}
