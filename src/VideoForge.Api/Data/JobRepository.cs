using System.Text.Json;
using Dapper;
using Npgsql;
using VideoForge.Api.Models;

namespace VideoForge.Api.Data;

public sealed record ClaimResult(Job Job, int AttemptNo);

public sealed class JobRepository(NpgsqlDataSource dataSource)
{
    public async Task<Job?> GetAsync(Guid id, CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        return await conn.QuerySingleOrDefaultAsync<Job>("SELECT * FROM jobs WHERE id = @id", new { id });
    }

    /// <summary>
    /// Idempotent insert for a submission key. Returns the existing row if the
    /// key was already submitted (any status), so replays never create a
    /// duplicate task.
    /// </summary>
    public async Task<(Job Job, bool Created)> CreateIfNotExistsAsync(
        Job job, CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        var inserted = await conn.QuerySingleOrDefaultAsync<Job>("""
            INSERT INTO jobs (id, project_id, submission_key, status, priority)
            VALUES (@Id, @ProjectId, @SubmissionKey, 'queued', @Priority)
            ON CONFLICT (submission_key) DO NOTHING
            RETURNING *
            """, job);
        if (inserted is not null) return (inserted, true);

        var existing = await conn.QuerySingleAsync<Job>(
            "SELECT * FROM jobs WHERE submission_key = @SubmissionKey",
            new { job.SubmissionKey });
        return (existing, false);
    }

    /// <summary>
    /// Atomically claim one queued job (SKIP LOCKED). Increments attempt
    /// number inside the same transaction. Returns null when the queue is
    /// empty or the job was cancelled before it started.
    /// </summary>
    public async Task<ClaimResult?> ClaimAsync(string workerId, int staleSeconds, CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        await using var tx = await conn.BeginTransactionAsync(ct);

        Job? claimed = null;
        await using (var cmd = new NpgsqlCommand("SELECT * FROM claim_next_job($1)",
                         (NpgsqlConnection)conn, (NpgsqlTransaction)tx))
        {
            cmd.Parameters.AddWithValue(workerId);
            await using var reader = await cmd.ExecuteReaderAsync(ct);
            while (await reader.ReadAsync(ct))
                claimed = MapJob(reader);
        }
        if (claimed is null)
        {
            await tx.CommitAsync(ct);
            return null;
        }

        // If cancel was requested while queued, settle immediately instead of rendering.
        if (claimed.CancelRequested)
        {
            await conn.ExecuteAsync("""
                UPDATE jobs SET status = 'cancelled', finished_at = now()
                WHERE id = @id AND status = 'processing'
                """, new { claimed.Id }, tx);
            await tx.CommitAsync(ct);
            return null;
        }

        var attemptNo = await conn.ExecuteScalarAsync<int>(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM job_attempts WHERE job_id = @id",
            new { claimed.Id }, tx);
        await conn.ExecuteAsync("""
            INSERT INTO job_attempts (job_id, attempt_no, worker_id, material_manifest)
            VALUES (@JobId, @AttemptNo, @WorkerId, '{}'::jsonb)
            """, new { JobId = claimed.Id, AttemptNo = attemptNo, WorkerId = workerId }, tx);

        await tx.CommitAsync(ct);
        return new ClaimResult(claimed, attemptNo);
    }

    public async Task HeartbeatAsync(Guid jobId, string workerId, CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        await conn.ExecuteAsync(
            "UPDATE jobs SET heartbeat_at = now() WHERE id = @id AND locked_by = @w",
            new { id = jobId, w = workerId });
    }

    /// <summary>
    /// Reclaims processing jobs whose worker went silent (process crash /
    /// machine loss): back to queued, lock cleared. Only stale rows move, so
    /// a live worker holding a job is never disturbed.
    /// </summary>
    public async Task<int> RequeueStaleAsync(int staleSeconds, CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        await using var tx = await conn.BeginTransactionAsync(ct);

        // Requeue the stale jobs...
        var affected = await conn.ExecuteAsync("""
            UPDATE jobs
               SET status = 'queued', locked_by = NULL, locked_at = NULL, heartbeat_at = NULL,
                   error = COALESCE(error, 'Worker interrupted; re-queued for recovery')
             WHERE status = 'processing'
               AND (heartbeat_at IS NULL
                    OR heartbeat_at < now() - make_interval(secs => @stale))
            """, new { stale = staleSeconds }, tx);

        // ...and close exactly their open attempts in the same transaction.
        if (affected > 0)
        {
            await conn.ExecuteAsync("""
                UPDATE job_attempts a
                   SET outcome = 'interrupted',
                       finished_at = now(),
                       error = COALESCE(a.error,
                               'Worker heartbeat stopped before the attempt finished')
                  FROM jobs j
                 WHERE a.job_id = j.id
                   AND a.outcome IS NULL
                   AND j.status = 'queued'
                   AND j.locked_by IS NULL
                """, transaction: tx);
        }

        await tx.CommitAsync(ct);
        return affected;
    }

    /// <summary>
    /// Request cancellation. A queued job moves straight to 'cancelled'
    /// atomically; a processing job only gets cancel_requested = true so its
    /// worker performs the terminal transition. If a claim is committing at
    /// the same instant, this statement blocks on the row lock and then sees
    /// 'processing', so exactly one rule applies.
    /// </summary>
    public async Task<(string? Status, bool Found, bool Changed)> RequestCancelAsync(
        Guid jobId, CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        await using var tx = await conn.BeginTransactionAsync(ct);
        var before = await conn.QuerySingleOrDefaultAsync<string>(
            "SELECT status FROM jobs WHERE id = @id FOR UPDATE", new { id = jobId }, tx);
        if (before is null)
        {
            await tx.CommitAsync(ct);
            return (null, false, false);
        }

        bool changed = false;
        string after = before;
        if (before is "queued" or "processing")
        {
            after = await conn.QuerySingleAsync<string>("""
                UPDATE jobs
                   SET cancel_requested = true,
                       status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
                       finished_at = CASE WHEN status = 'queued' THEN now() ELSE finished_at END
                 WHERE id = @id
                 RETURNING status
                """, new { id = jobId }, tx);
            changed = true;
        }
        await tx.CommitAsync(ct);
        return (after, true, changed);
    }

    /// <summary>
    /// Terminal transition by the worker. The guard is the crux of the
    /// cancel/complete race:
    ///  - success lands only if no cancel was requested and the job is still
    ///    owned by this worker in processing;
    ///  - cancellation lands only if a cancel WAS requested;
    ///  - neither path can overwrite an existing terminal row.
    /// Exactly one of them affects one row max.
    /// </summary>
    public async Task<bool> TryCompleteAsync(
        Guid jobId, string workerId, string publishPath, long sizeBytes, long durationMs,
        CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        var rows = await conn.ExecuteAsync("""
            UPDATE jobs
               SET status = 'completed',
                   publish_path = @publishPath,
                   output_size = @size,
                   output_duration_ms = @duration,
                   finished_at = now()
             WHERE id = @id
               AND status = 'processing'
               AND locked_by = @worker
               AND cancel_requested = false
            """, new { id = jobId, worker = workerId, publishPath, size = sizeBytes, duration = durationMs });
        return rows == 1;
    }

    /// <summary>
    /// Cancel a processing job whose render has stopped after a cancel
    /// request. Only succeeds against a still-processing row that had cancel
    /// requested; returns false if the completion already landed (in which
    /// case the published video stands).
    /// </summary>
    public async Task<bool> TryCancelAsync(Guid jobId, string workerId, CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        var rows = await conn.ExecuteAsync("""
            UPDATE jobs
               SET status = 'cancelled', finished_at = now()
             WHERE id = @id
               AND status = 'processing'
               AND locked_by = @worker
               AND cancel_requested = true
            """, new { id = jobId, worker = workerId });
        return rows == 1;
    }

    /// <summary>
    /// Mark an attempt failed. If more attempts remain and the job is still
    /// live (not cancelled, still processing/owned), requeue; otherwise the
    /// job fails terminally. Never touches an already-completed job, so a
    /// retry can never overwrite a published video.
    /// </summary>
    public async Task FailAsync(Guid jobId, string workerId, int attemptNo, string error, int maxAttempts,
        CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        await using var tx = await conn.BeginTransactionAsync(ct);

        await conn.ExecuteAsync("""
            UPDATE job_attempts
               SET outcome = 'failed', finished_at = now(), error = @error
             WHERE job_id = @id AND attempt_no = @no
            """, new { id = jobId, no = attemptNo, error }, tx);

        if (attemptNo < maxAttempts)
        {
            var requeued = await conn.ExecuteAsync("""
                UPDATE jobs
                   SET status = 'queued', locked_by = NULL, locked_at = NULL, heartbeat_at = NULL,
                       error = @error
                 WHERE id = @id AND status = 'processing' AND locked_by = @worker
                   AND cancel_requested = false
                """, new { id = jobId, worker = workerId, error }, tx);
            if (requeued == 0)
            {
                // Cancellation won the race, or ownership changed: settle to cancelled if requested.
                await conn.ExecuteAsync("""
                    UPDATE jobs SET status = 'cancelled', finished_at = now()
                    WHERE id = @id AND status = 'processing' AND cancel_requested = true
                    """, new { id = jobId }, tx);
            }
        }
        else
        {
            await conn.ExecuteAsync("""
                UPDATE jobs
                   SET status = 'failed', finished_at = now(), error = @error
                 WHERE id = @id AND status = 'processing' AND locked_by = @worker
                """, new { id = jobId, worker = workerId, error }, tx);
            // If cancellation was also requested on the final attempt, failed still stands as the
            // render outcome; cancel_requested remains visible on the row.
        }

        await tx.CommitAsync(ct);
    }

    public async Task RecordAttemptOutcomeAsync(Guid jobId, int attemptNo, string outcome,
        string? error = null, string? ffmpegLog = null, string? manifestJson = null,
        CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand("""
            UPDATE job_attempts
               SET outcome = $3, finished_at = now(),
                   error = COALESCE($4, error),
                   ffmpeg_log = COALESCE($5, ffmpeg_log),
                   material_manifest = COALESCE($6::jsonb, material_manifest)
             WHERE job_id = $1 AND attempt_no = $2
            """, conn);
        cmd.Parameters.AddWithValue(jobId);
        cmd.Parameters.AddWithValue(attemptNo);
        cmd.Parameters.AddWithValue(outcome);
        cmd.Parameters.AddWithValue(error ?? (object)DBNull.Value);
        cmd.Parameters.AddWithValue(ffmpegLog ?? (object)DBNull.Value);
        // Send the JSON document as text; the explicit ::jsonb cast parses it.
        // Sending a CLR string with parameter type jsonb would re-encode it as
        // a JSON string scalar (double encoding).
        var jsonParam = new NpgsqlParameter
        {
            NpgsqlDbType = NpgsqlTypes.NpgsqlDbType.Text,
            Value = (object?)manifestJson ?? DBNull.Value
        };
        cmd.Parameters.Add(jsonParam);
        await cmd.ExecuteNonQueryAsync(ct);
    }

    public async Task SaveManifestAsync(Guid jobId, int attemptNo, string manifestJson, CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "UPDATE job_attempts SET material_manifest = $3::jsonb WHERE job_id = $1 AND attempt_no = $2", conn);
        cmd.Parameters.AddWithValue(jobId);
        cmd.Parameters.AddWithValue(attemptNo);
        cmd.Parameters.AddWithValue(manifestJson);
        await cmd.ExecuteNonQueryAsync(ct);
    }

    public async Task<IReadOnlyList<JobAttempt>> GetAttemptsAsync(Guid jobId, CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        var rows = await conn.QueryAsync<JobAttempt>(
            "SELECT * FROM job_attempts WHERE job_id = @id ORDER BY attempt_no", new { id = jobId });
        return rows.AsList();
    }

    public async Task<bool> IsCancelRequestedAsync(Guid jobId, CancellationToken ct = default)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        return await conn.ExecuteScalarAsync<bool>(
            "SELECT cancel_requested FROM jobs WHERE id = @id", new { id = jobId });
    }

    private static Job MapJob(NpgsqlDataReader r)
    {
        T? N<T>(string name) where T : class
        {
            var o = r.GetOrdinal(name);
            return r.IsDBNull(o) ? null : (T)r.GetValue(o);
        }
        long? Nl(string name)
        {
            var o = r.GetOrdinal(name);
            return r.IsDBNull(o) ? null : r.GetInt64(o);
        }
        DateTime? Nd(string name)
        {
            var o = r.GetOrdinal(name);
            return r.IsDBNull(o) ? null : r.GetDateTime(o);
        }
        return new Job
        {
            Id = r.GetGuid(r.GetOrdinal("id")),
            ProjectId = r.GetGuid(r.GetOrdinal("project_id")),
            SubmissionKey = r.GetString(r.GetOrdinal("submission_key")),
            Status = r.GetString(r.GetOrdinal("status")),
            Priority = r.GetInt32(r.GetOrdinal("priority")),
            CancelRequested = r.GetBoolean(r.GetOrdinal("cancel_requested")),
            LockedBy = N<string>("locked_by"),
            LockedAt = Nd("locked_at"),
            HeartbeatAt = Nd("heartbeat_at"),
            StartedAt = Nd("started_at"),
            FinishedAt = Nd("finished_at"),
            PublishPath = N<string>("publish_path"),
            OutputSize = Nl("output_size"),
            OutputDurationMs = Nl("output_duration_ms"),
            Error = N<string>("error"),
            CreatedAt = r.GetDateTime(r.GetOrdinal("created_at"))
        };
    }
}
