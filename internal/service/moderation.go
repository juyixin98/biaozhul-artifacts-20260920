package service

import (
	"context"
	"strings"

	"github.com/jackc/pgx/v5"

	"communityvault/internal/auth"
	db "communityvault/internal/db"
)

// ClaimedTask is returned when a moderator successfully claims a queue item.
type ClaimedTask struct {
	TaskID         int64  `json:"task_id"`
	ContentID      int64  `json:"content_id"`
	RevisionID     int64  `json:"revision_id"`
	RuleVersionID  int64  `json:"rule_version_id"`
	State          string `json:"state"`
	ClaimedBy      int64  `json:"claimed_by"`
	ClaimExpiresAt string `json:"claim_expires_at"`
	Title          string `json:"title"`
	Body           string `json:"body"`
	Category       string `json:"category"`
	AuthorID       int64  `json:"author_id"`
}

// RequeueExpired is meant to be run periodically; it also runs before a claim.
func (s *Service) RequeueExpired(ctx context.Context) (int32, error) {
	rows, err := s.q.RequeueExpiredClaims(ctx)
	return int32(len(rows)), err
}

// ListQueue shows pending (or expired-claim) tasks visible to the moderator.
func (s *Service) ListQueue(ctx context.Context, actor *auth.Principal, limit int32) ([]ClaimedTask, error) {
	if limit <= 0 || limit > 100 {
		limit = 50
	}
	// sqlc query supports a single-category filter; load all open tasks and
	// filter by the moderator's scope set in Go.
	rows, err := s.q.ListQueueTasks(ctx, db.ListQueueTasksParams{Column1: "", Limit: limit})
	if err != nil {
		return nil, err
	}
	out := make([]ClaimedTask, 0, len(rows))
	for _, t := range rows {
		c, err := s.q.GetContent(ctx, t.ContentID)
		if err != nil {
			continue // content gone
		}
		if !actor.CanModerate(c.Category) {
			continue
		}
		out = append(out, taskSummary(t, c))
	}
	return out, nil
}

func taskSummary(t db.ReviewTask, c db.Content) ClaimedTask {
	return ClaimedTask{
		TaskID:        t.ID,
		ContentID:     t.ContentID,
		RevisionID:    t.RevisionID,
		RuleVersionID: t.RuleVersionID,
		State:         t.State,
		Category:      c.Category,
		AuthorID:      c.AuthorID,
		Title:         c.Title,
	}
}

// Claim atomically takes the next available task for this moderator's scope.
// Uses FOR UPDATE SKIP LOCKED so concurrent claimants never get the same task.
func (s *Service) Claim(ctx context.Context, actor *auth.Principal) (ClaimedTask, error) {
	var out ClaimedTask
	var cats []string
	if !actor.IsAdmin() {
		for cat := range actor.Scopes {
			cats = append(cats, cat)
		}
		if len(cats) == 0 {
			return ClaimedTask{}, ErrNoTask
		}
	}
	err := s.withTx(ctx, func(q *db.Queries, _ pgx.Tx) error {
		// Opportunistically expire stale claims first.
		if _, err := q.RequeueExpiredClaims(ctx); err != nil {
			return err
		}
		task, err := q.ClaimPendingTask(ctx, db.ClaimPendingTaskParams{
			ClaimedBy:  &actor.ID,
			Secs:       s.claimTTL.Seconds(),
			Categories: cats, // nil = all categories for admins
		})
		if err == pgx.ErrNoRows {
			return ErrNoTask
		}
		if err != nil {
			return err
		}
		c, err := q.GetContent(ctx, task.ContentID)
		if err != nil {
			return err
		}
		rev, err := q.GetRevision(ctx, task.RevisionID)
		if err != nil {
			return err
		}
		out = taskSummary(task, c)
		out.Body = rev.Body
		out.ClaimedBy = actor.ID
		if task.ClaimExpiresAt.Valid {
			out.ClaimExpiresAt = task.ClaimExpiresAt.Time.UTC().Format("2006-01-02T15:04:05.000Z")
		}
		return nil
	})
	return out, err
}

// Approve finalizes a claimed task and publishes the bound revision.
// The publish is only valid for the exact revision the decision is bound to.
func (s *Service) Approve(ctx context.Context, actor *auth.Principal, taskID int64, reason string) (ContentDTO, error) {
	return s.decide(ctx, actor, taskID, reason, "approved")
}

// Reject finalizes a claimed task with a mandatory reason; content goes back to draft.
func (s *Service) Reject(ctx context.Context, actor *auth.Principal, taskID int64, reason string) (ContentDTO, error) {
	if strings.TrimSpace(reason) == "" {
		return ContentDTO{}, ErrValidation
	}
	return s.decide(ctx, actor, taskID, reason, "rejected")
}

func (s *Service) decide(ctx context.Context, actor *auth.Principal, taskID int64, reason, state string) (ContentDTO, error) {
	var out ContentDTO
	err := s.withTx(ctx, func(q *db.Queries, _ pgx.Tx) error {
		// Serialize decisions against rule activation so the bound rule
		// version is always the one that was active at decision time.
		if err := q.TakeRuleLock(ctx, ruleLockKey); err != nil {
			return err
		}

		// Plain read of the task first, ONLY to resolve the content id.
		plainTask, err := q.GetReviewTask(ctx, taskID)
		if err == pgx.ErrNoRows {
			return ErrNotFound
		}
		if err != nil {
			return err
		}

		// Lock ordering is content-row BEFORE task-row everywhere
		// (the author Edit path locks in the same order), which removes the
		// deadlock between concurrent edit and approve.
		c, err := q.GetContentForUpdate(ctx, plainTask.ContentID)
		if err != nil {
			return err
		}
		if !actor.CanModerate(c.Category) {
			return ErrForbidden
		}
		task, err := q.GetReviewTaskForUpdate(ctx, taskID)
		if err != nil {
			return err
		}
		_ = task

		// CompleteClaim enforces: state='claimed' AND claimed_by=me AND
		// claim_expires_at >= now(). An expired claimer therefore cannot
		// overwrite a later re-claim/decision.
		done, err := q.CompleteClaim(ctx, db.CompleteClaimParams{
			ID: taskID, DecidedBy: &actor.ID,
			State: state, DecisionReason: reason,
		})
		if err == pgx.ErrNoRows {
			return ErrStaleClaim
		}
		if err != nil {
			return err
		}

		// The decision is bound to the exact revision and rule version the
		// task was created against.
		if _, err := q.InsertDecision(ctx, db.InsertDecisionParams{
			ContentID: c.ID, RevisionID: done.RevisionID,
			RuleVersionID: done.RuleVersionID,
			State:         state, ModeratorID: &actor.ID, Reason: reason,
		}); err != nil {
			return err
		}

		from := db.NullContentStatus{ContentStatus: db.ContentStatusPending, Valid: true}
		if state == "approved" {
			// Publish ONLY if the content's current revision is still the
			// approved one. If the author edited meanwhile, the task would
			// have been cancelled, so reaching here means it is current.
			if c.CurrentRevisionID == nil || *c.CurrentRevisionID != done.RevisionID {
				return ErrConflict
			}
			if err := q.PublishContent(ctx, db.PublishContentParams{
				ID: c.ID, CurrentRevisionID: &done.RevisionID,
			}); err != nil {
				return err
			}
			if _, err := q.InsertEvent(ctx, db.InsertEventParams{
				ContentID: c.ID, RevisionID: &done.RevisionID,
				RuleVersionID: &done.RuleVersionID,
				FromStatus:    from, ToStatus: "published",
				ActorID: &actor.ID, ActorRole: actor.Role, Reason: reason,
			}); err != nil {
				return err
			}
			c.Status = "published"
		} else {
			if err := q.SetContentStatus(ctx, db.SetContentStatusParams{
				ID: c.ID, Status: "draft",
			}); err != nil {
				return err
			}
			if _, err := q.InsertEvent(ctx, db.InsertEventParams{
				ContentID: c.ID, RevisionID: &done.RevisionID,
				RuleVersionID: &done.RuleVersionID,
				FromStatus:    from, ToStatus: "draft",
				ActorID: &actor.ID, ActorRole: actor.Role, Reason: reason,
			}); err != nil {
				return err
			}
			c.Status = "draft"
		}
		rev, err := q.GetRevision(ctx, done.RevisionID)
		if err != nil {
			return err
		}
		out = contentDTO(c, &rev, true)
		return nil
	})
	return out, err
}

// Decisions returns the moderation evidence (decision audit trail) for a post.
func (s *Service) Decisions(ctx context.Context, actor *auth.Principal, contentID int64) ([]db.ModerationDecision, error) {
	c, err := s.q.GetContent(ctx, contentID)
	if err == pgx.ErrNoRows {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	if !actor.CanModerate(c.Category) && c.AuthorID != actor.ID && !actor.IsAdmin() {
		return nil, ErrForbidden
	}
	return s.q.ListDecisionsForContent(ctx, contentID)
}

// Events returns the status-change audit trail.
func (s *Service) Events(ctx context.Context, actor *auth.Principal, contentID int64, limit int32) ([]db.StatusEvent, error) {
	c, err := s.q.GetContent(ctx, contentID)
	if err == pgx.ErrNoRows {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	if !actor.CanModerate(c.Category) && c.AuthorID != actor.ID && !actor.IsAdmin() {
		return nil, ErrForbidden
	}
	if limit <= 0 || limit > 200 {
		limit = 100
	}
	return s.q.ListEvents(ctx, db.ListEventsParams{ContentID: contentID, Limit: limit})
}
