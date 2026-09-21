package service

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	db "communityvault/internal/db"
)

// Sentinel errors mapped to HTTP status codes by the handlers.
var (
	ErrNotFound        = errors.New("not found")
	ErrConflict        = errors.New("conflict")
	ErrForbidden       = errors.New("forbidden")
	ErrValidation      = errors.New("validation error")
	ErrNoTask          = errors.New("no review task available")
	ErrStaleClaim      = errors.New("claim expired or owned by another moderator")
	ErrRuleViolation   = errors.New("content violates the active rule version")
	ErrAlreadyReported = errors.New("you have already reported this content")
)

// ruleLockKey is the single advisory-lock key that serializes rule activation
// against moderation decisions.
const ruleLockKey int64 = 0xC0_4E_17_01 // arbitrary constant, fixed for both paths

// Service holds the business logic.
type Service struct {
	pool     *pgxpool.Pool
	q        *db.Queries
	claimTTL time.Duration
}

func New(pool *pgxpool.Pool, claimTTL time.Duration) *Service {
	return &Service{pool: pool, q: db.New(pool), claimTTL: claimTTL}
}

// withTx runs fn inside a transaction using a transaction-bound Queries.
// Retriable serialization failures (deadlock / serialization failure) cause
// the whole transaction to be retried with a small backoff.
func (s *Service) withTx(ctx context.Context, fn func(q *db.Queries, tx pgx.Tx) error) error {
	const maxAttempts = 5
	var lastErr error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt) * 5 * time.Millisecond):
			}
		}
		lastErr = s.runTx(ctx, fn)
		if !isRetryable(lastErr) {
			return lastErr
		}
	}
	return lastErr
}

func (s *Service) runTx(ctx context.Context, fn func(q *db.Queries, tx pgx.Tx) error) error {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	err = fn(db.New(tx), tx)
	if err != nil {
		// commitWithError means "persist the work done so far, then surface
		// this business error to the caller" (used by submit-time rejection:
		// the evidence rows must survive even though the submit failed).
		var cwe *commitWithError
		if errors.As(err, &cwe) {
			if cerr := tx.Commit(ctx); cerr != nil {
				return cerr
			}
			return cwe.err
		}
		return err
	}
	return tx.Commit(ctx)
}

// isRetryable reports PostgreSQL serialization/deadlock errors that are safe
// to re-run because the transaction function is idempotent and short-lived.
func isRetryable(err error) bool {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		switch pgErr.Code {
		case "40P01", // deadlock_detected
			"40001", // serialization_failure
			"55P03": // lock_not_available
			return true
		}
	}
	return false
}

type commitWithError struct{ err error }

func (e *commitWithError) Error() string { return e.err.Error() }
func (e *commitWithError) Unwrap() error { return e.err }

// matchWords returns the first matching word (case-insensitive substring)
// against the given word list, or "" if none match.
func matchWords(body, title string, words []string) string {
	hay := strings.ToLower(title + "\n" + body)
	for _, w := range words {
		w = strings.TrimSpace(strings.ToLower(w))
		if w == "" {
			continue
		}
		if strings.Contains(hay, w) {
			return w
		}
	}
	return ""
}

// checkActiveRule loads the currently active rule version inside a tx and
// checks the title/body against its word list.
// Caller MUST hold the rule advisory lock when a decision is being recorded.
func checkActiveRule(ctx context.Context, q *db.Queries, title, body string) (db.RuleVersion, string, error) {
	rule, err := q.GetActiveRule(ctx)
	if err != nil {
		return db.RuleVersion{}, "", err
	}
	words, err := q.ListRuleWords(ctx, rule.ID)
	if err != nil {
		return db.RuleVersion{}, "", err
	}
	return rule, matchWords(body, title, words), nil
}
