using Dapper;
using VideoForge.Api.Models;

namespace VideoForge.Api.Data;

public enum CancelResult { Cancelled, AlreadyTerminal, NotFound }

public sealed class JobRepository
{
    private readonly Db _db;
    public JobRepository(Db db) => _db = db;

    private sealed class JobWithCreated : Job
    {
        public bool Created { get; set; }
    }

    /// <summary>
    /// Inserts a job for the idempotency key, or returns the existing one.
    /// The unique index on idempotency_key guarantees at most one job per key;
    /// the conflicting transaction blocks until the winner commits, then takes
    /// the no-op DO UPDATE path, so exactly one caller sees Created = true.
    /// </summary>
    public async Task<(Job Job, bool Created)> CreateIfAbsentAsync(
        long projectId, string idempotencyKey, int maxAttempts, CancellationToken ct = default)
    {
        const string sql = """
            INSERT INTO jobs (project_id, idempotency_key, max_attempts)
            VALUES (@projectId, @idempotencyKey, @maxAttempts)
            ON CONFLICT (idempotency_key) DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
            RETURNING *, (xmax = 0) AS created;
            """;
        await using var conn = await _db.OpenAsync(ct);
        var row = await conn.QuerySingleAsync<JobWithCreated>(new CommandDefinition(
            sql, new { projectId, idempotencyKey, maxAttempts }, cancellationToken: ct));
        return (row, row.Created);
    }

    /// <summary>
    /// Atomically claims the oldest queued job. SKIP LOCKED lets multiple workers
    /// compete without ever claiming the same row twice.
    /// </summary>
    public async Task<Job?> ClaimNextAsync(CancellationToken ct = default)
    {
        const string sql = """
            UPDATE jobs
            SET status = 'processing', attempt = attempt + 1, updated_at = now()
            WHERE id = (
                SELECT id FROM jobs WHERE status = 'queued'
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING *;
            """;
        await using var conn = await _db.OpenAsync(ct);
        return await conn.QuerySingleOrDefaultAsync<Job>(new CommandDefinition(sql, cancellationToken: ct));
    }

    public async Task<Job?> GetAsync(long id, CancellationToken ct = default)
    {
        await using var conn = await _db.OpenAsync(ct);
        return await conn.QuerySingleOrDefaultAsync<Job>(
            new CommandDefinition("SELECT * FROM jobs WHERE id = @id", new { id }, cancellationToken: ct));
    }

    /// <summary>
    /// Transitions a queued/processing job to cancelled. The conditional UPDATE
    /// guarantees exactly one terminal state: if the job already reached a
    /// terminal state (e.g. completed concurrently), this is a no-op.
    /// </summary>
    public async Task<(CancelResult Result, Job? Job)> TryCancelAsync(long id, CancellationToken ct = default)
    {
        const string sql = """
            UPDATE jobs SET status = 'cancelled', updated_at = now()
            WHERE id = @id AND status IN ('queued', 'processing')
            RETURNING *;
            """;
        await using var conn = await _db.OpenAsync(ct);
        var job = await conn.QuerySingleOrDefaultAsync<Job>(new CommandDefinition(sql, new { id }, cancellationToken: ct));
        if (job is not null) return (CancelResult.Cancelled, job);

        var current = await GetAsync(id, ct);
        return current is null ? (CancelResult.NotFound, null) : (CancelResult.AlreadyTerminal, current);
    }

    /// <summary>
    /// Marks the job completed, but only if it is still processing on this attempt.
    /// Returns false when a concurrent cancel (or recovery) won the race.
    /// </summary>
    public async Task<bool> TryCompleteAsync(long id, int attempt, string outputPath, CancellationToken ct = default)
    {
        const string sql = """
            UPDATE jobs SET status = 'completed', output_path = @outputPath, error = NULL, updated_at = now()
            WHERE id = @id AND status = 'processing' AND attempt = @attempt;
            """;
        await using var conn = await _db.OpenAsync(ct);
        var rows = await conn.ExecuteAsync(new CommandDefinition(sql, new { id, attempt, outputPath }, cancellationToken: ct));
        return rows == 1;
    }

    /// <summary>
    /// Fails the current attempt. Requeues when attempts remain, otherwise marks failed.
    /// Guarded by status/attempt so a concurrent cancel always wins.
    /// Returns the resulting status, or null when the guard no longer matched.
    /// </summary>
    public async Task<string?> FailOrRequeueAsync(long id, int attempt, string error, CancellationToken ct = default)
    {
        const string sql = """
            UPDATE jobs
            SET status = CASE WHEN attempt < max_attempts THEN 'queued' ELSE 'failed' END,
                error = @error,
                updated_at = now()
            WHERE id = @id AND status = 'processing' AND attempt = @attempt
            RETURNING status;
            """;
        await using var conn = await _db.OpenAsync(ct);
        return await conn.QuerySingleOrDefaultAsync<string?>(new CommandDefinition(
            sql, new { id, attempt, error }, cancellationToken: ct));
    }

    /// <summary>Requeues jobs left in 'processing' by a crash. Returns their ids.</summary>
    public async Task<IReadOnlyList<long>> RequeueInterruptedAsync(CancellationToken ct = default)
    {
        const string sql = """
            UPDATE jobs SET status = 'queued', updated_at = now()
            WHERE status = 'processing'
            RETURNING id;
            """;
        await using var conn = await _db.OpenAsync(ct);
        var rows = await conn.QueryAsync<long>(new CommandDefinition(sql, cancellationToken: ct));
        return rows.AsList();
    }

    /// <summary>
    /// Requeues jobs marked completed whose output file is gone (crash between the
    /// status update and the atomic publish). Returns their ids.
    /// </summary>
    public async Task<IReadOnlyList<long>> RequeueCompletedWithoutFileAsync(
        Func<string, bool> outputExists, CancellationToken ct = default)
    {
        await using var conn = await _db.OpenAsync(ct);
        var rows = await conn.QueryAsync<Job>(new CommandDefinition(
            "SELECT * FROM jobs WHERE status = 'completed'", cancellationToken: ct));
        var missing = rows.Where(j => j.OutputPath is null || !outputExists(j.OutputPath)).Select(j => j.Id).ToList();
        if (missing.Count > 0)
        {
            await conn.ExecuteAsync(new CommandDefinition("""
                UPDATE jobs SET status = 'queued', output_path = NULL, updated_at = now()
                WHERE id = ANY(@missing) AND status = 'completed';
                """, new { missing }, cancellationToken: ct));
        }
        return missing;
    }

    public async Task<long> InsertAttemptAsync(long jobId, int attemptNo, CancellationToken ct = default)
    {
        await using var conn = await _db.OpenAsync(ct);
        return await conn.QuerySingleAsync<long>(new CommandDefinition("""
            INSERT INTO job_attempts (job_id, attempt_no) VALUES (@jobId, @attemptNo) RETURNING id;
            """, new { jobId, attemptNo }, cancellationToken: ct));
    }

    public async Task SetAttemptAssetsAsync(long attemptId, string assetVersionsJson, CancellationToken ct = default)
    {
        await using var conn = await _db.OpenAsync(ct);
        await conn.ExecuteAsync(new CommandDefinition("""
            UPDATE job_attempts SET asset_versions = @assetVersionsJson::jsonb WHERE id = @attemptId;
            """, new { attemptId, assetVersionsJson }, cancellationToken: ct));
    }

    public async Task FinishAttemptAsync(long attemptId, string status, string? log, string? error, CancellationToken ct = default)
    {
        await using var conn = await _db.OpenAsync(ct);
        await conn.ExecuteAsync(new CommandDefinition("""
            UPDATE job_attempts SET status = @status, log = @log, error = @error, finished_at = now()
            WHERE id = @attemptId;
            """, new { attemptId, status, log, error }, cancellationToken: ct));
    }

    public async Task<IReadOnlyList<JobAttempt>> ListAttemptsAsync(long jobId, CancellationToken ct = default)
    {
        await using var conn = await _db.OpenAsync(ct);
        var rows = await conn.QueryAsync<JobAttempt>(new CommandDefinition(
            "SELECT * FROM job_attempts WHERE job_id = @jobId ORDER BY attempt_no", new { jobId }, cancellationToken: ct));
        return rows.AsList();
    }
}
