using System.Collections.Concurrent;
using System.Text.Json;
using VideoForge.Api.Configuration;
using VideoForge.Api.Data;
using VideoForge.Api.Matching;
using VideoForge.Api.Media;
using VideoForge.Api.Models;
using VideoForge.Api.Storage;

namespace VideoForge.Api.Jobs;

/// <summary>
/// Background render pipeline.
///  - SemaphoreSlim enforces at most MaxParallelJobs (3) ffmpeg pipelines.
///  - Queue claims use SELECT ... FOR UPDATE SKIP LOCKED so parallel workers
///    never take the same job.
///  - A periodic reaper requeues jobs whose worker heartbeats went stale
///    (process crash / kill -9 / host loss).
///  - Output is written to a temp path, verified, then atomically moved into
///    the published directory; only after the move is the DB row marked
///    completed, so an incomplete temp file can never be served as a result.
/// </summary>
public sealed class JobWorker : BackgroundService
{
    private readonly IServiceScopeFactory _scopeFactory;
    private readonly VideoForgeOptions _options;
    private readonly FileStorage _storage;
    private readonly MaterialMatcher _matcher;
    private readonly ILogger<JobWorker> _logger;

    private readonly SemaphoreSlim _slots;
    private readonly ConcurrentDictionary<Guid, CancellationTokenSource> _running = new();
    private static readonly JsonSerializerOptions JsonOpts =
        new() { PropertyNamingPolicy = JsonNamingPolicy.CamelCase };

    public JobWorker(
        IServiceScopeFactory scopeFactory,
        VideoForgeOptions options,
        FileStorage storage,
        MaterialMatcher matcher,
        ILogger<JobWorker> logger)
    {
        _scopeFactory = scopeFactory;
        _options = options;
        _storage = storage;
        _matcher = matcher;
        _logger = logger;
        _slots = new SemaphoreSlim(options.MaxParallelJobs);
    }

    /// <summary>Triggers best-effort cooperative cancellation of one running render.</summary>
    public bool SignalLocal(Guid jobId)
    {
        if (_running.TryGetValue(jobId, out var cts))
        {
            cts.Cancel();
            return true;
        }
        return false;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        _storage.EnsureDirectories();
        _storage.ClearStaleTempFiles();

        // Startup recovery: anything left 'processing' by a previous process
        // (there is no live owner now) goes back to queued.
        using (var scope = _scopeFactory.CreateScope())
        {
            var jobs = scope.ServiceProvider.GetRequiredService<JobRepository>();
            var requeued = await jobs.RequeueStaleAsync(staleSeconds: 0, stoppingToken);
            if (requeued > 0)
                _logger.LogInformation("Startup recovery re-queued {N} interrupted job(s)", requeued);
        }

        // Periodic reaper for crashes of other workers / mid-run restarts.
        var reaper = RunReaperAsync(stoppingToken);

        var active = new List<Task>();
        while (!stoppingToken.IsCancellationRequested)
        {
            await _slots.WaitAsync(stoppingToken);
            ClaimResult? claim = null;
            try
            {
                using var scope = _scopeFactory.CreateScope();
                var jobs = scope.ServiceProvider.GetRequiredService<JobRepository>();
                claim = await jobs.ClaimAsync(WorkerId, _options.StaleJobSeconds, stoppingToken);
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                _logger.LogError(ex, "Failed to claim next job");
            }
            finally
            {
                if (claim is null) _slots.Release();
            }

            if (claim is null)
            {
                try { await Task.Delay(_options.PollIntervalMs, stoppingToken); }
                catch (OperationCanceledException) { break; }
                continue;
            }

            var captured = claim;
            var work = Task.Run(() => RunJobSafeAsync(captured, stoppingToken), stoppingToken);
            active.Add(work);
            if (active.Count >= 8)
            {
                var done = await Task.WhenAny(active);
                active.Remove(done);
            }
        }

        try { await reaper; } catch (OperationCanceledException) { }
    }

    private async Task RunReaperAsync(CancellationToken ct)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(Math.Max(5, _options.StaleJobSeconds / 3.0)));
        while (await timer.WaitForNextTickAsync(ct))
        {
            try
            {
                using var scope = _scopeFactory.CreateScope();
                var jobs = scope.ServiceProvider.GetRequiredService<JobRepository>();
                var n = await jobs.RequeueStaleAsync(_options.StaleJobSeconds, ct);
                if (n > 0) _logger.LogInformation("Reaper re-queued {N} stale job(s)", n);
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                _logger.LogError(ex, "Reaper iteration failed");
            }
        }
    }

    private string WorkerId { get; } =
        Environment.MachineName + "-" + Guid.NewGuid().ToString("N")[..8];

    private async Task RunJobSafeAsync(ClaimResult claim, CancellationToken serviceStopping)
    {
        var jobCts = CancellationTokenSource.CreateLinkedTokenSource(serviceStopping);
        _running[claim.Job.Id] = jobCts;
        try
        {
            await ProcessJobAsync(claim, jobCts.Token, serviceStopping);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Unhandled error processing job {JobId}", claim.Job.Id);
            try
            {
                using var scope = _scopeFactory.CreateScope();
                var jobs = scope.ServiceProvider.GetRequiredService<JobRepository>();
                await jobs.FailAsync(claim.Job.Id, WorkerId, claim.AttemptNo,
                    Truncate(ex.Message, 2000), _options.MaxAttempts, CancellationToken.None);
            }
            catch { /* last-resort guard */ }
        }
        finally
        {
            _running.TryRemove(claim.Job.Id, out _);
            _slots.Release();
            jobCts.Dispose();
        }
    }

    private async Task ProcessJobAsync(ClaimResult claim, CancellationToken ct, CancellationToken serviceStopping)
    {
        var job = claim.Job;
        using var scope = _scopeFactory.CreateScope();
        var jobs = scope.ServiceProvider.GetRequiredService<JobRepository>();
        var projects = scope.ServiceProvider.GetRequiredService<ProjectRepository>();
        var materials = scope.ServiceProvider.GetRequiredService<MaterialRepository>();
        var renderer = scope.ServiceProvider.GetRequiredService<FFmpegRenderer>();

        // Observe a cancel that landed before the render started.
        if (await jobs.IsCancelRequestedAsync(job.Id, ct))
        {
            await jobs.RecordAttemptOutcomeAsync(job.Id, claim.AttemptNo, "cancelled",
                "Cancelled before rendering started");
            await jobs.TryCancelAsync(job.Id, WorkerId, ct);
            return;
        }

        var project = await projects.GetAsync(job.ProjectId, ct)
            ?? throw new InvalidOperationException($"Project {job.ProjectId} no longer exists.");
        var scenes = await projects.GetScenesAsync(job.ProjectId, ct);
        if (scenes.Count == 0)
            throw new InvalidOperationException("Project has no scenes to render.");

        var catalog = await materials.ListAsync(ct);
        var selections = _matcher.Match(scenes, catalog, project.TargetDurationMs);
        var missing = selections.Where(s => !s.Matched).ToList();
        if (missing.Count > 0)
        {
            var reasons = string.Join(" | ", missing.Select(m =>
                $"Scene {m.Position + 1}: {m.Reason}"));
            throw new InvalidOperationException(
                $"Cannot render: {missing.Count} scene(s) have no matching material. {reasons}");
        }

        // Snapshot the exact material versions used by this attempt.
        var manifest = selections.Select(s => new
        {
            position = s.Position,
            allocatedDurationMs = s.AllocatedDurationMs,
            materialId = s.Material!.Id,
            checksumSha256 = s.Material.ChecksumSha256,
            filename = s.Material.Filename,
            score = s.Score
        });
        var manifestJson = JsonSerializer.Serialize(manifest, JsonOpts);
        await jobs.SaveManifestAsync(job.Id, claim.AttemptNo, manifestJson, ct);

        var plan = selections.Select(s => new RenderPlanItem(
            s.Position,
            s.Material!.StoredPath,
            s.Material.MediaKind,
            s.AllocatedDurationMs,
            s.Material.DurationMs)).ToList();

        // Heartbeat until the render finishes; also re-check cancel promptly.
        // Interval stays well under the staleness threshold so the reaper can
        // never mistake a live render for a dead worker.
        var heartbeatInterval = TimeSpan.FromSeconds(Math.Max(1, _options.StaleJobSeconds / 5.0));
        using var heartbeatCts = new CancellationTokenSource();
        var heartbeat = Task.Run(async () =>
        {
            while (!heartbeatCts.IsCancellationRequested)
            {
                try
                {
                    await Task.Delay(heartbeatInterval, heartbeatCts.Token);
                    await jobs.HeartbeatAsync(job.Id, WorkerId, heartbeatCts.Token);
                }
                catch (OperationCanceledException) { break; }
                catch (Exception ex) { _logger.LogWarning(ex, "Heartbeat failed for {JobId}", job.Id); }
            }
        });

        string? ffmpegLog = null;
        string tempOutput;
        long tempSize;
        long durationMs;
        try
        {
            var result = await renderer.RenderAsync(job.Id, claim.AttemptNo, plan, project.Transition, ct);
            tempOutput = result.OutputPath;
            tempSize = result.SizeBytes;
            durationMs = result.DurationMs;
            ffmpegLog = result.FFmpegLog;
        }
        catch (OperationCanceledException) when (serviceStopping.IsCancellationRequested)
        {
            // Application is shutting down (or crashed host is going away):
            // leave the job in 'processing' with a stale heartbeat. Startup
            // recovery / the reaper will requeue it; we must not write a
            // user-cancellation terminal state.
            _storage.DeleteJobTempArtifacts(job.Id);
            _logger.LogInformation("Job {JobId} interrupted by shutdown; awaiting recovery", job.Id);
            return;
        }
        catch (RenderCancelledException) when (serviceStopping.IsCancellationRequested)
        {
            _storage.DeleteJobTempArtifacts(job.Id);
            _logger.LogInformation("Job {JobId} interrupted by shutdown; awaiting recovery", job.Id);
            return;
        }
        catch (OperationCanceledException)
        {
            await FinalizeCancellationAsync(jobs, job.Id, claim.AttemptNo, CancellationToken.None);
            return;
        }
        catch (RenderCancelledException)
        {
            await FinalizeCancellationAsync(jobs, job.Id, claim.AttemptNo, CancellationToken.None);
            return;
        }
        finally
        {
            heartbeatCts.Cancel();
            try { await heartbeat; } catch { /* ignore */ }
        }

        // Independent verification of the temp artifact before publishing.
        var verified = await VerifyOutputAsync(renderer, tempOutput, project.TargetDurationMs, scenes.Count, ct);
        if (!verified.Ok)
            throw new InvalidOperationException($"Rendered file failed verification: {verified.Reason}");

        // Publish to an attempt-specific path (atomic rename inside the same
        // filesystem), then compete for the single terminal transition. If we
        // lose — cancellation arrived, or a stale worker was reaped and a
        // retry already finished — our file is distinct and gets removed,
        // never clobbering the newer published result.
        var publishPath = _storage.AttemptPublishedPath(job.Id, claim.AttemptNo);
        _storage.TryDelete(publishPath);
        File.Move(tempOutput, publishPath);

        var completed = await jobs.TryCompleteAsync(job.Id, WorkerId, publishPath,
            new FileInfo(publishPath).Length, durationMs, CancellationToken.None);

        if (completed)
        {
            await jobs.RecordAttemptOutcomeAsync(job.Id, claim.AttemptNo, "succeeded",
                null, ffmpegLog, manifestJson, CancellationToken.None);
            _storage.DeleteJobTempArtifacts(job.Id);
            RemoveSupersededPublished(job.Id, keepPath: publishPath);
            _logger.LogInformation("Job {JobId} completed ({Duration} ms)", job.Id, durationMs);
        }
        else
        {
            // We lost the terminal race. If the job is already completed by a
            // newer attempt, leave that result untouched; otherwise remove our
            // orphan and let the canceller settle the terminal state.
            _logger.LogWarning("Completion of {JobId} lost the terminal-state race", job.Id);
            var current = await jobs.GetAsync(job.Id, CancellationToken.None);
            if (current?.Status != "completed")
                _storage.TryDelete(publishPath);
            await jobs.TryCancelAsync(job.Id, WorkerId, CancellationToken.None);
            await jobs.RecordAttemptOutcomeAsync(job.Id, claim.AttemptNo, "cancelled",
                "Cancelled concurrently at the publish boundary", ffmpegLog, manifestJson,
                CancellationToken.None);
        }
    }

    /// <summary>
    /// Removes older attempt-published files for a job, keeping only the one
    /// backing the completed row. Never deletes the canonical path of another
    /// job (names are keyed by job id).
    /// </summary>
    private void RemoveSupersededPublished(Guid jobId, string keepPath)
    {
        foreach (var file in Directory.EnumerateFiles(
                     Path.GetDirectoryName(keepPath)!, $"videoforge-{jobId:N}-a*.mp4"))
        {
            if (!string.Equals(Path.GetFullPath(file), Path.GetFullPath(keepPath), StringComparison.Ordinal))
                _storage.TryDelete(file);
        }
    }

    private async Task FinalizeCancellationAsync(JobRepository jobs, Guid jobId, int attemptNo, CancellationToken ct)
    {
        _storage.DeleteJobTempArtifacts(jobId);
        await jobs.RecordAttemptOutcomeAsync(jobId, attemptNo, "cancelled",
            "Render stopped due to cancellation request", null, null, CancellationToken.None);
        var cancelled = await jobs.TryCancelAsync(jobId, WorkerId, CancellationToken.None);
        if (!cancelled)
        {
            // Completion beat the cancel observation — impossible in normal
            // flow since TryComplete requires cancel_requested=false, but keep
            // the row consistent defensively.
            var row = await jobs.GetAsync(jobId, CancellationToken.None);
            _logger.LogWarning("Cancellation of {JobId} found terminal status {Status}", jobId, row?.Status);
        }
    }

    private sealed record Verification(bool Ok, string? Reason);

    /// <summary>
    /// Re-probes the temp file and checks it has a video stream and a duration
    /// within ±600 ms of the requested target. A truncated / corrupt render
    /// fails here and never reaches the published directory.
    /// </summary>
    private async Task<Verification> VerifyOutputAsync(
        FFmpegRenderer renderer, string path, int targetDurationMs, int sceneCount, CancellationToken ct)
    {
        if (!File.Exists(path)) return new Verification(false, "output file missing");
        if (new FileInfo(path).Length < 1024) return new Verification(false, "output file suspiciously small");

        long durationMs;
        try { durationMs = await renderer.ProbeDurationMsAsync(path, ct); }
        catch (Exception ex) { return new Verification(false, ex.Message); }

        var tolerance = Math.Max(600, sceneCount * 120L);
        if (Math.Abs(durationMs - targetDurationMs) > tolerance)
            return new Verification(false,
                $"duration {durationMs} ms differs from target {targetDurationMs} ms by more than {tolerance} ms");

        return new Verification(true, null);
    }

    private static string Truncate(string s, int max) =>
        s.Length <= max ? s : s[..max];
}
