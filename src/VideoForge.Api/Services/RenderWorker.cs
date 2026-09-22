using Microsoft.Extensions.Options;
using VideoForge.Api.Data;
using VideoForge.Api.Models;

namespace VideoForge.Api.Services;

/// <summary>
/// Startup recovery: jobs interrupted mid-render are requeued, stale temp files
/// are wiped, and completed jobs whose output never got published are requeued
/// too — a partial file is never treated as a finished video.
/// </summary>
public sealed class JobRecovery
{
    private readonly JobRepository _jobs;
    private readonly VideoForgeOptions _options;
    private readonly ILogger<JobRecovery> _logger;

    public JobRecovery(JobRepository jobs, IOptions<VideoForgeOptions> options, ILogger<JobRecovery> logger)
    {
        _jobs = jobs;
        _options = options.Value;
        _logger = logger;
    }

    public async Task RecoverAsync(CancellationToken ct = default)
    {
        Directory.CreateDirectory(_options.TmpDir);

        var requeued = await _jobs.RequeueInterruptedAsync(ct);
        foreach (var id in requeued)
            _logger.LogWarning("recovery: requeued interrupted job {JobId}", id);

        var missing = await _jobs.RequeueCompletedWithoutFileAsync(
            rel => File.Exists(PathSafety.SafeCombine(_options.StorageRoot, rel)), ct);
        foreach (var id in missing)
            _logger.LogWarning("recovery: completed job {JobId} had no output file; requeued", id);

        // Wipe leftover temp renders — they are never valid outputs.
        foreach (var tmp in Directory.EnumerateFiles(_options.TmpDir, "*.mp4"))
        {
            try { File.Delete(tmp); } catch { /* best effort */ }
        }
    }
}

/// <summary>
/// Background worker pool: up to MaxParallelJobs loops competing for queued jobs
/// via SELECT ... FOR UPDATE SKIP LOCKED.
/// </summary>
public sealed class RenderWorker : BackgroundService
{
    private readonly JobRepository _jobs;
    private readonly JobProcessor _processor;
    private readonly JobRecovery _recovery;
    private readonly VideoForgeOptions _options;
    private readonly ILogger<RenderWorker> _logger;

    public RenderWorker(JobRepository jobs, JobProcessor processor, JobRecovery recovery,
        IOptions<VideoForgeOptions> options, ILogger<RenderWorker> logger)
    {
        _jobs = jobs;
        _processor = processor;
        _recovery = recovery;
        _options = options.Value;
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await _recovery.RecoverAsync(stoppingToken);

        var workers = Enumerable.Range(0, Math.Max(1, _options.MaxParallelJobs))
            .Select(i => LoopAsync(i, stoppingToken));
        await Task.WhenAll(workers);
    }

    private async Task LoopAsync(int workerId, CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            Job? job;
            try
            {
                job = await _jobs.ClaimNextAsync(stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                return;
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "worker {WorkerId}: claim failed", workerId);
                await Task.Delay(1000, stoppingToken);
                continue;
            }

            if (job is null)
            {
                await Task.Delay(500, stoppingToken);
                continue;
            }

            await _processor.ProcessAsync(job, stoppingToken);
        }
    }
}
