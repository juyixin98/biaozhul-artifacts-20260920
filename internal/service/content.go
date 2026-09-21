package service

import (
	"context"
	"strings"

	"github.com/jackc/pgx/v5"

	"communityvault/internal/auth"
	db "communityvault/internal/db"
)

// ---------- request/response DTOs ----------

type CreateContentInput struct {
	Category   string `json:"category"`
	Title      string `json:"title"`
	Body       string `json:"body"`
	EditReason string `json:"edit_reason"`
}

type EditContentInput struct {
	Title      string `json:"title"`
	Body       string `json:"body"`
	EditReason string `json:"edit_reason"`
}

type ReasonInput struct {
	Reason string `json:"reason"`
}

type RevisionDTO struct {
	ID         int64  `json:"id"`
	RevisionNo int32  `json:"revision_no"`
	Body       string `json:"body,omitempty"` // omitted for readers who may not see it
	EditReason string `json:"edit_reason"`
	CreatedBy  int64  `json:"created_by"`
	CreatedAt  string `json:"created_at"`
}

type ContentDTO struct {
	ID                int64        `json:"id"`
	Category          string       `json:"category"`
	AuthorID          int64        `json:"author_id"`
	Title             string       `json:"title"`
	Status            string       `json:"status"`
	CurrentRevisionID *int64       `json:"current_revision_id"`
	CurrentRevision   *RevisionDTO `json:"current_revision,omitempty"`
	CreatedAt         string       `json:"created_at"`
	UpdatedAt         string       `json:"updated_at"`
}

// ---------- helpers ----------

func revisionDTO(r db.ContentRevision, includeBody bool) RevisionDTO {
	d := RevisionDTO{
		ID:         r.ID,
		RevisionNo: r.RevisionNo,
		EditReason: r.EditReason,
		CreatedBy:  r.CreatedBy,
		CreatedAt:  r.CreatedAt.Time.UTC().Format("2006-01-02T15:04:05.000Z"),
	}
	if includeBody {
		d.Body = r.Body
	}
	return d
}

func contentDTO(c db.Content, rev *db.ContentRevision, canSeeBody bool) ContentDTO {
	d := ContentDTO{
		ID:                c.ID,
		Category:          c.Category,
		AuthorID:          c.AuthorID,
		Title:             c.Title,
		Status:            c.Status,
		CurrentRevisionID: c.CurrentRevisionID,
		CreatedAt:         c.CreatedAt.Time.UTC().Format("2006-01-02T15:04:05.000Z"),
		UpdatedAt:         c.UpdatedAt.Time.UTC().Format("2006-01-02T15:04:05.000Z"),
	}
	if rev != nil {
		r := revisionDTO(*rev, canSeeBody)
		d.CurrentRevision = &r
	}
	return d
}

// addRevision appends an immutable revision, attaches it to the content,
// and writes a status event. Runs inside the caller's transaction.
func addRevision(ctx context.Context, q *db.Queries, c db.Content, body, reason string,
	actor *auth.Principal, fromStatus, toStatus string, ruleVersionID *int64) (db.ContentRevision, error) {

	nextNo, err := q.NextRevisionNo(ctx, c.ID)
	if err != nil {
		return db.ContentRevision{}, err
	}
	rev, err := q.InsertRevision(ctx, db.InsertRevisionParams{
		ContentID:  c.ID,
		RevisionNo: int32(nextNo + 1),
		Body:       body,
		EditReason: reason,
		CreatedBy:  actor.ID,
	})
	if err != nil {
		return db.ContentRevision{}, err
	}
	if err := q.AttachRevision(ctx, db.AttachRevisionParams{
		ID: c.ID, CurrentRevisionID: &rev.ID,
	}); err != nil {
		return db.ContentRevision{}, err
	}
	if fromStatus != toStatus {
		if err := q.SetContentStatus(ctx, db.SetContentStatusParams{
			ID: c.ID, Status: toStatus,
		}); err != nil {
			return db.ContentRevision{}, err
		}
		from := db.NullContentStatus{}
		if fromStatus != "" {
			from = db.NullContentStatus{ContentStatus: db.ContentStatus(fromStatus), Valid: true}
		}
		if _, err := q.InsertEvent(ctx, db.InsertEventParams{
			ContentID:     c.ID,
			RevisionID:    &rev.ID,
			RuleVersionID: ruleVersionID,
			FromStatus:    from,
			ToStatus:      toStatus,
			ActorID:       &actor.ID,
			ActorRole:     actor.Role,
			Reason:        reason,
		}); err != nil {
			return db.ContentRevision{}, err
		}
	}
	return rev, nil
}

func validatePost(category, title, body string) error {
	if strings.TrimSpace(category) == "" || strings.TrimSpace(title) == "" || strings.TrimSpace(body) == "" {
		return ErrValidation
	}
	return nil
}

// ---------- operations ----------

// Create makes a draft with revision #1.
func (s *Service) Create(ctx context.Context, actor *auth.Principal, in CreateContentInput) (ContentDTO, error) {
	if err := validatePost(in.Category, in.Title, in.Body); err != nil {
		return ContentDTO{}, err
	}
	var out ContentDTO
	err := s.withTx(ctx, func(q *db.Queries, _ pgx.Tx) error {
		c, err := q.CreateContent(ctx, db.CreateContentParams{
			Category: strings.TrimSpace(in.Category),
			AuthorID: actor.ID,
			Title:    strings.TrimSpace(in.Title),
		})
		if err != nil {
			return err
		}
		rev, err := addRevision(ctx, q, c, in.Body, in.EditReason, actor, "", "draft", nil)
		if err != nil {
			return err
		}
		c.Status = "draft"
		c.CurrentRevisionID = &rev.ID
		out = contentDTO(c, &rev, true)
		return nil
	})
	return out, err
}

// Edit creates a NEW revision. Editing pending content cancels its review
// task and returns the content to draft, so a stale approval can never publish
// the new body.
func (s *Service) Edit(ctx context.Context, actor *auth.Principal, contentID int64, in EditContentInput) (ContentDTO, error) {
	if strings.TrimSpace(in.Title) == "" || strings.TrimSpace(in.Body) == "" {
		return ContentDTO{}, ErrValidation
	}
	var out ContentDTO
	err := s.withTx(ctx, func(q *db.Queries, _ pgx.Tx) error {
		c, err := q.GetContentForUpdate(ctx, contentID)
		if err == pgx.ErrNoRows {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		if c.AuthorID != actor.ID && !actor.IsAdmin() {
			return ErrForbidden
		}
		if c.Status == "published" {
			return ErrConflict // published posts must be withdrawn before editing
		}
		c.Title = strings.TrimSpace(in.Title)
		if err := q.UpdateContentTitle(ctx, db.UpdateContentTitleParams{
			ID: contentID, Title: c.Title,
		}); err != nil {
			return err
		}
		oldTask, err := q.FindOpenTaskForContent(ctx, contentID)
		if err != nil && err != pgx.ErrNoRows {
			return err
		}
		if err == nil {
			if err := q.CancelOpenTask(ctx, oldTask.ID); err != nil {
				return err
			}
			// Evidence: why the pending review disappeared.
			if _, err := q.InsertDecision(ctx, db.InsertDecisionParams{
				ContentID: c.ID, RevisionID: oldTask.RevisionID,
				RuleVersionID: oldTask.RuleVersionID,
				State:         "cancelled", ModeratorID: nil,
				Reason: "author edited the content; review bound to the old revision was invalidated",
			}); err != nil {
				return err
			}
		}
		rev, err := addRevision(ctx, q, c, in.Body, in.EditReason, actor, c.Status, "draft", nil)
		if err != nil {
			return err
		}
		// record the cancellation as evidence on the old task
		c.Status = "draft"
		c.CurrentRevisionID = &rev.ID
		out = contentDTO(c, &rev, true)
		return nil
	})
	return out, err
}

// Submit sends the current revision to review against the ACTIVE rule version.
// If the current body already violates the rule it is rejected immediately.
func (s *Service) Submit(ctx context.Context, actor *auth.Principal, contentID int64) (ContentDTO, error) {
	var out ContentDTO
	err := s.withTx(ctx, func(q *db.Queries, _ pgx.Tx) error {
		c, err := q.GetContentForUpdate(ctx, contentID)
		if err == pgx.ErrNoRows {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		if c.AuthorID != actor.ID && !actor.IsAdmin() {
			return ErrForbidden
		}
		if c.Status != "draft" {
			return ErrConflict
		}
		rev, err := q.GetRevision(ctx, *c.CurrentRevisionID)
		if err != nil {
			return err
		}

		// Serialize against rule activation so the bound rule version is deterministic.
		if err := q.TakeRuleLock(ctx, ruleLockKey); err != nil {
			return err
		}
		rule, hit, err := checkActiveRule(ctx, q, c.Title, rev.Body)
		if err != nil {
			return err
		}
		if hit != "" {
			// Immediate rejection against the current rule version: the
			// content stays a draft. The rejection evidence (bound to both
			// the revision and the rule version) is recorded as a moderation
			// decision; no draft->draft status event is written. The evidence
			// rows must COMMIT even though the submit returns an error, hence
			// commitWithError.
			if _, err := q.InsertDecision(ctx, db.InsertDecisionParams{
				ContentID: c.ID, RevisionID: rev.ID, RuleVersionID: rule.ID,
				State: "rejected", ModeratorID: nil,
				Reason: "auto-rejected on submit: matched sensitive word \"" + hit + "\"",
			}); err != nil {
				return err
			}
			out = contentDTO(c, &rev, true)
			return &commitWithError{err: ErrRuleViolation}
		}

		if _, err := q.InsertReviewTask(ctx, db.InsertReviewTaskParams{
			ContentID: c.ID, RevisionID: rev.ID, RuleVersionID: rule.ID,
		}); err != nil {
			return err
		}
		if err := q.SetContentStatus(ctx, db.SetContentStatusParams{
			ID: c.ID, Status: "pending",
		}); err != nil {
			return err
		}
		if _, err := q.InsertEvent(ctx, db.InsertEventParams{
			ContentID: c.ID, RevisionID: &rev.ID, RuleVersionID: &rule.ID,
			FromStatus: db.NullContentStatus{ContentStatus: db.ContentStatusDraft, Valid: true},
			ToStatus:   "pending",
			ActorID:    &actor.ID, ActorRole: actor.Role,
			Reason: "submitted for review against rule version v" +
				itoa(int64(rule.Version)),
		}); err != nil {
			return err
		}
		c.Status = "pending"
		out = contentDTO(c, &rev, true)
		return nil
	})
	return out, err
}

// Rollback creates a NEW revision whose body is copied from an older one.
// History revisions are never mutated or deleted.
func (s *Service) Rollback(ctx context.Context, actor *auth.Principal, contentID, revisionID int64, reason string) (ContentDTO, error) {
	if strings.TrimSpace(reason) == "" {
		return ContentDTO{}, ErrValidation
	}
	var out ContentDTO
	err := s.withTx(ctx, func(q *db.Queries, _ pgx.Tx) error {
		c, err := q.GetContentForUpdate(ctx, contentID)
		if err == pgx.ErrNoRows {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		if c.AuthorID != actor.ID {
			return ErrForbidden
		}
		if c.Status != "draft" {
			return ErrConflict
		}
		target, err := q.GetRevision(ctx, revisionID)
		if err == pgx.ErrNoRows || target.ContentID != contentID {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		rev, err := addRevision(ctx, q, c, target.Body,
			"rollback to revision #"+itoa(int64(target.RevisionNo))+": "+reason,
			actor, "draft", "draft", nil)
		if err != nil {
			return err
		}
		c.CurrentRevisionID = &rev.ID
		out = contentDTO(c, &rev, true)
		return nil
	})
	return out, err
}

// Withdraw takes a published post down. Reason is mandatory and kept as evidence.
func (s *Service) Withdraw(ctx context.Context, actor *auth.Principal, contentID int64, reason string) (ContentDTO, error) {
	if strings.TrimSpace(reason) == "" {
		return ContentDTO{}, ErrValidation
	}
	var out ContentDTO
	err := s.withTx(ctx, func(q *db.Queries, _ pgx.Tx) error {
		c, err := q.GetContentForUpdate(ctx, contentID)
		if err == pgx.ErrNoRows {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		if c.AuthorID != actor.ID && !actor.CanModerate(c.Category) {
			return ErrForbidden
		}
		if c.Status != "published" {
			return ErrConflict
		}
		if err := q.SetContentStatus(ctx, db.SetContentStatusParams{
			ID: c.ID, Status: "withdrawn",
		}); err != nil {
			return err
		}
		from := db.NullContentStatus{ContentStatus: db.ContentStatusPublished, Valid: true}
		if _, err := q.InsertEvent(ctx, db.InsertEventParams{
			ContentID: c.ID, RevisionID: c.CurrentRevisionID,
			FromStatus: from, ToStatus: "withdrawn",
			ActorID: &actor.ID, ActorRole: actor.Role, Reason: reason,
		}); err != nil {
			return err
		}
		rev, _ := q.GetRevision(ctx, *c.CurrentRevisionID)
		c.Status = "withdrawn"
		out = contentDTO(c, &rev, c.AuthorID == actor.ID || actor.CanModerate(c.Category))
		return nil
	})
	return out, err
}

func itoa(n int64) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}
