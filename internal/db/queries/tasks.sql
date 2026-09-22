-- name: CreateTask :one
INSERT INTO render_tasks (id, project_id, composition_id, version_id,
                         frame_start, frame_end, priority, created_by, output_dir)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
RETURNING *;

-- name: CreateFrame :exec
INSERT INTO frames (id, task_id, frame_index)
VALUES ($1, $2, $3);

-- name: GetTask :one
SELECT * FROM render_tasks WHERE id = $1;

-- name: LockTask :one
SELECT * FROM render_tasks WHERE id = $1 FOR UPDATE;

-- name: MarkTaskRunning :exec
UPDATE render_tasks
SET status = 'running', started_at = COALESCE(started_at, now())
WHERE id = $1 AND status = 'queued';

-- name: ListProjectTasks :many
SELECT * FROM render_tasks
WHERE project_id = $1
ORDER BY created_at DESC
LIMIT $2;

-- name: FrameStatusCounts :many
SELECT status, count(*)::bigint AS count
FROM frames WHERE task_id = $1 GROUP BY status;

-- name: ListFrames :many
SELECT * FROM frames WHERE task_id = $1 ORDER BY frame_index;

-- name: ListExpiredLeasedFrameTaskIDs :many
SELECT DISTINCT task_id FROM frames
WHERE status = 'leased' AND lease_expires_at < now();

-- name: ResetExpiredLeases :many
-- Expired leases return to the pending pool, generation is bumped so the old
-- worker can never overwrite the new holder's result. Attempts is left as-is
-- (it was incremented when the lease was granted).
WITH expired AS (
    SELECT id
    FROM frames
    WHERE status = 'leased' AND lease_expires_at < now()
)
UPDATE frames f
SET status = 'pending',
    lease_token = NULL,
    leased_by = NULL,
    leased_at = NULL,
    lease_expires_at = NULL,
    generation = generation + 1,
    updated_at = now()
FROM expired e
WHERE f.id = e.id
RETURNING f.id, f.task_id;

-- name: ClaimNextFrame :one
-- Highest task priority first, FIFO (enqueued_seq) within a priority, frames in
-- order within a task. Pending rows, or leased rows whose lease is dead and
-- which still have retries left, are claimable. FOR UPDATE SKIP LOCKED makes
-- concurrent claimers skip a frame another transaction has already taken.
WITH candidate AS MATERIALIZED (
    SELECT f.id AS frame_id, f.task_id AS frame_task_id
    FROM frames f
    JOIN render_tasks t ON t.id = f.task_id
    WHERE t.status IN ('queued', 'running')
      AND (
            f.status = 'pending'
         OR (f.status = 'leased'
             AND f.lease_expires_at < now()
             AND f.attempts < $3)
      )
    ORDER BY t.priority DESC, t.enqueued_seq ASC, f.frame_index ASC
    LIMIT 1
    FOR UPDATE OF f SKIP LOCKED
), claimed_frame AS (
    UPDATE frames f
    SET status = 'leased',
        attempts = attempts + 1,
        lease_token = $1,
        leased_by = $2,
        leased_at = now(),
        lease_expires_at = now() + make_interval(secs => $4),
        generation = CASE WHEN f.status = 'pending' THEN f.generation ELSE f.generation + 1 END,
        updated_at = now()
    FROM candidate c
    WHERE f.id = c.frame_id
    RETURNING f.*
), claimed AS (
    SELECT cf.id AS frame_id, cf.task_id, cf.frame_index, cf.generation
    FROM claimed_frame cf
)
UPDATE render_tasks t
SET status = 'running', started_at = COALESCE(started_at, now())
FROM claimed c
WHERE t.id = c.task_id
RETURNING c.frame_id AS frame_id, t.id AS task_id, t.version_id, t.output_dir,
          c.frame_index AS frame_index, c.generation AS generation;

-- name: RenewFrameLease :one
-- Heartbeat: only the current lease holder with the current generation renews.
UPDATE frames
SET lease_expires_at = now() + make_interval(secs => $4),
    updated_at = now()
WHERE id = $1
  AND lease_token = $2
  AND generation = $3
  AND status = 'leased'
RETURNING *;

-- name: LockTaskForFrame :one
-- Take the task row lock on behalf of a frame submit. Cancel takes the same
-- lock, so completion and cancellation serialize: exactly one terminal state.
SELECT t.*
FROM render_tasks t
JOIN frames f ON f.task_id = t.id
WHERE f.id = $1
FOR UPDATE OF t;

-- name: SubmitFrameSuccessUpdate :execrows
-- Flip one leased frame to succeeded. Rejected (0 rows) when the lease token
-- or generation is stale, the frame is not leased, or the task is already
-- cancelled/failed. Must run inside a tx that first holds LockTaskForFrame.
UPDATE frames
SET status = 'succeeded',
    output_path = sub.output_path,
    output_sha256 = sub.output_sha256,
    output_size_bytes = sub.output_size_bytes,
    lease_token = NULL,
    leased_by = NULL,
    leased_at = NULL,
    lease_expires_at = NULL,
    error = NULL,
    updated_at = now()
FROM (
    SELECT sqlc.arg(frame_id)::uuid AS frame_id,
           sqlc.arg(lease_token)::uuid AS lease_token,
           sqlc.arg(generation)::bigint AS generation,
           sqlc.arg(output_path)::text AS output_path,
           sqlc.arg(output_sha256)::text AS output_sha256,
           sqlc.arg(output_size_bytes)::bigint AS output_size_bytes
) sub
WHERE frames.id = sub.frame_id
  AND frames.lease_token = sub.lease_token
  AND frames.generation = sub.generation
  AND frames.status = 'leased'
  AND EXISTS (
      SELECT 1 FROM render_tasks t
      WHERE t.id = frames.task_id
        AND t.status NOT IN ('cancelled', 'failed')
  );

-- name: CountOpenFramesForTask :one
-- Frames not in a final state (cancelled and succeeded are final; failed is
-- permanent too and is handled by the failure path).
SELECT count(*)::bigint AS open_count
FROM frames
WHERE task_id = $1
  AND status IN ('pending', 'leased', 'failed');

-- name: CompleteTaskIfAllDone :execrows
-- Mark a non-terminal task succeeded only when no open frames remain.
UPDATE render_tasks rt
SET status = 'succeeded', finished_at = now()
WHERE rt.id = $1
  AND rt.status NOT IN ('cancelled', 'failed', 'succeeded')
  AND NOT EXISTS (
      SELECT 1 FROM frames fr
      WHERE fr.task_id = rt.id AND fr.status IN ('pending', 'leased', 'failed')
  );

-- name: SubmitFrameFailure :execrows
-- Record an attempt failure. If no retries remain, mark frame failed.
-- The task is failed separately (FailTask) inside the same tx, which cancels
-- all non-terminal sibling frames. Returns 0 rows affected when the submitter
-- is stale (wrong lease token / generation / no longer leased).
WITH input AS (
    SELECT sqlc.arg(frame_id)::uuid AS frame_id,
           sqlc.arg(lease_token)::uuid AS lease_token,
           sqlc.arg(generation)::bigint AS generation,
           sqlc.arg(max_attempts)::smallint AS max_attempts,
           sqlc.arg(error)::text AS err
)
UPDATE frames f
SET status = CASE WHEN f.attempts >= i.max_attempts THEN 'failed'::text ELSE 'pending'::text END,
    lease_token = NULL,
    leased_by = NULL,
    leased_at = NULL,
    lease_expires_at = NULL,
    generation = f.generation + 1,
    error = i.err,
    updated_at = now()
FROM input i
WHERE f.id = i.frame_id
  AND f.lease_token = i.lease_token
  AND f.generation = i.generation
  AND f.status = 'leased';

-- name: GetFrame :one
SELECT * FROM frames WHERE id = $1;

-- name: GetFrameForTask :one
SELECT * FROM frames WHERE task_id = $1 AND frame_index = $2;

-- name: FailTask :execrows
-- Terminal failure: exactly one row wins the transition.
UPDATE render_tasks
SET status = 'failed', finished_at = now(), error = $2
WHERE id = $1 AND status NOT IN ('failed', 'cancelled', 'succeeded');

-- name: CancelRemainingFrames :many
-- Rows in a failing/cancelled task that never reach a terminal state become
-- 'cancelled'; already terminal frames (succeeded/failed) keep their state.
UPDATE frames
SET status = 'cancelled',
    lease_token = NULL,
    leased_by = NULL,
    leased_at = NULL,
    lease_expires_at = NULL,
    generation = generation + 1,
    updated_at = now()
WHERE task_id = $1
  AND status IN ('pending', 'leased')
RETURNING id;

-- name: CancelTask :one
-- Cancellation takes the task row lock; the worker's submit takes the same
-- lock, so exactly one terminal outcome survives.
UPDATE render_tasks
SET status = 'cancelled', finished_at = now()
WHERE id = $1 AND status IN ('queued', 'running')
RETURNING *;

-- name: ResetFrameForRecovery :exec
-- Startup reconciliation: a frame claims it succeeded but the output file is
-- missing/corrupt -> it must be rendered again.
UPDATE frames
SET status = 'pending',
    lease_token = NULL,
    leased_by = NULL,
    leased_at = NULL,
    lease_expires_at = NULL,
    generation = generation + 1,
    output_path = NULL,
    output_sha256 = NULL,
    output_size_bytes = NULL,
    error = NULL,
    updated_at = now()
WHERE id = $1 AND status = 'succeeded';

-- name: RecoverInterruptedLeases :many
-- Any lease still open at startup is stale (the owning worker is dead).
UPDATE frames
SET status = 'pending',
    lease_token = NULL,
    leased_by = NULL,
    leased_at = NULL,
    lease_expires_at = NULL,
    generation = generation + 1,
    updated_at = now()
WHERE status = 'leased'
RETURNING id, task_id;

-- name: UnfinishTasksFromFrames :many
-- A task marked running/succeeded with pending frames must be (re)queued.
UPDATE render_tasks t
SET status = 'queued',
    finished_at = NULL,
    error = NULL
WHERE status IN ('running', 'succeeded')
  AND EXISTS (SELECT 1 FROM frames f
              WHERE f.task_id = t.id
                AND f.status IN ('pending', 'leased'))
  AND NOT EXISTS (SELECT 1 FROM frames f
                  WHERE f.task_id = t.id AND f.status = 'failed')
RETURNING id;

-- name: ListSucceededFrames :many
SELECT * FROM frames WHERE status = 'succeeded';

-- name: SucceededFrameExistsAtPath :one
SELECT EXISTS (
    SELECT 1 FROM frames WHERE output_path = $1 AND status = 'succeeded'
);

-- name: RefinalizeSucceededTasks :execrows
-- Tasks whose frames are all done but which are not yet terminal -> succeeded.
UPDATE render_tasks t
SET status = 'succeeded', finished_at = COALESCE(finished_at, now())
WHERE status IN ('queued', 'running')
  AND NOT EXISTS (SELECT 1 FROM frames f
                  WHERE f.task_id = t.id
                    AND f.status IN ('pending', 'leased', 'failed'))
  AND EXISTS (SELECT 1 FROM frames f WHERE f.task_id = t.id);

-- name: RefinalizeFailedTasks :execrows
-- A non-terminal task containing a permanently failed frame is failed.
UPDATE render_tasks t
SET status = 'failed',
    finished_at = COALESCE(finished_at, now()),
    error = COALESCE(error, 'one or more frames failed permanently')
WHERE status IN ('queued', 'running')
  AND EXISTS (SELECT 1 FROM frames f
              WHERE f.task_id = t.id AND f.status = 'failed');

-- name: ListFailedTaskIDs :many
SELECT id FROM render_tasks WHERE status = 'failed';
