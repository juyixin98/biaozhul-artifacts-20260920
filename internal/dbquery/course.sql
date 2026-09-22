-- name: CreateCourse :one
INSERT INTO courses (community_id, author_id, title, required_level)
VALUES ($1, $2, $3, $4) RETURNING *;

-- name: GetCourse :one
SELECT * FROM courses WHERE id = $1 AND community_id = $2;

-- name: GetCourseForUpdate :one
SELECT * FROM courses WHERE id = $1 AND community_id = $2 FOR UPDATE;

-- name: ListCourses :many
SELECT * FROM courses
WHERE community_id = $1
  AND (sqlc.narg(published)::boolean IS NULL OR published = sqlc.narg(published))
ORDER BY id DESC;

-- name: ListDraftModules :many
SELECT * FROM course_modules WHERE course_id = $1 ORDER BY position;

-- name: ListDraftLessons :many
SELECT l.* FROM course_lessons l
JOIN course_modules m ON m.id = l.module_id
WHERE m.course_id = $1 ORDER BY m.position, l.position;

-- name: CreateModule :one
INSERT INTO course_modules (course_id, position, title)
VALUES ($1, $2, $3) RETURNING *;

-- name: CreateLesson :one
INSERT INTO course_lessons (module_id, position, title, content_version_id)
VALUES ($1, $2, $3, $4) RETURNING *;

-- name: DeleteModulesForCourse :exec
DELETE FROM course_modules WHERE course_id = $1;

-- name: CreatePublish :one
INSERT INTO course_publishes (course_id, community_id, published_by)
VALUES ($1, $2, $3) RETURNING *;

-- name: CreatePublishModule :one
INSERT INTO course_publish_modules (publish_id, position, title)
VALUES ($1, $2, $3) RETURNING *;

-- name: CreatePublishLesson :one
INSERT INTO course_publish_lessons
    (publish_module_id, position, title, content_version_id, content_id, required_level)
VALUES ($1, $2, $3, $4, $5, $6) RETURNING *;

-- name: MarkCoursePublished :exec
UPDATE courses SET published = TRUE, published_at = now()
WHERE id = $1 AND community_id = $2;

-- name: GetLatestPublish :one
SELECT * FROM course_publishes
WHERE course_id = $1 AND community_id = $2
ORDER BY id DESC LIMIT 1;

-- name: ListPublishModules :many
SELECT * FROM course_publish_modules
WHERE publish_id = $1 ORDER BY position;

-- name: ListPublishLessons :many
SELECT * FROM course_publish_lessons
WHERE publish_module_id = $1 ORDER BY position;

-- Full validation of every lesson's referenced version at publish time:
-- version must exist, be the content's currently published frozen version,
-- content published (not delisted), and the content's required tier must not
-- exceed the course's tier.
-- name: ValidateCourseDraft :many
SELECT m.position AS module_pos, l.position AS lesson_pos, l.title,
       v.id AS version_id, v.review_status,
       c.id AS content_id, c.status AS content_status,
       c.published_version_id, c.required_level AS content_level
FROM course_modules m
JOIN course_lessons l ON l.module_id = m.id
LEFT JOIN content_versions v ON v.id = l.content_version_id
LEFT JOIN contents c ON c.id = v.content_id
WHERE m.course_id = $1
ORDER BY m.position, l.position;
