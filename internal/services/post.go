package services

import (
	"archive/zip"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"communitygov/internal/database"
	"communitygov/internal/database/sqlcgen"
	"communitygov/internal/middleware"
)

type PostService struct{ store *database.Store }

func NewPostService(s *database.Store) *PostService { return &PostService{store: s} }

// CreatePost starts a post in draft with its first immutable version.
func (s *PostService) CreatePost(ctx context.Context, communityID, authorID int64,
	level int32, title, body string) (sqlcgen.Post, sqlcgen.PostVersion, error) {
	if level < 1 || level > 10 {
		return sqlcgen.Post{}, sqlcgen.PostVersion{}, E(ErrMaxTiers, "required_tier_level 1..10")
	}
	if title == "" {
		return sqlcgen.Post{}, sqlcgen.PostVersion{}, E(ErrValidation, "title required")
	}
	var post sqlcgen.Post
	var ver sqlcgen.PostVersion
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		var err error
		post, err = q.CreatePost(ctx, sqlcgen.CreatePostParams{
			CommunityID: communityID, AuthorID: authorID,
			RequiredTierLevel: level, Title: title,
		})
		if err != nil {
			return err
		}
		ver, err = q.InsertVersion(ctx, sqlcgen.InsertVersionParams{
			PostID: post.ID, VersionNumber: 1, Title: title, Body: body,
			RequiredTierLevel: level, AuthorID: authorID, ReviewStatus: "draft",
		})
		if err != nil {
			return err
		}
		return setPostCurrent(ctx, q, post.ID, ver.ID)
	})
	return post, ver, err
}

// setPostCurrent points current_version at a new version.
func setPostCurrent(ctx context.Context, q *sqlcgen.Queries, postID, versionID int64) error {
	return q.SetPostCurrentVersion(ctx,
		sqlcgen.SetPostCurrentVersionParams{ID: postID, CurrentVersionID: toPgInt8(versionID)})
}

// AddVersion appends a NEW immutable version to a post the author is editing.
// A post under review cannot be edited (409) — the author must withdraw first —
// which is what keeps an approval racing an edit from publishing new content.
// Published posts may be edited: a fresh draft version is created and the post
// returns to draft (the published version stays untouched until re-approval).
func (s *PostService) AddVersion(ctx context.Context, postID, authorID int64,
	title, body string, level int32) (sqlcgen.PostVersion, error) {
	if level < 1 || level > 10 {
		return sqlcgen.PostVersion{}, E(ErrMaxTiers, "required_tier_level 1..10")
	}
	var ver sqlcgen.PostVersion
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		p, err := q.GetPostForUpdate(ctx, postID)
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "post")
		} else if err != nil {
			return err
		}
		if p.AuthorID != authorID {
			return E(ErrForbidden, "only the author can edit")
		}
		if p.Status == "pending" {
			return E(ErrPostState, "post is under review; withdraw before editing")
		}
		if p.Status == "removed" {
			return E(ErrPostState, "removed posts cannot be edited; request restore or create a new post")
		}
		next, err := q.NextVersionNumber(ctx, postID)
		if err != nil {
			return err
		}
		ver, err = q.InsertVersion(ctx, sqlcgen.InsertVersionParams{
			PostID: postID, VersionNumber: int32(next), Title: title, Body: body,
			RequiredTierLevel: level, AuthorID: authorID, ReviewStatus: "draft",
		})
		if err != nil {
			return err
		}
		if _, err := q.MarkDraft(ctx, sqlcgen.MarkDraftParams{ID: postID, CommunityID: p.CommunityID}); err != nil {
			return err
		}
		return setPostCurrent(ctx, q, postID, ver.ID)
	})
	return ver, err
}

// Submit moves the author's current draft version into the review queue.
func (s *PostService) Submit(ctx context.Context, postID, authorID int64) (sqlcgen.Post, error) {
	var post sqlcgen.Post
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		p, err := q.GetPostForUpdate(ctx, postID)
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "post")
		} else if err != nil {
			return err
		}
		if p.AuthorID != authorID {
			return E(ErrForbidden, "only the author can submit")
		}
		if p.Status != "draft" && p.Status != "published" {
			return E(ErrPostState, "post cannot be submitted from its current state")
		}
		if p.Status == "published" && (!p.PublishedVersionID.Valid ||
			(p.CurrentVersionID.Valid && p.CurrentVersionID.Int64 == p.PublishedVersionID.Int64)) {
			return E(ErrPostState, "no new edit is awaiting submission on this live post")
		}
		cur, err := q.GetVersion(ctx, p.CurrentVersionID.Int64)
		if err != nil {
			return err
		}
		if cur.ReviewStatus == "approved" {
			return E(ErrPostState, "current version is already approved")
		}
		n, err := q.SubmitVersionPending(ctx, cur.ID)
		if err != nil {
			return err
		}
		if n == 0 {
			return E(ErrPostState, "version cannot be submitted from its state")
		}
		post, err = q.SubmitForReview(ctx,
			sqlcgen.SubmitForReviewParams{ID: postID, CommunityID: p.CommunityID})
		return err
	})
	return post, err
}

func (s *PostService) Withdraw(ctx context.Context, postID, authorID int64) (sqlcgen.Post, error) {
	var post sqlcgen.Post
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		p, err := q.GetPostForUpdate(ctx, postID)
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "post")
		} else if err != nil {
			return err
		}
		if p.AuthorID != authorID {
			return E(ErrForbidden, "only the author can withdraw")
		}
		// Withdrawal applies only to a first submission (pending). A re-edit of
		// a live post stays published; its author instead waits for rejection
		// or edits the already-draft newest version after a reject.
		if p.Status != "pending" {
			return E(ErrPostState, "only a pending first submission can be withdrawn")
		}
		post, err = q.WithdrawSubmission(ctx,
			sqlcgen.WithdrawSubmissionParams{ID: postID, CommunityID: p.CommunityID})
		if err != nil {
			return err
		}
		if p.CurrentVersionID.Valid {
			if err := q.SetVersionStatus(ctx,
				sqlcgen.SetVersionStatusParams{ID: p.CurrentVersionID.Int64, ReviewStatus: "draft"}); err != nil {
				return err
			}
		}
		return nil
	})
	return post, err
}

// ReviewAction is an approve/reject decision bound to both the version the
// reviewer saw and the expected post status.
type ReviewAction struct {
	PostID         int64
	CommunityID    int64
	ReviewerID     int64
	VersionID      int64
	Approve        bool
	Reason         string
	ExpectedStatus string // must be "pending"
}

// Review performs the atomic approve/reject. The UPDATE ... WHERE
// status='pending' AND current_version_id=$version matches zero rows if the
// author swapped versions in the meantime, so unreviewed content can never be
// published by a stale approval.
func (s *PostService) Review(ctx context.Context, in ReviewAction) (sqlcgen.Post, error) {
	if in.ExpectedStatus == "" {
		in.ExpectedStatus = "pending"
	}
	var post sqlcgen.Post
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		p, err := q.GetPostForUpdate(ctx, in.PostID)
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "post")
		} else if err != nil {
			return err
		}
		if p.CommunityID != in.CommunityID {
			return E(ErrNotFound, "post not in community")
		}
		if p.AuthorID == in.ReviewerID {
			return E(ErrSelfReview, "")
		}
		// Determine the gate state the reviewer must be acting on.
		// First submission: 'pending'. Re-review of an edit to a live post:
		// the post remains 'published' while the new current version is pending
		// and the old published version keeps serving members.
		switch p.Status {
		case "pending":
			in.ExpectedStatus = "pending"
		case "published":
			if !p.PublishedVersionID.Valid ||
				(p.CurrentVersionID.Valid && p.CurrentVersionID.Int64 == p.PublishedVersionID.Int64) {
				_ = insertReviewLog(ctx, q, in, false)
				return E(ErrPostState, "post has no edit awaiting review")
			}
			in.ExpectedStatus = "published"
		default:
			_ = insertReviewLog(ctx, q, in, false)
			return E(ErrPostState, "post is not awaiting review")
		}
		if !p.CurrentVersionID.Valid || p.CurrentVersionID.Int64 != in.VersionID {
			// The approval targets a stale version: a newer edit exists.
			_ = insertReviewLog(ctx, q, in, false)
			return E(ErrVersionMismatch, "")
		}
		v, err := q.GetVersion(ctx, in.VersionID)
		if err != nil {
			return err
		}
		if v.PostID != in.PostID || v.ReviewStatus != "pending" {
			_ = insertReviewLog(ctx, q, in, false)
			return E(ErrVersionMismatch, "version is not awaiting review")
		}

		action := "reject"
		if in.Approve {
			action = "approve"
		}
		if in.Approve {
			post, err = q.ApproveVersion(ctx, sqlcgen.ApproveVersionParams{
				ID: in.PostID, CommunityID: in.CommunityID,
				VersionID:      toPgInt8(in.VersionID),
				ExpectedStatus: in.ExpectedStatus,
			})
			if err != nil {
				if errors.Is(err, pgx.ErrNoRows) {
					_ = insertReviewLog(ctx, q, in, false)
					return E(ErrVersionMismatch, "")
				}
				return err
			}
			if err := q.MarkVersionApproved(ctx, in.VersionID); err != nil {
				return err
			}
		} else {
			post, err = q.RejectPostToDraft(ctx,
				sqlcgen.RejectPostToDraftParams{ID: in.PostID, CommunityID: in.CommunityID})
			if err != nil {
				if errors.Is(err, pgx.ErrNoRows) {
					_ = insertReviewLog(ctx, q, in, false)
					return E(ErrPostState, "post is not awaiting review")
				}
				return err
			}
			if err := q.MarkVersionRejected(ctx, in.VersionID); err != nil {
				return err
			}
			// On rejection of a re-edit, members keep the old published version
			// and current pointer should fall back to it.
			if in.ExpectedStatus == "published" && post.PublishedVersionID.Valid {
				if err := setPostCurrent(ctx, q, post.ID, post.PublishedVersionID.Int64); err != nil {
					return err
				}
				post, err = q.GetPost(ctx, post.ID)
				if err != nil {
					return err
				}
			}
		}
		in.Approve = action == "approve"
		return insertReviewLog(ctx, q, in, true)
	})
	return post, err
}

func insertReviewLog(ctx context.Context, q *sqlcgen.Queries, in ReviewAction, success bool) error {
	action := "reject"
	if in.Approve {
		action = "approve"
	}
	_, err := q.InsertReview(ctx, sqlcgen.InsertReviewParams{
		PostID: in.PostID, VersionID: in.VersionID, ReviewerID: in.ReviewerID,
		Action: action, Reason: in.Reason, ExpectedStatus: in.ExpectedStatus,
		Success: success,
	})
	return err
}

// Takedown immediately removes a published post. Access is revoked in the same
// statement by clearing published_version_id and setting status='removed'.
func (s *PostService) Takedown(ctx context.Context, postID, communityID, actorID int64,
	reason string, includePending bool) (sqlcgen.Post, error) {
	var post sqlcgen.Post
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		p, err := q.GetPostForUpdate(ctx, postID)
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "post")
		} else if err != nil {
			return err
		}
		if p.CommunityID != communityID {
			return E(ErrNotFound, "post not in community")
		}
		if p.AuthorID == actorID {
			return E(ErrForbidden, "moderators cannot take down their own posts")
		}
		if p.Status == "published" {
			post, err = q.TakedownPublishedPost(ctx, sqlcgen.TakedownPublishedPostParams{
				ID: postID, CommunityID: communityID, Reason: reason,
			})
		} else if includePending && p.Status == "pending" {
			post, err = q.TakedownPost(ctx, sqlcgen.TakedownPostParams{
				ID: postID, CommunityID: communityID, Reason: reason,
			})
		} else {
			return E(ErrPostState, "post is not published")
		}
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return E(ErrPostState, "post is not published")
			}
			return err
		}
		versionID := int64(0)
		if p.PublishedVersionID.Valid {
			versionID = p.PublishedVersionID.Int64
		} else if p.CurrentVersionID.Valid {
			versionID = p.CurrentVersionID.Int64
		}
		_, err = q.InsertReview(ctx, sqlcgen.InsertReviewParams{
			PostID: postID, VersionID: versionID, ReviewerID: actorID,
			Action: "takedown", Reason: reason, ExpectedStatus: p.Status, Success: true,
		})
		return err
	})
	return post, err
}

// Restore re-publishes an approved version of a removed post. It refuses when
// any version newer than the target exists, so restoring an old snapshot can
// never overwrite later modifications. Actor and basis are recorded.
func (s *PostService) Restore(ctx context.Context, postID, communityID, actorID, versionID int64,
	reason string) (sqlcgen.Post, error) {
	var post sqlcgen.Post
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		p, err := q.GetPostForUpdate(ctx, postID)
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "post")
		} else if err != nil {
			return err
		}
		if p.CommunityID != communityID {
			return E(ErrNotFound, "post not in community")
		}
		if p.Status != "removed" {
			return E(ErrPostState, "only removed posts can be restored")
		}
		v, err := q.GetVersion(ctx, versionID)
		if errors.Is(err, pgx.ErrNoRows) || v.PostID != postID {
			return E(ErrNotFound, "version")
		} else if err != nil {
			return err
		}
		if v.ReviewStatus != "approved" {
			return E(ErrConflict, "only approved versions can be restored")
		}
		latest, err := q.NextVersionNumber(ctx, postID)
		if err != nil {
			return err
		}
		if int32(latest) == v.VersionNumber+1 {
			// Target is already the newest version: re-point at it directly.
			post, err = q.RestoreOldVersion(ctx, sqlcgen.RestoreOldVersionParams{
				ID: postID, CommunityID: communityID, VersionID: toPgInt8(versionID),
			})
			if err != nil {
				return err
			}
		} else {
			// A newer modification exists. Restoring must NOT overwrite it:
			// instead mint a fresh immutable version carrying the old content,
			// approved by this moderator action. The newer versions remain in
			// history untouched.
			nv, err := q.InsertVersion(ctx, sqlcgen.InsertVersionParams{
				PostID: postID, VersionNumber: int32(latest),
				Title: v.Title, Body: v.Body,
				RequiredTierLevel: v.RequiredTierLevel, AuthorID: p.AuthorID,
				ReviewStatus: "approved",
			})
			if err != nil {
				return err
			}
			post, err = q.RestoreOldVersion(ctx, sqlcgen.RestoreOldVersionParams{
				ID: postID, CommunityID: communityID, VersionID: toPgInt8(nv.ID),
			})
			if err != nil {
				return err
			}
			versionID = nv.ID
		}
		_, err = q.InsertReview(ctx, sqlcgen.InsertReviewParams{
			PostID: post.ID, VersionID: versionID, ReviewerID: actorID,
			Action: "approve", Reason: "restore: " + reason,
			ExpectedStatus: "removed", Success: true,
		})
		return err
	})
	return post, err
}

// AddAttachment stores an attachment against an immutable version. Only the
// author may attach, and only to the current draft/pending version they own.
func (s *PostService) AddAttachment(ctx context.Context, versionID, authorID int64,
	filename, contentType string, data []byte) (sqlcgen.Attachment, error) {
	var att sqlcgen.Attachment
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		v, err := q.GetVersion(ctx, versionID)
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "version")
		} else if err != nil {
			return err
		}
		if v.AuthorID != authorID {
			return E(ErrForbidden, "only the author can attach files")
		}
		att, err = q.AddAttachment(ctx, sqlcgen.AddAttachmentParams{
			VersionID: versionID, Filename: filename, ContentType: contentType, Data: data,
		})
		return err
	})
	return att, err
}

// ReadContext describes what a viewer wants to read.
type ReadContext struct {
	CommunityID int64
	User        middleware.CurrentUser
	IsReviewer  bool
}

// AuthorizeVersion is THE single access gate used by body, attachment and
// export handlers, guaranteeing identical isolation across all three.
//
//	member  -> approved version of a live post, tier >= version's level,
//	           membership active and unexpired (SQL clock, evaluated now)
//	reviewer/admin -> any version inside the community (moderation only;
//	           reviewers have no renewal/payment power)
func (s *PostService) AuthorizeVersion(ctx context.Context, versionID int64, rc ReadContext) (sqlcgen.PostVersion, error) {
	v, err := s.store.GetVersion(ctx, versionID)
	if errors.Is(err, pgx.ErrNoRows) {
		return sqlcgen.PostVersion{}, E(ErrNotFound, "version")
	} else if err != nil {
		return sqlcgen.PostVersion{}, err
	}
	p, err := s.store.GetPost(ctx, v.PostID)
	if err != nil {
		return sqlcgen.PostVersion{}, err
	}
	if p.CommunityID != rc.CommunityID {
		return sqlcgen.PostVersion{}, E(ErrNotFound, "not in community")
	}
	if rc.User.Role == "admin" || rc.IsReviewer {
		// Reviewers moderate but cannot profit from it; reading for review is
		// allowed across versions.
		return v, nil
	}
	// Author can always read own versions.
	if p.AuthorID == rc.User.ID {
		return v, nil
	}
	_, err = s.store.CheckVersionAccess(ctx,
		sqlcgen.CheckVersionAccessParams{ID: versionID, UserID: rc.User.ID})
	if errors.Is(err, pgx.ErrNoRows) {
		return sqlcgen.PostVersion{}, E(ErrForbidden, "membership required, tier too low or expired")
	}
	if err != nil {
		return sqlcgen.PostVersion{}, err
	}
	return v, nil
}

// Export bundles the body and all attachments of a version into a ZIP. It runs
// through the same AuthorizeVersion gate as body/attachment reads.
func (s *PostService) Export(ctx context.Context, versionID int64, rc ReadContext) (string, []byte, error) {
	v, err := s.AuthorizeVersion(ctx, versionID, rc)
	if err != nil {
		return "", nil, err
	}
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	w, err := zw.Create("body.md")
	if err != nil {
		return "", nil, err
	}
	if _, err := io.WriteString(w, fmt.Sprintf("# %s\n\n%s\n", v.Title, v.Body)); err != nil {
		return "", nil, err
	}
	atts, err := s.store.ListAttachments(ctx, versionID)
	if err != nil {
		return "", nil, err
	}
	for _, meta := range atts {
		full, err := s.store.GetAttachment(ctx, meta.ID)
		if err != nil {
			return "", nil, err
		}
		fw, err := zw.Create("attachments/" + full.Filename)
		if err != nil {
			return "", nil, err
		}
		if _, err := fw.Write(full.Data); err != nil {
			return "", nil, err
		}
	}
	if err := zw.Close(); err != nil {
		return "", nil, err
	}
	return fmt.Sprintf("post-v%d.zip", v.VersionNumber), buf.Bytes(), nil
}

func (s *PostService) GetPost(ctx context.Context, id int64) (sqlcgen.Post, error) {
	p, err := s.store.GetPost(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		return sqlcgen.Post{}, E(ErrNotFound, "post")
	}
	return p, err
}

func (s *PostService) GetVersion(ctx context.Context, id int64) (sqlcgen.PostVersion, error) {
	v, err := s.store.GetVersion(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		return sqlcgen.PostVersion{}, E(ErrNotFound, "version")
	}
	return v, err
}

func (s *PostService) ListVersions(ctx context.Context, postID int64) ([]sqlcgen.PostVersion, error) {
	return s.store.ListVersions(ctx, postID)
}

func (s *PostService) ListPosts(ctx context.Context, communityID int64, authorID int64,
	status string, limit, offset int32) ([]sqlcgen.Post, error) {
	params := sqlcgen.ListPostsParams{
		Limit: limit, Offset: offset,
		CommunityID: toPgInt8(communityID),
	}
	if authorID > 0 {
		params.AuthorID = toPgInt8(authorID)
	}
	if status != "" {
		params.Status = pgtype.Text{String: status, Valid: true}
	}
	return s.store.ListPosts(ctx, params)
}

func (s *PostService) GetAttachmentForDownload(ctx context.Context, attachmentID int64,
	rc ReadContext) (sqlcgen.Attachment, error) {
	att, err := s.store.GetAttachment(ctx, attachmentID)
	if errors.Is(err, pgx.ErrNoRows) {
		return sqlcgen.Attachment{}, E(ErrNotFound, "attachment")
	} else if err != nil {
		return sqlcgen.Attachment{}, err
	}
	if _, err := s.AuthorizeVersion(ctx, att.VersionID, rc); err != nil {
		return sqlcgen.Attachment{}, err
	}
	return att, nil
}
