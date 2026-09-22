// Package content implements the content lifecycle:
// draft -> pending -> published, plus withdrawn, with every body change
// producing a new immutable revision.
package content

import (
	"context"
	"encoding/json"
	"errors"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"communityvault/internal/apperr"
	"communityvault/internal/db"
	"communityvault/internal/rules"
)

type Service struct {
	pool *pgxpool.Pool
	rs   *rules.Service
}

func New(pool *pgxpool.Pool, rs *rules.Service) *Service {
	return &Service{pool: pool, rs: rs}
}

// Create stores a draft with revision 1.
func (s *Service) Create(ctx context.Context, authorID, categoryID int64, title, body string) (db.Content, db.ContentRevision, error) {
	title, body = strings.TrimSpace(title), strings.TrimSpace(body)
	if title == "" || body == "" {
		return db.Content{}, db.ContentRevision{}, apperr.ErrValidation
	}
	var c db.Content
	var rev db.ContentRevision
	err := db.InTx(ctx, s.pool, func(_ pgx.Tx, q *db.Queries) error {
		if _, err := q.GetCategory(ctx, categoryID); err != nil {
			return apperr.New(422, "validation_error", "unknown category")
		}
		// Insert with a placeholder current_revision, then create revision 1
		// and point the content row at it.
		var err error
		c, err = q.CreateContent(ctx, db.CreateContentParams{
			CategoryID:      categoryID,
			AuthorID:        authorID,
			Title:           title,
			CurrentRevision: int64Ptr(0),
		})
		if err != nil {
			return err
		}
		rev, err = q.CreateRevision(ctx, db.CreateRevisionParams{
			ContentID:  c.ID,
			RevisionNo: 1,
			Body:       body,
			EditReason: "initial draft",
			Origin:     "edit",
			AuthorID:   authorID,
		})
		if err != nil {
			return err
		}
		if err := q.SetCurrentRevision(ctx, db.SetCurrentRevisionParams{
			ID: c.ID, CurrentRevision: &rev.ID,
		}); err != nil {
			return err
		}
		c.CurrentRevision = &rev.ID
		return q.InsertEvent(ctx, db.InsertEventParams{
			ContentID: c.ID, RevisionID: &rev.ID, ActorID: &authorID,
			Event: "created", ToStatus: strPtr("draft"),
		})
	})
	return c, rev, err
}

// Edit appends a new revision to a draft. Only the author may edit, and the
// content must be a draft: editing pending/published text is impossible, so
// an old approval can never silently cover a new body.
func (s *Service) Edit(ctx context.Context, authorID, contentID int64, body, reason string) (db.ContentRevision, error) {
	body, reason = strings.TrimSpace(body), strings.TrimSpace(reason)
	if body == "" {
		return db.ContentRevision{}, apperr.ErrValidation
	}
	var rev db.ContentRevision
	err := db.InTx(ctx, s.pool, func(_ pgx.Tx, q *db.Queries) error {
		c, err := q.GetContentForUpdate(ctx, contentID)
		if err != nil {
			return apperr.ErrNotFound
		}
		if c.AuthorID != authorID {
			return apperr.ErrForbidden
		}
		if c.Status != "draft" {
			return apperr.New(409, "not_editable",
				"only draft content can be edited; withdraw or wait for review")
		}
		return s.appendRevision(ctx, q, c, body, reason, "edit", nil, authorID, &rev)
	})
	return rev, err
}

// Submit runs the active sensitive-word rules and moves a draft to pending,
// opening (or refreshing) its review task bound to the current revision.
// The advisory lock makes a concurrent rule switch deterministic.
func (s *Service) Submit(ctx context.Context, authorID, contentID int64) (db.Content, []string, db.RuleVersion, error) {
	var c db.Content
	var matched []string
	var rv db.RuleVersion
	// A rule hit is a committed outcome (the rule_checked evidence must be
	// retained), so it is signaled post-commit rather than rolling the tx
	// back.
	var ruleErr error
	err := db.InTx(ctx, s.pool, func(_ pgx.Tx, q *db.Queries) error {
		if err := q.LockRuleAdvisory(ctx); err != nil {
			return err
		}
		var err error
		c, err = q.GetContentForUpdate(ctx, contentID)
		if err != nil {
			return apperr.ErrNotFound
		}
		if c.AuthorID != authorID {
			return apperr.ErrForbidden
		}
		if c.Status != "draft" {
			return apperr.New(409, "not_draft", "only draft content can be submitted")
		}
		rev, err := q.GetRevision(ctx, *c.CurrentRevision)
		if err != nil {
			return err
		}
		rv, err = q.GetActiveRule(ctx)
		if err != nil {
			return apperr.New(500, "no_active_rule", "no active rule version")
		}
		words, err := q.ListRuleWords(ctx, rv.ID)
		if err != nil {
			return err
		}
		matched = rules.Contains(rev.Body, words)
		evidence, _ := json.Marshal(matched)
		if len(matched) > 0 {
			_ = q.InsertEvent(ctx, db.InsertEventParams{
				ContentID: c.ID, RevisionID: &rev.ID, ActorID: &authorID,
				Event: "rule_checked", Reason: string(evidence),
			})
			ruleErr = apperr.ErrSensitiveWords
			return nil // commit the evidence; content stays draft
		}
		from := c.Status
		if err := q.SetContentStatus(ctx, db.SetContentStatusParams{
			ID: c.ID, Status: "pending",
		}); err != nil {
			return err
		}
		c.Status = "pending"
		// Clear any claim left from a previous (cancelled/done) task before
		// reopening it, so InsertClaim at claim time never collides.
		if err := q.DeleteClaimForContent(ctx, c.ID); err != nil {
			return err
		}
		if err := q.UpsertPendingTask(ctx, db.UpsertPendingTaskParams{
			ContentID: c.ID, RevisionID: rev.ID,
		}); err != nil {
			return err
		}
		return q.InsertEvent(ctx, db.InsertEventParams{
			ContentID: c.ID, RevisionID: &rev.ID, ActorID: &authorID,
			Event: "submitted", FromStatus: &from, ToStatus: strPtr("pending"),
			Reason: "submitted for review against rule v" + itoa(rv.Version),
		})
	})
	if err != nil {
		return c, matched, rv, err
	}
	return c, matched, rv, ruleErr
}

// Withdraw moves pending or published content to withdrawn and cancels its
// queue task. Authors may withdraw their own content; moderators may
// withdraw in their categories. A reason is always retained.
func (s *Service) Withdraw(ctx context.Context, actor db.User, contentID int64, reason string, modCategories []int64) (db.Content, error) {
	reason = strings.TrimSpace(reason)
	if reason == "" {
		return db.Content{}, apperr.New(422, "reason_required", "a withdrawal reason is required")
	}
	var c db.Content
	err := db.InTx(ctx, s.pool, func(_ pgx.Tx, q *db.Queries) error {
		var err error
		c, err = q.GetContentForUpdate(ctx, contentID)
		if err != nil {
			return apperr.ErrNotFound
		}
		if !canModerate(actor, c, modCategories) && c.AuthorID != actor.ID {
			return apperr.ErrForbidden
		}
		if c.Status != "pending" && c.Status != "published" {
			return apperr.New(409, "not_withdrawable", "only pending or published content can be withdrawn")
		}
		from := c.Status
		if err := q.CancelTaskForContent(ctx, c.ID); err != nil {
			return err
		}
		// Remove any open claim too: otherwise a re-submit after rollback
		// would try to insert a claim onto a task whose old claim still
		// exists (PK violation). The cancelled task can never be completed.
		if err := q.DeleteClaimForContent(ctx, c.ID); err != nil {
			return err
		}
		if err := q.SetContentStatus(ctx, db.SetContentStatusParams{ID: c.ID, Status: "withdrawn"}); err != nil {
			return err
		}
		c.Status = "withdrawn"
		return q.InsertEvent(ctx, db.InsertEventParams{
			ContentID: c.ID, RevisionID: c.CurrentRevision, ActorID: &actor.ID,
			Event: "withdrawn", FromStatus: &from, ToStatus: strPtr("withdrawn"), Reason: reason,
		})
	})
	return c, err
}

// Rollback creates a NEW revision whose body is copied from an older one.
// History is never rewritten or deleted. Draft stays draft; withdrawn content
// returns to draft and must be resubmitted and re-approved.
func (s *Service) Rollback(ctx context.Context, authorID, contentID int64, targetRevisionNo int32, reason string) (db.ContentRevision, db.Content, error) {
	reason = strings.TrimSpace(reason)
	if reason == "" {
		return db.ContentRevision{}, db.Content{}, apperr.New(422, "reason_required", "a rollback reason is required")
	}
	var rev db.ContentRevision
	var c db.Content
	err := db.InTx(ctx, s.pool, func(_ pgx.Tx, q *db.Queries) error {
		var err error
		c, err = q.GetContentForUpdate(ctx, contentID)
		if err != nil {
			return apperr.ErrNotFound
		}
		if c.AuthorID != authorID {
			return apperr.ErrForbidden
		}
		if c.Status != "draft" && c.Status != "withdrawn" {
			return apperr.New(409, "not_rollbackable",
				"withdraw the content before rolling it back")
		}
		target, err := q.GetRevisionByNo(ctx, db.GetRevisionByNoParams{
			ContentID: contentID, RevisionNo: targetRevisionNo,
		})
		if err != nil {
			return apperr.New(404, "revision_not_found", "target revision does not exist")
		}
		from := c.Status
		if err := s.appendRevision(ctx, q, c, target.Body, reason, "rollback", &target.ID, authorID, &rev); err != nil {
			return err
		}
		if c.Status == "withdrawn" {
			if err := q.SetContentStatus(ctx, db.SetContentStatusParams{ID: c.ID, Status: "draft"}); err != nil {
				return err
			}
			c.Status = "draft"
		}
		return q.InsertEvent(ctx, db.InsertEventParams{
			ContentID: c.ID, RevisionID: &rev.ID, ActorID: &authorID,
			Event: "rolled_back", FromStatus: &from, ToStatus: strPtr(c.Status),
			Reason: reason,
		})
	})
	return rev, c, err
}

func (s *Service) appendRevision(ctx context.Context, q *db.Queries, c db.Content, body, reason, origin string, sourceID *int64, authorID int64, out *db.ContentRevision) error {
	maxNo, err := q.MaxRevisionNo(ctx, c.ID)
	if err != nil {
		return err
	}
	rev, err := q.CreateRevision(ctx, db.CreateRevisionParams{
		ContentID: c.ID, RevisionNo: maxNo + 1, Body: body,
		EditReason: reason, Origin: origin, SourceRevisionID: sourceID,
		AuthorID: authorID,
	})
	if err != nil {
		return err
	}
	if err := q.SetCurrentRevision(ctx, db.SetCurrentRevisionParams{
		ID: c.ID, CurrentRevision: &rev.ID,
	}); err != nil {
		return err
	}
	c.CurrentRevision = &rev.ID
	*out = rev
	return nil
}

// canModerate reports whether a moderator/admin actor may act on content in
// its category. Members never pass.
func canModerate(actor db.User, c db.Content, modCategories []int64) bool {
	if actor.Role == "admin" {
		return true
	}
	if actor.Role != "moderator" {
		return false
	}
	for _, id := range modCategories {
		if id == c.CategoryID {
			return true
		}
	}
	return false
}

func strPtr(s string) *string { return &s }

func int64Ptr(v int64) *int64 { return &v }

func itoa(v int32) string { return strconv.Itoa(int(v)) }

// ErrNoRows maps pgx no-rows onto the domain not-found error.
func ErrNoRows(err error) bool { return errors.Is(err, pgx.ErrNoRows) }
