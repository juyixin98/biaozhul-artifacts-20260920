-- name: InsertFrame :one
INSERT INTO frames (job_id, frame_no, max_attempts)
VALUES ($1, $2, $3)
RETURNING *;

-- name: GetFrame :one
SELECT * FROM frames WHERE id = $1;

-- name: ListFramesOfJob :many
SELECT * FROM frames WHERE job_id = $1 ORDER BY frame_no;

-- Claim one runnable frame across all runnable jobs.
--
-- Ordering implements priority 1..10 then FIFO by enqueued_at, then frame
-- number. Frames are runnable when pending, or leased but past their lease
-- deadline (the old worker is presumed dead). FOR UPDATE SKIP LOCKED makes
-- the two workers contend-free: a row locked by the other worker is skipped.
-- Each successful (re)claim bumps generation, which is how a late result
-- from the previous owner is rejected on submit.
-- name: ClaimFrame :one
WITH runnable AS (
    SELECT f.id
    FROM frames f
    JOIN jobs j ON j.id = f.job_id
    WHERE j.status IN ('queued', 'running')
      AND (
            f.status = 'pending'
         OR (f.status = 'leased' AND f.leased_until < now())
      )
    ORDER BY j.priority, j.enqueued_at, f.frame_no
    LIMIT 1
    FOR UPDATE OF f SKIP LOCKED
)
UPDATE frames f
SET status       = 'leased',
    leased_by    = sqlc.arg(worker_id)::text,
    leased_until = now() + make_interval(secs => sqlc.arg(lease_seconds)::float8),
    attempts     = f.attempts + 1,
    generation   = f.generation + 1,
    updated_at   = now()
FROM runnable r
WHERE f.id = r.id
RETURNING f.*;

-- Heartbeat: only the current generation owner may extend the lease.
-- name: RenewFrameLease :execrows
UPDATE frames
SET leased_until = now() + make_interval(secs => sqlc.arg(lease_seconds)::float8)
WHERE id = sqlc.arg(id) AND status = 'leased' AND generation = sqlc.arg(generation);

-- Successful submit. Every guard matters:
--   * the frame must still be leased (not reset/reclaimed)
--   * the submitting generation must be current (late worker loses)
--   * the owning job must not have been canceled (no results after cancel)
-- name: CompleteFrame :execrows
UPDATE frames fr
SET status = 'succeeded',
    output_sha256 = sqlc.arg(sha256),
    output_size = sqlc.arg(size_bytes),
    last_error = '',
    leased_by = NULL,
    leased_until = NULL,
    updated_at = now()
WHERE fr.id = sqlc.arg(id)
  AND fr.status = 'leased'
  AND fr.generation = sqlc.arg(generation)
  AND EXISTS (SELECT 1 FROM jobs j WHERE j.id = fr.job_id AND j.status IN ('queued', 'running'));

-- Failure submit. If the frame still has attempts left it goes back to
-- pending for another worker; otherwise it is terminally failed.
-- name: FailFrame :execrows
UPDATE frames fr
SET last_error = sqlc.arg(last_error),
    leased_by = NULL,
    leased_until = NULL,
    status = CASE WHEN fr.attempts < fr.max_attempts THEN 'pending' ELSE 'failed' END,
    updated_at = now()
WHERE fr.id = sqlc.arg(id)
  AND fr.status = 'leased'
  AND fr.generation = sqlc.arg(generation)
  AND EXISTS (SELECT 1 FROM jobs j WHERE j.id = fr.job_id AND j.status IN ('queued', 'running'));

-- Frames of a terminal/canceled job: the worker's submit is simply dropped,
-- lease row is released. Used when CompleteFrame affected 0 rows because the
-- job was canceled after claim.
-- name: ReleaseStaleLease :exec
UPDATE frames fr
SET leased_by = NULL, leased_until = NULL
WHERE fr.id = sqlc.arg(id) AND fr.status = 'leased' AND fr.generation = sqlc.arg(generation);

-- name: CountFrameStatuses :one
SELECT
    count(*) AS total,
    count(*) FILTER (WHERE status = 'pending')   AS pending,
    count(*) FILTER (WHERE status = 'leased')    AS leased,
    count(*) FILTER (WHERE status = 'succeeded') AS succeeded,
    count(*) FILTER (WHERE status = 'failed')    AS failed
FROM frames WHERE job_id = $1;

-- Crash recovery: a frame the DB thinks succeeded but whose output file is
-- missing or corrupt is run again. Bumping generation invalidates any lease
-- the dead process held.
-- name: ResetSucceededFrameForRecovery :exec
UPDATE frames
SET status = 'pending', output_sha256 = '', output_size = 0,
    last_error = 'reset by crash recovery: output file missing',
    leased_by = NULL, leased_until = NULL,
    generation = generation + 1, updated_at = now()
WHERE id = sqlc.arg(id) AND status = 'succeeded';

-- name: ListFramesOfJobForRecovery :many
SELECT id, frame_no, status, output_sha256, output_size, job_id
FROM frames WHERE job_id = sqlc.arg(job_id) ORDER BY frame_no;
