-- name: CreateCourse :one
INSERT INTO courses (community_id, title, status)
VALUES ($1, $2, 'draft')
RETURNING id, community_id, title, status, published_snapshot_id,
          created_at, updated_at;

-- name: GetCourse :one
SELECT id, community_id, title, status, published_snapshot_id,
       created_at, updated_at
FROM courses WHERE id = $1;

-- name: ListCourses :many
SELECT id, community_id, title, status, published_snapshot_id,
       created_at, updated_at
FROM courses WHERE community_id = $1 ORDER BY id DESC;

-- name: UpdateCourseTitle :exec
UPDATE courses SET title = $2, updated_at = now()
WHERE id = $1 AND status = 'draft';

-- Draft modules / lessons -----------------------------------------------------

-- name: UpsertModule :one
INSERT INTO course_modules (course_id, position, title)
VALUES ($1, $2, $3)
ON CONFLICT (course_id, position) DO UPDATE SET title = EXCLUDED.title
RETURNING id, course_id, position, title;

-- name: DeleteModulesFromPosition :exec
DELETE FROM course_modules WHERE course_id = $1 AND position >= $2;

-- name: DeleteAllModules :exec
DELETE FROM course_modules WHERE course_id = $1;

-- name: ListDraftModules :many
SELECT id, course_id, position, title
FROM course_modules WHERE course_id = $1 ORDER BY position;

-- name: UpsertLesson :one
INSERT INTO course_lessons (module_id, position, title, content_version_id)
VALUES ($1, $2, $3, $4)
ON CONFLICT (module_id, position) DO UPDATE
SET title = EXCLUDED.title,
    content_version_id = EXCLUDED.content_version_id
RETURNING id, module_id, position, title, content_version_id;

-- name: DeleteLessonsFromPosition :exec
DELETE FROM course_lessons WHERE module_id = $1 AND position >= $2;

-- name: ListDraftLessons :many
SELECT id, module_id, position, title, content_version_id
FROM course_lessons WHERE module_id = $1 ORDER BY position;

-- Publishing ------------------------------------------------------------------

-- name: MarkCoursePublished :one
UPDATE courses
SET status = 'published', published_snapshot_id = $2, updated_at = now()
WHERE id = $1 AND community_id = $3
RETURNING id, community_id, title, status, published_snapshot_id,
          created_at, updated_at;

-- name: ResetCourseToDraft :one
UPDATE courses SET status = 'draft', updated_at = now()
WHERE id = $1 AND community_id = $2
RETURNING id, community_id, title, status, published_snapshot_id,
          created_at, updated_at;

-- name: InsertSnapshot :one
INSERT INTO course_publish_snapshots (course_id, title, published_by)
VALUES ($1, $2, $3)
RETURNING id, course_id, title, published_at, published_by;

-- name: InsertSnapshotModule :one
INSERT INTO course_snapshot_modules (snapshot_id, position, title)
VALUES ($1, $2, $3)
RETURNING id, snapshot_id, position, title;

-- name: InsertSnapshotLesson :one
INSERT INTO course_snapshot_lessons (snap_module_id, position, title,
                                     content_version_id)
VALUES ($1, $2, $3, $4)
RETURNING id, snap_module_id, position, title, content_version_id;

-- name: GetSnapshot :one
SELECT id, course_id, title, published_at, published_by
FROM course_publish_snapshots WHERE id = $1;

-- name: ListSnapshotModules :many
SELECT id, snapshot_id, position, title
FROM course_snapshot_modules WHERE snapshot_id = $1 ORDER BY position;

-- name: ListSnapshotLessons :many
SELECT id, snap_module_id, position, title, content_version_id
FROM course_snapshot_lessons
WHERE snap_module_id = ANY($1::bigint[]) ORDER BY snap_module_id, position;

-- name: GetDraftStructure :many
-- All lessons of a course with module positions, used at publish validation.
SELECT cl.content_version_id AS content_version_id,
       cm.position AS module_position,
       cl.position AS lesson_position
FROM course_modules cm
JOIN course_lessons cl ON cl.module_id = cm.id
WHERE cm.course_id = $1
ORDER BY cm.position, cl.position;

-- name: GetVersionsForPublish :many
SELECT pv.id, pv.post_id, pv.version_number, pv.title, pv.review_status,
       p.status AS post_status, p.published_version_id
FROM post_versions pv
JOIN posts p ON p.id = pv.post_id
WHERE pv.id = ANY($1::bigint[]);
