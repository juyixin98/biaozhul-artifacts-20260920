// Package alerts implements alert triage: optimistic status transitions with
// an expected version, append-only status history, evidence-preserving
// recomputation, and read access always scoped to the caller's org.
package alerts

import (
	"context"
	"errors"
	"fmt"
	"time"

	"dams/internal/db"
	"dams/internal/platform/dbpool"
	"dams/internal/service/auditchain"
	"dams/internal/service/detection"
	svcrules "dams/internal/service/rules"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	ErrNotFound          = errors.New("alert not found")
	ErrVersionConflict   = errors.New("alert version conflict")
	ErrInvalidTransition = errors.New("invalid status transition")
)

type Actor struct {
	APIKeyID int64
	Label    string
}

type Service struct {
	Pool   *pgxpool.Pool
	Chain  *auditchain.Service
	Engine *detection.Engine
}

// allowed transitions from pending -> investigating -> resolved/false_positive;
// re-opening an investigated-but-not-resolved alert is permitted.
var allowed = map[string]map[string]bool{
	"pending":        {"investigating": true, "resolved": true, "false_positive": true},
	"investigating":  {"resolved": true, "false_positive": true, "pending": true},
	"resolved":       {"investigating": true}, // reopen
	"false_positive": {"investigating": true}, // reopen
}

type TransitionInput struct {
	AlertID         int64  `json:"alert_id"`
	ExpectedVersion int32  `json:"expected_version"`
	Status          string `json:"status"`
	Note            string `json:"note"`
}

func (s *Service) Transition(ctx context.Context, orgID int64, actor Actor, in TransitionInput) (db.Alert, error) {
	var out db.Alert
	err := dbpool.InTx(ctx, s.Pool, func(tx pgx.Tx) error {
		q := db.New(tx)
		cur, err := q.GetAlert(ctx, db.GetAlertParams{OrgID: orgID, ID: in.AlertID})
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return ErrNotFound
			}
			return err
		}
		if cur.Version != in.ExpectedVersion {
			return fmt.Errorf("%w: server version=%d", ErrVersionConflict, cur.Version)
		}
		if !allowed[cur.Status][in.Status] {
			return fmt.Errorf("%w: %s -> %s", ErrInvalidTransition, cur.Status, in.Status)
		}
		var resolvedBy pgtype.Int8
		if in.Status == "resolved" || in.Status == "false_positive" {
			resolvedBy = pgtype.Int8{Int64: actor.APIKeyID, Valid: true}
		}
		row, err := q.TransitionAlert(ctx, db.TransitionAlertParams{
			AlertID:         in.AlertID,
			OrgID:           orgID,
			ToStatus:        in.Status,
			ResolvedBy:      resolvedBy,
			DetailJson:      nil,
			ExpectedVersion: in.ExpectedVersion,
		})
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return ErrVersionConflict
			}
			return err
		}
		if err := q.InsertStatusHistory(ctx, db.InsertStatusHistoryParams{
			AlertID:    in.AlertID,
			FromStatus: pgtype.Text{String: cur.Status, Valid: true},
			ToStatus:   in.Status,
			Note:       in.Note,
			ActedBy:    pgtype.Int8{Int64: actor.APIKeyID, Valid: true},
		}); err != nil {
			return err
		}
		if _, err := s.Chain.Append(ctx, tx, orgID, auditchain.Entry{
			Type:  "alert.update",
			Actor: &auditchain.Actor{APIKeyID: actor.APIKeyID, Label: actor.Label},
			Payload: map[string]any{
				"alert_id": in.AlertID,
				"from":     cur.Status,
				"to":       in.Status,
				"version":  row.Version,
				"note":     in.Note,
			},
		}); err != nil {
			return err
		}
		out = row
		return nil
	})
	return out, err
}

// Get loads one alert scoped by org, plus history, evidence and contributing
// events. Contributing events are returned already masked by the export layer
// when the caller asks for masked output.
type Detail struct {
	Alert    db.Alert                   `json:"alert"`
	History  []db.AlertStatusHistory    `json:"history"`
	Evidence []db.AlertEvidenceRevision `json:"evidence"`
	Events   []map[string]any           `json:"events"`
}

func (s *Service) Detail(ctx context.Context, q *db.Queries, orgID, alertID int64, mask func(db.Event) map[string]any) (*Detail, error) {
	a, err := q.GetAlert(ctx, db.GetAlertParams{OrgID: orgID, ID: alertID})
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	history, err := q.ListStatusHistory(ctx, alertID)
	if err != nil {
		return nil, err
	}
	evidence, err := q.ListEvidenceRevisions(ctx, alertID)
	if err != nil {
		return nil, err
	}
	evs, err := q.ListAlertEvents(ctx, alertID)
	if err != nil {
		return nil, err
	}
	out := make([]map[string]any, 0, len(evs))
	for _, ev := range evs {
		out = append(out, mask(ev))
	}
	return &Detail{Alert: a, History: history, Evidence: evidence, Events: out}, nil
}

// RecomputeInput recounts one frequency window for one user. It is used after
// late/out-of-order backfill. The recomputation can only ADD evidence
// revisions to an existing alert; it never changes an alert's status.
type RecomputeInput struct {
	DbUser      string    `json:"db_user"`
	WindowStart time.Time `json:"window_start"`
}

// Recompute recounts one window after a late/out-of-order backfill. Only an
// evidence revision is appended to an existing alert; the status set by an
// investigator is never overwritten.
func (s *Service) Recompute(ctx context.Context, orgID int64, actor Actor, in RecomputeInput) (map[string]any, error) {
	out := map[string]any{}
	err := dbpool.InTx(ctx, s.Pool, func(tx pgx.Tx) error {
		q := db.New(tx)
		org, err := q.GetOrganization(ctx, orgID)
		if err != nil {
			return err
		}
		loc, err := time.LoadLocation(org.Timezone)
		if err != nil {
			return err
		}
		start := svcrules.WindowStart(in.WindowStart, loc).UTC()
		created, count, err := s.Engine.RecomputeWindow(ctx, tx, orgID, in.DbUser, start)
		if err != nil {
			return err
		}
		rv, err := db.New(tx).GetRuleVersionAt(ctx, db.GetRuleVersionAtParams{
			OrgID: orgID, RuleType: "frequency",
			EffectiveAt: pgtype.Timestamptz{Time: start, Valid: true},
		})
		if err != nil {
			return err
		}
		fp, err := svcrules.ParseFrequency(rv.Params)
		if err != nil {
			return err
		}
		end := start.Add(time.Duration(fp.WindowSeconds) * time.Second)
		out = map[string]any{
			"db_user":       in.DbUser,
			"window_start":  start.UTC().Format(time.RFC3339Nano),
			"window_end":    end.UTC().Format(time.RFC3339Nano),
			"event_count":   count,
			"threshold":     fp.Threshold,
			"violating":     count > fp.Threshold,
			"alert_created": created,
		}
		if _, err := s.Chain.Append(ctx, tx, orgID, auditchain.Entry{
			Type:  "alert.recompute",
			Actor: &auditchain.Actor{APIKeyID: actor.APIKeyID, Label: actor.Label},
			Payload: map[string]any{
				"db_user":       in.DbUser,
				"window_start":  start.UTC().Format(time.RFC3339Nano),
				"window_end":    end.UTC().Format(time.RFC3339Nano),
				"event_count":   count,
				"alert_created": created,
			},
		}); err != nil {
			return err
		}
		return nil
	})
	return out, err
}
