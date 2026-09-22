-- name: GetUserByToken :one
SELECT * FROM users WHERE api_token = $1;

-- name: GetUserByID :one
SELECT * FROM users WHERE id = $1;

-- name: ModeratorCategoryIDs :many
SELECT category_id FROM moderator_categories WHERE moderator_id = $1;

-- name: IsModeratorInCategory :one
SELECT EXISTS (
    SELECT 1 FROM moderator_categories
    WHERE moderator_id = $1 AND category_id = $2
) AS allowed;
