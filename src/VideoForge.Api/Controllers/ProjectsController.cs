using Microsoft.AspNetCore.Mvc;
using VideoForge.Api.Data;
using VideoForge.Api.Models;
using VideoForge.Api.Services;

namespace VideoForge.Api.Controllers;

[ApiController]
[Route("api/projects")]
public sealed class ProjectsController : ControllerBase
{
    private readonly ProjectRepository _projects;
    private readonly JobRepository _jobs;
    private readonly VideoForgeOptions _options;

    public ProjectsController(ProjectRepository projects, JobRepository jobs,
        Microsoft.Extensions.Options.IOptions<VideoForgeOptions> options)
    {
        _projects = projects;
        _jobs = jobs;
        _options = options.Value;
    }

    [HttpPost]
    public async Task<IActionResult> Create([FromBody] CreateProjectRequest req, CancellationToken ct)
    {
        if (!ModelState.IsValid) return ValidationProblem(ModelState);
        if (!Transitions.IsValid(req.Transition))
            return BadRequest(new { error = "transition must be 'cut' or 'fade'" });

        var project = new Project
        {
            Name = req.Name.Trim(),
            TargetDurationSeconds = req.TargetDurationSeconds,
            Transition = req.Transition,
            Scenes = req.Scenes.Select((s, i) => new Scene
            {
                Ord = i + 1,
                Description = s.Description.Trim(),
            }).ToList(),
        };
        var created = await _projects.InsertAsync(project, ct);
        return Created($"/api/projects/{created.Id}", created.ToDto());
    }

    [HttpGet("{id:long}")]
    public async Task<IActionResult> Get(long id, CancellationToken ct)
    {
        var project = await _projects.GetAsync(id, ct);
        return project is null ? NotFound() : Ok(project.ToDto());
    }

    /// <summary>Submit a render job. The idempotency key deduplicates retries.</summary>
    [HttpPost("{id:long}/jobs")]
    public async Task<IActionResult> SubmitJob(long id, [FromBody] SubmitJobRequest req, CancellationToken ct)
    {
        if (!ModelState.IsValid) return ValidationProblem(ModelState);
        if (await _projects.GetAsync(id, ct) is null)
            return NotFound(new { error = $"project {id} not found" });

        var (job, created) = await _jobs.CreateIfAbsentAsync(id, req.IdempotencyKey.Trim(), _options.MaxAttempts, ct);
        return created
            ? Accepted($"/api/jobs/{job.Id}", job.ToDto())
            : Ok(job.ToDto());
    }
}
