-- name: CreateJob :one
INSERT INTO jobs (version_id, project_id, created_by, priority, frame_start, frame_end,
                  status, enqueued_at)
VALUES ($1, $2, $3, $4, $5, $6, 'queued', now())
RETURNING *;

-- name: GetJob :one
SELECT * FROM jobs WHERE id = $1;

-- name: GetJobWithVersion :one
SELECT
    j.id, j.version_id, j.project_id, j.created_by, j.priority,
    j.frame_start, j.frame_end, j.status, j.error,
    j.enqueued_at, j.started_at, j.finished_at, j.canceled_by, j.created_at,
    v.composition_id,
    v.manifest,
    v.manifest_sha256 AS version_sha256
FROM jobs j
JOIN versions v ON v.id = j.version_id
WHERE j.id = $1;

-- name: ListJobsForProject :many
SELECT * FROM jobs
WHERE project_id = $1
ORDER BY priority, enqueued_at DESC;

-- name: MarkJobRunning :execrows
UPDATE jobs
SET status = 'running', started_at = now()
WHERE id = $1 AND status = 'queued';

-- Exactly one of the two terminal transitions can win: all three are
-- guarded by status IN ('queued','running'), so a concurrent cancel and
-- complete serialize on the row and only the first survives.

-- name: CompleteJobIfDone :execrows
WITH counts AS (
    SELECT
        count(*)                                          AS total,
        count(*) FILTER (WHERE status = 'succeeded')      AS succeeded,
        count(*) FILTER (WHERE status = 'failed')         AS failed
    FROM frames WHERE job_id = $1
)
UPDATE jobs j
SET status = 'succeeded', finished_at = now(), error = ''
FROM counts c
WHERE j.id = $1
  AND j.status IN ('queued', 'running')
  AND c.succeeded = c.total
;

-- name: FailJobIfAllAttempted :execrows
WITH counts AS (
    SELECT
        count(*)                                          AS total,
        count(*) FILTER (WHERE status = 'succeeded')      AS succeeded,
        count(*) FILTER (WHERE status = 'failed')         AS failed,
        count(*) FILTER (WHERE status IN ('pending','leased')) AS pending
    FROM frames WHERE job_id = $1
)
UPDATE jobs j
SET status = 'failed',
    finished_at = now(),
    error = $2
FROM counts c
WHERE j.id = $1
  AND j.status IN ('queued', 'running')
  AND c.pending = 0 AND c.failed > 0;

-- name: CancelJob :one
UPDATE jobs
SET status = 'canceled', finished_at = now(), canceled_by = $2,
    error = 'canceled'
WHERE id = $1 AND status IN ('queued', 'running')
RETURNING status;

-- name: RequeueLeasedFramesOfJob :exec
UPDATE frames
SET status = 'pending', leased_by = NULL, leased_until = NULL, generation = generation + 1
WHERE job_id = $1 AND status = 'leased';

-- At process (re)start every lease is ownerless: the old worker is gone.
-- Bump every lease generation so any late result from the dead process is
-- rejected by its stale generation.
-- name: RequeueAllLeasedFramesAtStartup :exec
UPDATE frames
SET status = 'pending', leased_by = NULL, leased_until = NULL, generation = generation + 1
WHERE status = 'leased';

-- Jobs that were running when the process died return to 'queued' so the
-- dispatch CTE picks their remaining frames; finished frames stay put.
-- name: RequeueRunningJobsAtStartup :exec
UPDATE jobs SET status = 'queued', started_at = NULL
WHERE status = 'running';

-- Crash recovery: list every job id so outputs can be reconciled.
-- name: ListAllJobIDs :many
SELECT id FROM jobs;

-- A job that reached 'succeeded' but whose outputs are missing/incomplete
-- after a crash goes back to 'queued' (frames are reset separately).
-- name: ReopenSucceededJobForRecovery :execrows
UPDATE jobs SET status = 'queued', finished_at = NULL, error = ''
WHERE id = $1 AND status = 'succeeded';


-- Row-locking read used by the worker submit transaction so cancel and
-- completion serialize on the job row.
-- name: GetJobForUpdate :one
SELECT * FROM jobs WHERE id = $1 FOR UPDATE;
