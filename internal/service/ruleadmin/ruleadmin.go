// Package ruleadmin manages versioned detection rule configuration. Every
// create is itself recorded in the append-only audit chain.
package ruleadmin

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"dams/internal/db"
	"dams/internal/platform/dbpool"
	"dams/internal/service/auditchain"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Actor struct {
	APIKeyID int64
	Label    string
}

type Service struct {
	Pool  *pgxpool.Pool
	Chain *auditchain.Service
}

// CreateInput is a rule configuration request.
type CreateInput struct {
	RuleType    string          `json:"rule_type"`
	Params      json.RawMessage `json:"params"`
	EffectiveAt *time.Time      `json:"effective_at,omitempty"`
}

func validate(ruleType string, raw json.RawMessage) error {
	var probe map[string]any
	if err := json.Unmarshal(raw, &probe); err != nil {
		return fmt.Errorf("params must be a JSON object: %w", err)
	}
	switch ruleType {
	case "frequency":
		var p struct {
			WindowSeconds *int     `json:"window_seconds"`
			Threshold     *int     `json:"threshold"`
			Actions       []string `json:"actions"`
		}
		if err := json.Unmarshal(raw, &p); err != nil {
			return err
		}
		if p.WindowSeconds != nil && *p.WindowSeconds <= 0 {
			return errors.New("window_seconds must be > 0")
		}
		if p.Threshold != nil && *p.Threshold <= 0 {
			return errors.New("threshold must be > 0")
		}
		for _, a := range p.Actions {
			if !validAction[a] {
				return fmt.Errorf("unknown action category %q", a)
			}
		}
	case "sensitive_hours":
		var p struct {
			SensitiveTables []struct {
				Schema string `json:"schema"`
				Table  string `json:"table"`
			} `json:"sensitive_tables"`
			AllowedStartHour *int `json:"allowed_start_hour"`
			AllowedEndHour   *int `json:"allowed_end_hour"`
			Actions          []string
		}
		if err := json.Unmarshal(raw, &p); err != nil {
			return err
		}
		if p.AllowedStartHour != nil && (*p.AllowedStartHour < 0 || *p.AllowedStartHour > 23) {
			return errors.New("allowed_start_hour must be 0..23")
		}
		if p.AllowedEndHour != nil && (*p.AllowedEndHour < 0 || *p.AllowedEndHour > 23) {
			return errors.New("allowed_end_hour must be 0..23")
		}
		if len(p.SensitiveTables) == 0 {
			return errors.New("sensitive_tables must contain at least one table")
		}
		for _, t := range p.SensitiveTables {
			if t.Schema == "" || t.Table == "" {
				return errors.New("each sensitive table needs schema and table")
			}
		}
	default:
		return fmt.Errorf("unknown rule_type %q", ruleType)
	}
	return nil
}

var validAction = map[string]bool{
	"select": true, "insert": true, "update": true, "delete": true,
	"ddl": true, "grant": true, "login": true, "other": true,
}

// ruleLockNS/Base key the transaction advisory lock used to serialize rule
// version creation per (org, rule type), so concurrent creates never pick the
// same version number or fight the partial unique index.
const (
	ruleLockNS   int64 = 0xD4A6
	lockTypeFreq int64 = 1
	lockTypeSens int64 = 2
)

// Create publishes a new rule version and deactivates prior versions of the
// same type. Alerts already bound to older versions keep pointing at them.
func (s *Service) Create(ctx context.Context, orgID int64, actor Actor, in CreateInput) (db.RuleVersion, error) {
	if err := validate(in.RuleType, in.Params); err != nil {
		return db.RuleVersion{}, err
	}
	lockKey := lockTypeFreq
	if in.RuleType == "sensitive_hours" {
		lockKey = lockTypeSens
	}
	var created db.RuleVersion
	err := dbpool.InTx(ctx, s.Pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock($1, $2)", ruleLockNS,
			orgID*10+lockKey); err != nil {
			return err
		}
		q := db.New(tx)
		nextVer, err := q.NextRuleVersion(ctx, db.NextRuleVersionParams{
			OrgID: orgID, RuleType: in.RuleType,
		})
		if err != nil {
			return err
		}
		eff := time.Now().UTC()
		if in.EffectiveAt != nil {
			eff = in.EffectiveAt.UTC()
		}
		// Deactivate first: the partial unique index allows only one active
		// version per org+type, and the new row is inserted afterwards.
		if err := q.DeactivateAllActiveRules(ctx, db.DeactivateAllActiveRulesParams{
			OrgID: orgID, RuleType: in.RuleType,
		}); err != nil {
			return err
		}
		row, err := q.CreateRuleVersion(ctx, db.CreateRuleVersionParams{
			OrgID:       orgID,
			RuleType:    in.RuleType,
			Version:     nextVer,
			IsActive:    true,
			Params:      []byte(in.Params),
			EffectiveAt: pgtype.Timestamptz{Time: eff, Valid: true},
			CreatedBy:   pgtype.Int8{},
		})
		if err != nil {
			return err
		}
		var paramsAny any
		_ = json.Unmarshal(in.Params, &paramsAny)
		if _, err := s.Chain.Append(ctx, tx, orgID, auditchain.Entry{
			Type:  "rule.create",
			Actor: &auditchain.Actor{APIKeyID: actor.APIKeyID, Label: actor.Label},
			Payload: map[string]any{
				"rule_version_id": row.ID,
				"rule_type":       row.RuleType,
				"version":         row.Version,
				"params":          paramsAny,
				"effective_at":    eff.Format(time.RFC3339Nano),
			},
		}); err != nil {
			return err
		}
		created = row
		return nil
	})
	return created, err
}

// List returns versions of a rule type (newest first).
func (s *Service) List(ctx context.Context, q *db.Queries, orgID int64, ruleType string) ([]db.RuleVersion, error) {
	return q.ListRuleVersions(ctx, db.ListRuleVersionsParams{
		OrgID: orgID, RuleType: ruleType,
	})
}
