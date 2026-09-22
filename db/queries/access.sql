-- name: CheckVersionAccess :one
-- Unified gate used for body, attachment download, export and course lessons.
-- Access is granted to a member when:
--   * membership is active and not expired (evaluated at call time);
--   * the member's tier level covers the version's own required level;
--   * the version is approved AND is the currently published version of a live
--     post. During re-review of an edit the post stays 'published' with the
--     old approved version still attached, so members read the last approved
--     content and the unreviewed new version (current_version_id) is denied.
SELECT pv.id AS version_id
FROM post_versions pv
JOIN posts p ON p.id = pv.post_id
JOIN memberships m ON m.community_id = p.community_id AND m.user_id = $2
JOIN tiers t ON t.id = m.tier_id
WHERE pv.id = $1
  AND pv.review_status = 'approved'
  AND t.level >= pv.required_tier_level
  AND m.status = 'active'
  AND m.expires_at > now()
  AND p.status = 'published'
  AND p.published_version_id = pv.id
LIMIT 1;

-- name: GetVersionForModeration :one
-- Reviewers (and admins) may read any version inside their community.
SELECT pv.id AS version_id
FROM post_versions pv
JOIN posts p ON p.id = pv.post_id
WHERE pv.id = $1 AND p.community_id = $2
LIMIT 1;
