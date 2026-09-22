// Package moderation implements reports, the claimable review queue and
// moderator decisions.
package moderation

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"communityvault/internal/apperr"
	"communityvault/internal/db"
	"communityvault/internal/pgtypex"
	"communityvault/internal/rules"
)

type Service struct {
	pool     *pgxpool.Pool
	rs       *rules.Service
	claimTTL time.Duration
}

func New(pool *pgxpool.Pool, rs *rules.Service, claimTTL time.Duration) *Service {
	return &Service{pool: pool, rs: rs, claimTTL: claimTTL}
}

// Report inserts a user->content report. The (reporter, content) unique
// constraint makes a repeat report a no-op conflict; it never increments a
// counter.
func (s *Service) Report(ctx context.Context, reporterID, contentID int64, reason string) (db.Report, error) {
	reason = strings.TrimSpace(reason)
	if reason == "" {
		return db.Report{}, apperr.ErrValidation
	}
	var r db.Report
	err := db.InTx(ctx, s.pool, func(_ pgx.Tx, q *db.Queries) error {
		c, err := q.GetContent(ctx, contentID)
		if err != nil {
			return apperr.ErrNotFound
		}
		if c.AuthorID == reporterID {
			return apperr.New(422, "self_report", "authors cannot report their own content")
		}
		r, err = q.InsertReport(ctx, db.InsertReportParams{
			ContentID: contentID, ReporterID: reporterID, Reason: reason,
		})
		if err != nil {
			if isUniqueViolation(err) {
				return apperr.ErrDuplicateReport
			}
			return err
		}
		return nil
	})
	return r, err
}

// Claim atomically recycles expired claims and hands one open task to the
// moderator. FOR UPDATE SKIP LOCKED means concurrent claimants never get the
// same task. categoryIDs nil means an admin may claim from any category.
// READ COMMITTED is sufficient (and high-throughput): the SKIP LOCKED row
// lock plus the conditional MarkTaskClaimed guard guarantee exclusivity.
func (s *Service) Claim(ctx context.Context, mod db.User, categoryIDs []int64) (db.ReviewTask, db.ReviewClaim, error) {
	var task db.ReviewTask
	var claim db.ReviewClaim
	err := db.InTxRC(ctx, s.pool, func(_ pgx.Tx, q *db.Queries) error {
		// Step 1: synchronously recycle expired claims inside this
		// transaction. The UPDATE ... FROM only touches claimed tasks with a
		// past expiry, so a live claimant is never disturbed.
		expired, err := q.RecycleExpiredClaims(ctx)
		if err != nil {
			return err
		}
		for _, tid := range expired {
			if err := q.DeleteClaim(ctx, tid); err != nil {
				return err
			}
		}
		// Step 2: pick one open row-lock; SKIP LOCKED guarantees two
		// concurrent claimants never select the same task.
		if mod.Role == "admin" {
			task, err = q.ClaimNextTaskAdmin(ctx)
		} else {
			if len(categoryIDs) == 0 {
				return apperr.ErrForbidden
			}
			task, err = q.ClaimNextTaskModerator(ctx, categoryIDs)
		}
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return apperr.ErrNoTask
			}
			return err
		}
		// Step 3: flip status conditionally (defends against a same-instant
		// recycler that had already reopened it).
		task, err = q.MarkTaskClaimed(ctx, task.ID)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return apperr.ErrNoTask
			}
			return err
		}
		expires := time.Now().Add(s.claimTTL)
		if err := q.InsertClaim(ctx, db.InsertClaimParams{
			TaskID: task.ID, ClaimantID: mod.ID,
			ExpiresAt: pgTimestamptz(expires),
		}); err != nil {
			return err
		}
		claim = db.ReviewClaim{
			TaskID: task.ID, ClaimantID: mod.ID,
			ClaimedAt: pgTimestamptz(time.Now()),
			ExpiresAt: pgTimestamptz(expires),
		}
		return nil
	})
	return task, claim, err
}

// Reject records a rejection bound to the claimed revision and the active
// rule version, returns the content to draft. A stale claim (expired,
// recycled, or revision changed) fails with ErrStaleClaim: the late
// moderator cannot overwrite a later resolution.
func (s *Service) Reject(ctx context.Context, mod db.User, taskID int64, categoryIDs []int64, reason string) (db.Review, error) {
	reason = strings.TrimSpace(reason)
	if reason == "" {
		return db.Review{}, apperr.New(422, "reason_required", "a rejection reason is required")
	}
	return s.decide(ctx, mod, taskID, categoryIDs, "rejected", reason)
}

// Approve re-validates the claimed revision against the rule version active
// at decision time (under the advisory lock), publishes it, and records the
// review with the matched-rule evidence. If newer rules now flag the body,
// the result is a rejection instead of a publish.
func (s *Service) Approve(ctx context.Context, mod db.User, taskID int64, categoryIDs []int64) (db.Review, error) {
	return s.decide(ctx, mod, taskID, categoryIDs, "approved", "")
}

func (s *Service) decide(ctx context.Context, mod db.User, taskID int64, categoryIDs []int64, decision, reason string) (db.Review, error) {
	var review db.Review
	err := db.InTx(ctx, s.pool, func(_ pgx.Tx, q *db.Queries) error {
		if err := q.LockRuleAdvisory(ctx); err != nil {
			return err
		}
		task, err := q.GetTaskForUpdate(ctx, taskID)
		if err != nil {
			return apperr.ErrNotFound
		}
		c, err := q.GetContentForUpdate(ctx, task.ContentID)
		if err != nil {
			return err
		}
		if mod.Role != "admin" {
			inCat := false
			for _, id := range categoryIDs {
				if id == c.CategoryID {
					inCat = true
				}
			}
			if mod.Role != "moderator" || !inCat {
				return apperr.ErrForbidden
			}
		}
		// Stale-claim guard: the conditional UPDATE flips the task only if
		// THIS moderator still owns a live claim on THIS revision.
		done, err := q.CompleteTaskWithGuard(ctx, db.CompleteTaskWithGuardParams{
			ID: taskID, ClaimantID: mod.ID, RevisionID: task.RevisionID,
		})
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return apperr.ErrStaleClaim
			}
			return err
		}
		task = done
		rev, err := q.GetRevision(ctx, task.RevisionID)
		if err != nil {
			return err
		}
		rv, err := q.GetActiveRule(ctx)
		if err != nil {
			return apperr.New(500, "no_active_rule", "no active rule version")
		}
		words, err := q.ListRuleWords(ctx, rv.ID)
		if err != nil {
			return err
		}
		matched := rules.Contains(rev.Body, words)
		evidence, _ := json.Marshal(matched)
		_ = q.InsertEvent(ctx, db.InsertEventParams{
			ContentID: c.ID, RevisionID: &rev.ID, ActorID: &mod.ID,
			Event: "rule_checked", Reason: string(evidence),
		})

		finalDecision := decision
		finalReason := reason
		if decision == "approved" && len(matched) > 0 {
			// Rules changed after submission: an approval cannot publish
			// content violating the currently active rules.
			finalDecision = "rejected"
			finalReason = "auto-rejected: body violates active rule v" + itoa(rv.Version)
		}

		review, err = q.InsertReview(ctx, db.InsertReviewParams{
			ContentID: c.ID, RevisionID: rev.ID, RuleVersionID: rv.ID,
			ReviewerID: mod.ID, Decision: finalDecision, Reason: finalReason,
			MatchedWords: pgtypex.StringList(matched),
		})
		if err != nil {
			if isUniqueViolation(err) {
				return apperr.New(409, "already_reviewed", "this revision was already reviewed")
			}
			return err
		}
		if err := q.DeleteClaim(ctx, taskID); err != nil {
			return err
		}
		from := c.Status
		if finalDecision == "approved" {
			if err := q.PublishRevision(ctx, db.PublishRevisionParams{
				ID: c.ID, CurrentRevision: &rev.ID,
			}); err != nil {
				return err
			}
			c.Status = "published"
			return q.InsertEvent(ctx, db.InsertEventParams{
				ContentID: c.ID, RevisionID: &rev.ID, ActorID: &mod.ID,
				Event: "approved", FromStatus: &from, ToStatus: strPtr("published"),
				Reason: "approved under rule v" + itoa(rv.Version),
			})
		}
		if err := q.SetContentStatus(ctx, db.SetContentStatusParams{ID: c.ID, Status: "draft"}); err != nil {
			return err
		}
		c.Status = "draft"
		return q.InsertEvent(ctx, db.InsertEventParams{
			ContentID: c.ID, RevisionID: &rev.ID, ActorID: &mod.ID,
			Event: "rejected", FromStatus: &from, ToStatus: strPtr("draft"), Reason: finalReason,
		})
	})
	return review, err
}

// Recycle reopens tasks whose claims have expired. Safe to run from a
// scheduler; claim acquisition also recycles inline, so this is best-effort.
// READ COMMITTED like Claim: the conditional UPDATE only touches expired
// rows and never blocks a live claimant.
func (s *Service) Recycle(ctx context.Context) (int, error) {
	n := 0
	err := db.InTxRC(ctx, s.pool, func(_ pgx.Tx, q *db.Queries) error {
		ids, err := q.RecycleExpiredClaims(ctx)
		if err != nil {
			return err
		}
		for _, id := range ids {
			if err := q.DeleteClaim(ctx, id); err != nil {
				return err
			}
		}
		n = len(ids)
		return nil
	})
	return n, err
}
