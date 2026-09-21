package service

import (
	"context"
	"strings"

	"github.com/jackc/pgx/v5"

	"communityvault/internal/auth"
	db "communityvault/internal/db"
)

// CreateRuleVersionInput activates a brand-new rule version with the given
// words in one serialized transaction. The previous active version is archived
// atomically, so "the currently effective rule set" is always unambiguous.
type CreateRuleVersionInput struct {
	Description string   `json:"description"`
	Words       []string `json:"words"`
}

// RuleVersionDTO is the safe representation of a rule version.
type RuleVersionDTO struct {
	ID          int64    `json:"id"`
	Version     int32    `json:"version"`
	Status      string   `json:"status"`
	Description string   `json:"description"`
	Words       []string `json:"words,omitempty"`
	CreatedAt   string   `json:"created_at"`
	ActivatedAt string   `json:"activated_at"`
}

// CreateRuleVersion publishes a new active rule version. Admin only.
//
// Concurrency: it takes the same global advisory lock that moderation
// decisions take (ruleLockKey). Rule activation and a submit/approve therefore
// cannot interleave: every check observes a single, well-defined active
// version and binds its id onto the task/decision.
func (s *Service) CreateRuleVersion(ctx context.Context, actor *auth.Principal, in CreateRuleVersionInput) (RuleVersionDTO, error) {
	if !actor.IsAdmin() {
		return RuleVersionDTO{}, ErrForbidden
	}
	cleaned := make([]string, 0, len(in.Words))
	seen := map[string]bool{}
	for _, w := range in.Words {
		w = strings.ToLower(strings.TrimSpace(w))
		if w == "" || seen[w] {
			continue
		}
		seen[w] = true
		cleaned = append(cleaned, w)
	}
	var out RuleVersionDTO
	err := s.withTx(ctx, func(q *db.Queries, _ pgx.Tx) error {
		if err := q.TakeRuleLock(ctx, ruleLockKey); err != nil {
			return err
		}
		nextNo, err := q.NextRuleVersionNo(ctx)
		if err != nil {
			return err
		}
		// Archive the currently active version first; under the advisory lock
		// and inside this transaction no other reader can observe the gap.
		if err := q.ArchiveActiveRule(ctx); err != nil {
			return err
		}
		rv, err := q.InsertRuleVersion(ctx, db.InsertRuleVersionParams{
			Version:     int32(nextNo + 1),
			Description: in.Description,
			CreatedBy:   &actor.ID,
		})
		if err != nil {
			return err
		}
		for _, w := range cleaned {
			if err := q.InsertRuleWord(ctx, db.InsertRuleWordParams{
				RuleVersionID: rv.ID, Word: w,
			}); err != nil {
				return err
			}
		}
		out = RuleVersionDTO{
			ID: rv.ID, Version: rv.Version, Status: "active",
			Description: rv.Description, Words: cleaned,
			CreatedAt:   rv.CreatedAt.Time.UTC().Format("2006-01-02T15:04:05.000Z"),
			ActivatedAt: rv.ActivatedAt.Time.UTC().Format("2006-01-02T15:04:05.000Z"),
		}
		return nil
	})
	return out, err
}

// ActiveRule returns the single active rule version. Visible to moderators/admins.
func (s *Service) ActiveRule(ctx context.Context, actor *auth.Principal) (RuleVersionDTO, error) {
	if !actor.IsAdmin() && !actor.IsModerator() {
		return RuleVersionDTO{}, ErrForbidden
	}
	rv, err := s.q.GetActiveRule(ctx)
	if err == pgx.ErrNoRows {
		return RuleVersionDTO{}, ErrNotFound
	}
	if err != nil {
		return RuleVersionDTO{}, err
	}
	words, err := s.q.ListRuleWords(ctx, rv.ID)
	if err != nil {
		return RuleVersionDTO{}, err
	}
	return RuleVersionDTO{
		ID: rv.ID, Version: rv.Version, Status: rv.Status,
		Description: rv.Description, Words: words,
		CreatedAt:   rv.CreatedAt.Time.UTC().Format("2006-01-02T15:04:05.000Z"),
		ActivatedAt: rv.ActivatedAt.Time.UTC().Format("2006-01-02T15:04:05.000Z"),
	}, nil
}

// ListRules returns all rule versions, newest first. Admin only. Words are
// included so the evidence behind past decisions is fully auditable.
func (s *Service) ListRules(ctx context.Context, actor *auth.Principal) ([]RuleVersionDTO, error) {
	if !actor.IsAdmin() {
		return nil, ErrForbidden
	}
	rvs, err := s.q.ListRuleVersions(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]RuleVersionDTO, 0, len(rvs))
	for _, rv := range rvs {
		words, err := s.q.ListRuleWords(ctx, rv.ID)
		if err != nil {
			return nil, err
		}
		out = append(out, RuleVersionDTO{
			ID: rv.ID, Version: rv.Version, Status: rv.Status,
			Description: rv.Description, Words: words,
			CreatedAt:   rv.CreatedAt.Time.UTC().Format("2006-01-02T15:04:05.000Z"),
			ActivatedAt: rv.ActivatedAt.Time.UTC().Format("2006-01-02T15:04:05.000Z"),
		})
	}
	return out, nil
}
