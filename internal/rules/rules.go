// Package rules implements versioned sensitive-word rules.
//
// Concurrency contract: rule activation and publishing-time checks both
// acquire the same transaction-scoped advisory lock (LockRuleAdvisory) and
// run at serializable isolation. A submit/approve racing a rule switch
// therefore has exactly one deterministic outcome — it sees the single rule
// version that was active when the lock was acquired — rather than depending
// on statement timing.
package rules

import (
	"context"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"communityvault/internal/apperr"
	"communityvault/internal/db"
)

type Service struct {
	pool *pgxpool.Pool
}

func New(pool *pgxpool.Pool) *Service { return &Service{pool: pool} }

// Active returns the currently active rule version and its words.
func (s *Service) Active(ctx context.Context) (db.RuleVersion, []string, error) {
	q := db.New(s.pool)
	rv, err := q.GetActiveRule(ctx)
	if err != nil {
		return db.RuleVersion{}, nil, err
	}
	words, err := q.ListRuleWords(ctx, rv.ID)
	return rv, words, err
}

// Version returns one rule version by id.
func (s *Service) Version(ctx context.Context, id int64) (db.RuleVersion, []string, error) {
	q := db.New(s.pool)
	rv, err := q.GetRuleVersion(ctx, id)
	if err != nil {
		return db.RuleVersion{}, nil, apperr.ErrNotFound
	}
	words, err := q.ListRuleWords(ctx, rv.ID)
	return rv, words, err
}

func (s *Service) ListVersions(ctx context.Context) ([]db.RuleVersion, error) {
	return db.New(s.pool).ListRuleVersions(ctx)
}

// CreateVersion stores a new draft rule version with its words. It is not
// active until Activate is called, so staging words never affects publishes.
func (s *Service) CreateVersion(ctx context.Context, note string, words []string) (db.RuleVersion, error) {
	if len(words) == 0 {
		return db.RuleVersion{}, apperr.New(422, "validation_error", "a rule version needs at least one word")
	}
	var rv db.RuleVersion
	err := db.InTx(ctx, s.pool, func(_ pgx.Tx, q *db.Queries) error {
		maxV, err := q.MaxRuleVersion(ctx)
		if err != nil {
			return err
		}
		rv, err = q.CreateRuleVersion(ctx, db.CreateRuleVersionParams{
			Version: maxV + 1,
			Note:    note,
		})
		if err != nil {
			return err
		}
		for _, w := range dedupe(normalizeWords(words)) {
			if err := q.AddSensitiveWord(ctx, db.AddSensitiveWordParams{
				RuleVersionID: rv.ID,
				Word:          w,
			}); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return db.RuleVersion{}, err
	}
	return rv, nil
}

// Activate makes the given version the unique active version. The advisory
// lock means an in-flight publish check either fully completes against the
// previous rules or fully runs against the new ones.
func (s *Service) Activate(ctx context.Context, id int64) (db.RuleVersion, error) {
	var rv db.RuleVersion
	err := db.InTx(ctx, s.pool, func(_ pgx.Tx, q *db.Queries) error {
		if err := q.LockRuleAdvisory(ctx); err != nil {
			return err
		}
		var err error
		rv, err = q.GetRuleVersion(ctx, id)
		if err != nil {
			return apperr.ErrNotFound
		}
		if err := q.DeactivateActiveRules(ctx); err != nil {
			return err
		}
		if err := q.ActivateRule(ctx, id); err != nil {
			return err
		}
		rv.Active = true
		return nil
	})
	if err != nil {
		return db.RuleVersion{}, err
	}
	return rv, nil
}

// Check reports every active word found in body, lower-cased substring match.
func Contains(body string, words []string) []string {
	lower := strings.ToLower(body)
	var matched []string
	seen := map[string]bool{}
	for _, w := range words {
		w = strings.TrimSpace(strings.ToLower(w))
		if w == "" || seen[w] {
			continue
		}
		if strings.Contains(lower, w) {
			matched = append(matched, w)
			seen[w] = true
		}
	}
	return matched
}

func normalizeWords(words []string) []string {
	out := make([]string, 0, len(words))
	for _, w := range words {
		if w = strings.TrimSpace(strings.ToLower(w)); w != "" {
			out = append(out, w)
		}
	}
	return out
}

func dedupe(words []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, w := range words {
		if !seen[w] {
			seen[w] = true
			out = append(out, w)
		}
	}
	return out
}
