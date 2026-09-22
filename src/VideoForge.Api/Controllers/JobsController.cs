using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;
using VideoForge.Api.Data;
using VideoForge.Api.Models;
using VideoForge.Api.Services;

namespace VideoForge.Api.Controllers;

[ApiController]
[Route("api/jobs")]
public sealed class JobsController : ControllerBase
{
    private readonly JobRepository _jobs;
    private readonly JobCancellationRegistry _cancellation;
    private readonly VideoForgeOptions _options;

    public JobsController(JobRepository jobs, JobCancellationRegistry cancellation,
        IOptions<VideoForgeOptions> options)
    {
        _jobs = jobs;
        _cancellation = cancellation;
        _options = options.Value;
    }

    [HttpGet("{id:long}")]
    public async Task<IActionResult> Get(long id, CancellationToken ct)
    {
        var job = await _jobs.GetAsync(id, ct);
        if (job is null) return NotFound();
        var attempts = await _jobs.ListAttemptsAsync(id, ct);
        return Ok(job.ToDto(attempts));
    }

    /// <summary>
    /// Cancel a queued/processing job. Exactly one terminal state can win:
    /// if the job already completed or failed, 409 is returned with its status.
    /// </summary>
    [HttpPost("{id:long}/cancel")]
    public async Task<IActionResult> Cancel(long id, CancellationToken ct)
    {
        var (result, job) = await _jobs.TryCancelAsync(id, ct);
        switch (result)
        {
            case CancelResult.NotFound:
                return NotFound();
            case CancelResult.AlreadyTerminal:
                return Conflict(new { error = $"job already in terminal state '{job!.Status}'", job.Status });
            default:
                _cancellation.Cancel(id); // abort ffmpeg if it is running on this node
                return Ok(job!.ToDto());
        }
    }

    [HttpGet("{id:long}/download")]
    public async Task<IActionResult> Download(long id, CancellationToken ct)
    {
        var job = await _jobs.GetAsync(id, ct);
        if (job is null) return NotFound();
        if (job.Status != JobStatuses.Completed || job.OutputPath is null)
            return Conflict(new { error = $"job is '{job.Status}', no finished video available" });

        string path;
        try
        {
            path = PathSafety.SafeCombine(_options.StorageRoot, job.OutputPath);
        }
        catch (ArgumentException)
        {
            return StatusCode(500, new { error = "invalid output path" });
        }
        if (!System.IO.File.Exists(path))
            return Conflict(new { error = "output file missing; the job will be requeued on next startup" });

        return PhysicalFile(path, "video/mp4", $"videoforge-job-{id}.mp4");
    }
}
