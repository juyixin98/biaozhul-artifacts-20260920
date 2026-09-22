using System.Text;
using Microsoft.Extensions.Options;
using VideoForge.Api.Data;
using VideoForge.Api.Models;

namespace VideoForge.Api.Services;

/// <summary>
/// Executes one claimed job: match assets, render to a temp file, validate the
/// result, then publish atomically. A job is only marked completed when the
/// conditional UPDATE wins, so a concurrent cancel can never be overwritten —
/// and a published output is never touched by retries (the job is terminal then).
/// </summary>
public sealed class JobProcessor
{
    private readonly JobRepository _jobs;
    private readonly ProjectRepository _projects;
    private readonly AssetRepository _assets;
    private readonly AssetMatcher _matcher;
    private readonly IVideoRenderer _renderer;
    private readonly JobCancellationRegistry _cancellation;
    private readonly VideoForgeOptions _options;
    private readonly ILogger<JobProcessor> _logger;

    public JobProcessor(
        JobRepository jobs, ProjectRepository projects, AssetRepository assets,
        AssetMatcher matcher, IVideoRenderer renderer, JobCancellationRegistry cancellation,
        IOptions<VideoForgeOptions> options, ILogger<JobProcessor> logger)
    {
        _jobs = jobs;
        _projects = projects;
        _assets = assets;
        _matcher = matcher;
        _renderer = renderer;
        _cancellation = cancellation;
        _options = options.Value;
        _logger = logger;
    }

    public async Task ProcessAsync(Job job, CancellationToken stoppingToken = default)
    {
        var attempt = job.Attempt;
        long attemptId = 0;
        var log = new StringBuilder();
        string? tmpFile = null;
        try
        {
            attemptId = await _jobs.InsertAttemptAsync(job.Id, attempt, stoppingToken);
            var ct = _cancellation.Register(job.Id);
            using var linked = CancellationTokenSource.CreateLinkedTokenSource(ct, stoppingToken);
            ct = linked.Token;

            var project = await _projects.GetAsync(job.ProjectId, ct)
                ?? throw new InvalidOperationException($"project {job.ProjectId} not found");
            var assets = await _assets.ListAsync(ct);
            var plan = _matcher.BuildPlan(project, assets);
            await _jobs.SetAttemptAssetsAsync(attemptId, plan.AssetVersionsJson, ct);

            var inputPaths = plan.Segments
                .Select(s => PathSafety.SafeCombine(_options.StorageRoot, s.Asset.StoredPath))
                .ToList();
            var missingFile = inputPaths.FirstOrDefault(p => !File.Exists(p));
            if (missingFile is not null)
                throw new InvalidOperationException($"asset file missing on disk: {missingFile}");

            tmpFile = Path.Combine(_options.TmpDir, $"{job.Id}-{attempt}-{Guid.NewGuid():N}.mp4");
            var result = await _renderer.RenderAsync(plan, inputPaths, tmpFile, log, ct);
            if (!result.Success)
                throw new InvalidOperationException(result.Error ?? "render failed");

            var actual = await _renderer.ProbeDurationSecondsAsync(tmpFile, ct);
            if (actual is null)
                throw new InvalidOperationException("rendered file failed ffprobe validation");
            if (Math.Abs(actual.Value - plan.TargetSeconds) > _options.DurationToleranceSeconds)
                throw new InvalidOperationException(
                    $"duration check failed: expected {plan.TargetSeconds:F2}s, got {actual.Value:F2}s");

            ct.ThrowIfCancellationRequested();

            var outputRel = Path.Combine("outputs", $"{job.Id}.mp4");
            if (await _jobs.TryCompleteAsync(job.Id, attempt, outputRel, CancellationToken.None))
            {
                // Atomic publish: same-filesystem rename, only after validation passed.
                File.Move(tmpFile, PathSafety.SafeCombine(_options.StorageRoot, outputRel));
                tmpFile = null;
                await _jobs.FinishAttemptAsync(attemptId, "succeeded", log.ToString(), null);
                _logger.LogInformation("job {JobId} completed on attempt {Attempt}", job.Id, attempt);
            }
            else
            {
                // Cancelled (or recovered) concurrently — the other terminal state won.
                await _jobs.FinishAttemptAsync(attemptId, "cancelled", log.ToString(),
                    "job left processing state before publish");
            }
        }
        catch (OperationCanceledException)
        {
            if (attemptId != 0)
                await _jobs.FinishAttemptAsync(attemptId, "cancelled", log.ToString(), "cancelled");
            // If the process is merely stopping (not a user cancel), the job stays
            // 'processing' and startup recovery will requeue it.
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "job {JobId} attempt {Attempt} failed", job.Id, attempt);
            var newStatus = await _jobs.FailOrRequeueAsync(job.Id, attempt, ex.Message);
            if (attemptId != 0)
                await _jobs.FinishAttemptAsync(attemptId, "failed", log.ToString(), ex.Message);
            _logger.LogInformation("job {JobId} attempt {Attempt} -> {Status}", job.Id, attempt, newStatus ?? "unchanged (cancelled concurrently)");
        }
        finally
        {
            _cancellation.Unregister(job.Id);
            if (tmpFile is not null)
            {
                try { File.Delete(tmpFile); } catch { /* best effort */ }
            }
        }
    }
}
