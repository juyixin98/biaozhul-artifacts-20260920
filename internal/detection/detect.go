// Package detection evaluates newly ingested events against the current set
// of enabled, versioned rules.
//
// Boundary semantics (explicit by design):
//
//   - Rate windows are FIXED, half-open intervals aligned to the Unix epoch:
//     windowStart = floor(occurred_at / window_seconds) * window_seconds,
//     and an event belongs to [windowStart, windowStart+window_seconds).
//     An event exactly on the closing boundary belongs to the NEXT window.
//     A window fires when count > max_events (strictly "more than").
//   - Sensitive-table rules use the organization's IANA time zone. The
//     allowed interval is [hour_start:00, hour_end:00) local time, i.e.
//     06:00:00 is allowed and 20:00:00 is not; 19:59:59 is allowed.
//
// Alerts carry the rule version in their fingerprint, so editing a rule never
// mutates existing alerts; late events recompute only alerts for the current
// version (see EvaluateBatch).
package detection

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"

	sqlcgen "dams.local/dams/internal/db/sqlc"
)

const (
	KindRate      = "rate"
	KindSensitive = "sensitive"
)

// Outcome describes what detection did with one logical rule/window target.
type Outcome struct {
	AlertID     int64  `json:"alert_id"`
	Fingerprint string `json:"fingerprint"`
	Kind        string `json:"kind"`
	Action      string `json:"action"` // created | updated | unchanged
	EventCount  int32  `json:"event_count"`
}

type windowKey struct {
	ruleID   int64
	version  int32
	user     string
	startUTC time.Time
}

// EvaluateBatch inspects events already inserted inside tx against the given
// current rules. It is safe to call concurrently from parallel ingests:
// alert fingerprints have a UNIQUE constraint and evidence links are
// INSERT ... ON CONFLICT DO NOTHING; callers should run the surrounding
// transaction at SERIALIZABLE and retry serialization failures.
func EvaluateBatch(
	ctx context.Context,
	tx pgx.Tx,
	q *sqlcgen.Queries,
	orgID int64,
	loc *time.Location,
	rules []sqlcgen.Rule,
	evs []sqlcgen.Event,
) ([]Outcome, error) {
	if loc == nil {
		loc = time.UTC
	}
	var out []Outcome

	rateRules := make(map[int64]sqlcgen.Rule)
	var sensRules []sqlcgen.Rule
	for _, r := range rules {
		switch r.Kind {
		case KindRate:
			rateRules[r.ID] = r
		case KindSensitive:
			sensRules = append(sensRules, r)
		}
	}

	// ---- Rate rules -----------------------------------------------------
	// Collect every (rule, user, fixed window) touched by this batch; each
	// one is fully recomputed from the events table, so late/out-of-order
	// events naturally revise already-open windows.
	touched := make(map[windowKey]struct{})
	for _, e := range evs {
		for _, r := range rateRules {
			start := windowStart(e.OccurredAt.Time, r.WindowSeconds)
			touched[windowKey{r.ID, r.Version, e.DbUser, start}] = struct{}{}
		}
	}
	for k := range touched {
		r := rateRules[k.ruleID]
		end := k.startUTC.Add(time.Duration(r.WindowSeconds) * time.Second)
		rows, err := q.EventsInWindow(ctx, sqlcgen.EventsInWindowParams{
			OrgID:        orgID,
			DbUser:       k.user,
			OccurredAt:   pgTimestamptz(k.startUTC),
			OccurredAt_2: pgTimestamptz(end),
		})
		if err != nil {
			return nil, fmt.Errorf("rate window query: %w", err)
		}
		o, err := reconcileRate(ctx, q, orgID, r, k, rows)
		if err != nil {
			return nil, err
		}
		if o.Action != "" {
			out = append(out, o)
		}
	}

	// ---- Sensitive-table rules -----------------------------------------
	for _, e := range evs {
		for _, r := range sensRules {
			if !tableMatches(r.Tables, e.SchemaName, e.TableName) {
				continue
			}
			local := e.OccurredAt.Time.In(loc)
			if withinAllowedHours(local.Hour(), r.HourStart, r.HourEnd) {
				continue
			}
			o, err := reconcileSensitive(ctx, q, orgID, r, e, local)
			if err != nil {
				return nil, err
			}
			if o.Action != "" {
				out = append(out, o)
			}
		}
	}

	return out, nil
}

// windowStart floors t to the most recent fixed boundary. windowSeconds for
// rate rules divides 3600 by configuration, so the alignment is identical in
// every time zone (verified on rule creation).
func windowStart(t time.Time, windowSeconds int32) time.Time {
	u := t.Unix()
	w := int64(windowSeconds)
	return time.Unix(u-u%w, 0).UTC()
}

func withinAllowedHours(hour int, start, end int32) bool {
	// Non-wrapping intervals only (start < end, validated by admin API):
	// [start, end), so hour==end is OUTSIDE.
	return int32(hour) >= start && int32(hour) < end
}

func tableMatches(patterns []string, schema, table string) bool {
	full := table
	if schema != "" {
		full = schema + "." + table
	}
	for _, p := range patterns {
		if p == full || (schema == "" && p == table) {
			return true
		}
		// Allow "schema.table" patterns to match events that only carry a
		// bare table name, and bare patterns to match the table part.
		if p == table {
			return true
		}
	}
	return false
}

func rateFingerprint(ruleID int64, version int32, user string, start time.Time) string {
	return fmt.Sprintf("rate:%d:v%d:%s:%d", ruleID, version, user, start.Unix())
}

func reconcileRate(
	ctx context.Context,
	q *sqlcgen.Queries,
	orgID int64,
	r sqlcgen.Rule,
	k windowKey,
	rows []sqlcgen.Event,
) (Outcome, error) {
	count := int32(len(rows))
	fp := rateFingerprint(r.ID, r.Version, k.user, k.startUTC)

	if count <= r.MaxEvents {
		// A previously firing window can dip below threshold only if events
		// were deleted (events are immutable), so nothing to close/revise.
		return Outcome{}, nil
	}

	end := k.startUTC.Add(time.Duration(r.WindowSeconds) * time.Second)
	user := k.user
	alert, err := q.GetAlertByFingerprint(ctx, sqlcgen.GetAlertByFingerprintParams{
		OrgID: orgID, Fingerprint: fp,
	})
	created := false
	if errors.Is(err, pgx.ErrNoRows) {
		startT := pgTimestamptz(k.startUTC)
		endT := pgTimestamptz(end)
		alert, err = q.InsertAlert(ctx, sqlcgen.InsertAlertParams{
			OrgID:       orgID,
			RuleID:      r.ID,
			RuleVersion: r.Version,
			Kind:        KindRate,
			Fingerprint: fp,
			WindowStart: startT,
			WindowEnd:   endT,
			DbUser:      &user,
			EventID:     nil,
			EventPk:     nil,
			EventCount:  count,
		})
		switch {
		case err == nil:
			created = true
		case errors.Is(err, pgx.ErrNoRows):
			// Lost the insert race to a concurrent batch; reconcile the
			// alert the winner created instead of failing.
			alert, err = q.GetAlertByFingerprint(ctx,
				sqlcgen.GetAlertByFingerprintParams{OrgID: orgID, Fingerprint: fp})
			if err != nil {
				return Outcome{}, err
			}
		default:
			return Outcome{}, fmt.Errorf("insert rate alert: %w", err)
		}
	} else if err != nil {
		return Outcome{}, err
	}

	// Evidence links are idempotent (ON CONFLICT DO NOTHING); the counter is
	// derived from the evidence table. Status, assignment and investigator
	// decisions are never touched on this path.
	for _, e := range rows {
		if err := q.LinkAlertEvent(ctx, sqlcgen.LinkAlertEventParams{
			AlertID: alert.ID, EventPk: e.ID,
		}); err != nil {
			return Outcome{}, err
		}
	}
	linked, err := q.CountAlertEvents(ctx, alert.ID)
	if err != nil {
		return Outcome{}, err
	}

	if created {
		if err := q.InsertAlertRevision(ctx, sqlcgen.InsertAlertRevisionParams{
			AlertID:    alert.ID,
			Revision:   "created",
			FromStatus: nil,
			ToStatus:   strPtr("open"),
			Note:       fmt.Sprintf("rate rule v%d: %d events in %ds window", r.Version, linked, r.WindowSeconds),
			EventCount: &linked,
			ActorID:    nil,
		}); err != nil {
			return Outcome{}, err
		}
		return Outcome{AlertID: alert.ID, Fingerprint: fp, Kind: KindRate,
			Action: "created", EventCount: linked}, nil
	}

	if linked == alert.EventCount {
		return Outcome{AlertID: alert.ID, Fingerprint: fp, Kind: KindRate,
			Action: "unchanged", EventCount: alert.EventCount}, nil
	}
	added := linked - alert.EventCount
	if err := q.BumpAlertCount(ctx, sqlcgen.BumpAlertCountParams{
		OrgID: orgID, ID: alert.ID, EventCount: linked,
	}); err != nil {
		return Outcome{}, err
	}
	note := fmt.Sprintf("recomputed after late/out-of-order events: %d new, %d total in window",
		added, linked)
	if err := q.InsertAlertRevision(ctx, sqlcgen.InsertAlertRevisionParams{
		AlertID:    alert.ID,
		Revision:   "recompute",
		FromStatus: nil,
		ToStatus:   nil,
		Note:       note,
		EventCount: &linked,
		ActorID:    nil,
	}); err != nil {
		return Outcome{}, err
	}
	return Outcome{AlertID: alert.ID, Fingerprint: fp, Kind: KindRate,
		Action: "updated", EventCount: linked}, nil
}

func reconcileSensitive(
	ctx context.Context,
	q *sqlcgen.Queries,
	orgID int64,
	r sqlcgen.Rule,
	e sqlcgen.Event,
	local time.Time,
) (Outcome, error) {
	// Fingerprint binds the event PK, so duplicate delivery of the same
	// event can never raise a second alert; binding the rule version keeps
	// later table-list edits from rewriting this alert.
	fp := fmt.Sprintf("sensitive:%d:v%d:event:%d", r.ID, r.Version, e.ID)
	existing, err := q.GetAlertByFingerprint(ctx, sqlcgen.GetAlertByFingerprintParams{
		OrgID: orgID, Fingerprint: fp,
	})
	if err == nil {
		return Outcome{AlertID: existing.ID, Fingerprint: fp, Kind: KindSensitive,
			Action: "unchanged", EventCount: 1}, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return Outcome{}, err
	}

	eventID := e.EventID
	eventPk := e.ID
	note := mustJSON(map[string]any{
		"local_time":   local.Format("2006-01-02T15:04:05-07:00"),
		"table":        e.TableName,
		"schema":       e.SchemaName,
		"action":       e.Action,
		"allowed_from": r.HourStart,
		"allowed_to":   r.HourEnd,
	})
	alert, err := q.InsertAlert(ctx, sqlcgen.InsertAlertParams{
		OrgID:       orgID,
		RuleID:      r.ID,
		RuleVersion: r.Version,
		Kind:        KindSensitive,
		Fingerprint: fp,
		WindowStart: pgTimestamptz(time.Time{}),
		WindowEnd:   pgTimestamptz(time.Time{}),
		DbUser:      nil,
		EventID:     &eventID,
		EventPk:     &eventPk,
		EventCount:  1,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		// Concurrent batch created it first.
		return Outcome{}, nil
	}
	if err != nil {
		return Outcome{}, fmt.Errorf("insert sensitive alert: %w", err)
	}
	if err := q.LinkAlertEvent(ctx, sqlcgen.LinkAlertEventParams{
		AlertID: alert.ID, EventPk: e.ID,
	}); err != nil {
		return Outcome{}, err
	}
	if err := q.InsertAlertRevision(ctx, sqlcgen.InsertAlertRevisionParams{
		AlertID:    alert.ID,
		Revision:   "created",
		ToStatus:   strPtr("open"),
		Note:       string(note),
		EventCount: int32Ptr(1),
	}); err != nil {
		return Outcome{}, err
	}
	return Outcome{AlertID: alert.ID, Fingerprint: fp, Kind: KindSensitive,
		Action: "created", EventCount: 1}, nil
}

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}

func strPtr(s string) *string { return &s }
func int32Ptr(i int32) *int32 { return &i }
