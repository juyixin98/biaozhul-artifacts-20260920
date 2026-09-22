// Package detection evaluates rules against newly ingested events and
// recomputes windows on late arrival. All state transitions are idempotent:
// alerts are keyed by fingerprint and bound to the rule version effective at
// window start / event time.
package detection

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"time"

	"dams/internal/db"
	svcrules "dams/internal/service/rules"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

type Engine struct{}

func New() *Engine { return &Engine{} }

func ts(t time.Time) pgtype.Timestamptz {
	return pgtype.Timestamptz{Time: t.UTC(), Valid: true}
}

func i8(n int64) pgtype.Int8 { return pgtype.Int8{Int64: n, Valid: true} }

func actionsOrNil(a []string) []string {
	if len(a) == 0 {
		return nil
	}
	return a
}

// OnEvents runs both rule families for the freshly inserted events inside
// the caller's ingest transaction.
func (e *Engine) OnEvents(ctx context.Context, tx pgx.Tx, orgID int64, evs []db.Event, loc *time.Location) (int, error) {
	q := db.New(tx)
	created := 0

	active, err := q.GetActiveRuleVersions(ctx, orgID)
	if err != nil {
		return 0, err
	}
	var freqRules, sensRules []db.RuleVersion
	for _, rv := range active {
		switch rv.RuleType {
		case "frequency":
			freqRules = append(freqRules, rv)
		case "sensitive_hours":
			sensRules = append(sensRules, rv)
		}
	}

	// Collect the set of (user, windowStart) touched by new events, grouped by
	// the rule version effective at the window start.
	type windowKey struct {
		user  string
		start time.Time
	}
	touched := map[int64]map[windowKey]struct{}{} // ruleVersionID -> set
	for _, ev := range evs {
		for _, rv := range freqRules {
			p, err := svcrules.ParseFrequency(rv.Params)
			if err != nil {
				return 0, err
			}
			window := time.Duration(p.WindowSeconds) * time.Second
			for _, ws := range svcrules.ContainingWindows(ev.OccurredAt.Time, window, loc) {
				// Bind to the version effective at the window start — even if a
				// different active version exists now (late events).
				eff, err := q.GetRuleVersionAt(ctx, db.GetRuleVersionAtParams{
					OrgID: orgID, RuleType: "frequency",
					EffectiveAt: ts(ws),
				})
				if err == pgx.ErrNoRows {
					continue
				}
				if err != nil {
					return 0, err
				}
				set, ok := touched[eff.ID]
				if !ok {
					set = map[windowKey]struct{}{}
					touched[eff.ID] = set
				}
				set[windowKey{ev.DbUser, ws.UTC()}] = struct{}{}
			}
		}
	}
	for ruleVersionID, set := range touched {
		rv, err := q.GetRuleVersion(ctx, ruleVersionID)
		if err != nil {
			return 0, err
		}
		p, err := svcrules.ParseFrequency(rv.Params)
		if err != nil {
			return 0, err
		}
		window := time.Duration(p.WindowSeconds) * time.Second
		for k := range set {
			ok, _, err := e.recomputeWindow(ctx, q, orgID, rv, p, k.user, k.start, window)
			if err != nil {
				return 0, err
			}
			if ok {
				created++
			}
		}
	}

	// Sensitive-hours: evaluate each new access once, against the version
	// effective at the event time.
	for _, ev := range evs {
		for _, rv := range sensRules {
			p, err := svcrules.ParseSensitiveHours(rv.Params)
			if err != nil {
				return 0, err
			}
			if !p.MatchesAction(ev.ActionCategory) || !p.MatchesTable(ev.SchemaName, ev.TableName) {
				continue
			}
			eff, err := q.GetRuleVersionAt(ctx, db.GetRuleVersionAtParams{
				OrgID: orgID, RuleType: "sensitive_hours",
				EffectiveAt: ts(ev.OccurredAt.Time),
			})
			if err == pgx.ErrNoRows {
				continue
			}
			if err != nil {
				return 0, err
			}
			effP, err := svcrules.ParseSensitiveHours(eff.Params)
			if err != nil {
				return 0, err
			}
			local := ev.OccurredAt.Time.In(loc)
			if effP.Allowed(local) {
				continue
			}
			ok, err := e.createSensitiveAlert(ctx, q, orgID, eff, effP, ev, local)
			if err != nil {
				return 0, err
			}
			if ok {
				created++
			}
		}
	}
	return created, nil
}

// RecomputeWindow recounts one frequency window from the events table using
// the rule version effective at the window start, upserts detection state,
// and creates the alert if missing or appends an evidence revision when the
// count changed. Status is never touched. Returns (created bool, count).
func (e *Engine) RecomputeWindow(ctx context.Context, tx pgx.Tx, orgID int64, user string, start time.Time) (bool, int, error) {
	q := db.New(tx)
	rv, err := q.GetRuleVersionAt(ctx, db.GetRuleVersionAtParams{
		OrgID:       orgID,
		RuleType:    "frequency",
		EffectiveAt: ts(start),
	})
	if err != nil {
		return false, 0, err
	}
	p, err := svcrules.ParseFrequency(rv.Params)
	if err != nil {
		return false, 0, err
	}
	window := time.Duration(p.WindowSeconds) * time.Second
	created, count, err := e.recomputeWindow(ctx, q, orgID, rv, p, user, start.UTC(), window)
	return created, int(count), err
}

// CountWindow is a read-only window recount for previews/tests. It aligns
// the requested timestamp to the 1-minute step grid in the org timezone.
func (e *Engine) CountWindow(ctx context.Context, q *db.Queries, orgID int64, user string, start time.Time, loc *time.Location) (int, time.Time, time.Time, error) {
	aligned := svcrules.WindowStart(start, loc).UTC()
	rv, err := q.GetRuleVersionAt(ctx, db.GetRuleVersionAtParams{
		OrgID: orgID, RuleType: "frequency", EffectiveAt: ts(aligned),
	})
	if err != nil {
		return 0, aligned, aligned, err
	}
	p, err := svcrules.ParseFrequency(rv.Params)
	if err != nil {
		return 0, aligned, aligned, err
	}
	end := aligned.Add(time.Duration(p.WindowSeconds) * time.Second)
	cnt, err := q.CountEventsInWindow(ctx, db.CountEventsInWindowParams{
		OrgID:       orgID,
		DbUser:      user,
		WindowStart: ts(aligned),
		WindowEnd:   ts(end),
		Actions:     p.Actions,
	})
	if err != nil {
		return 0, aligned, end, err
	}
	return int(cnt.EventCount), aligned, end, nil
}

// recomputeWindow recounts one stepped window from the events table and
// upserts detection state. Returns (newAlertCreated, eventCount, error).
func (e *Engine) recomputeWindow(
	ctx context.Context, q *db.Queries, orgID int64, rv db.RuleVersion,
	p svcrules.FrequencyParams, user string, start time.Time, window time.Duration,
) (bool, int32, error) {
	end := start.Add(window)
	cnt, err := q.CountEventsInWindow(ctx, db.CountEventsInWindowParams{
		OrgID:       orgID,
		DbUser:      user,
		WindowStart: ts(start),
		WindowEnd:   ts(end),
		Actions:     actionsOrNil(p.Actions),
	})
	if err != nil {
		return false, 0, err
	}
	if _, err := q.UpsertDetectionWindow(ctx, db.UpsertDetectionWindowParams{
		OrgID:         orgID,
		RuleVersionID: rv.ID,
		DbUser:        user,
		WindowStart:   ts(start),
		WindowEnd:     ts(end),
		EventCount:    cnt.EventCount,
		FirstEventAt:  cnt.FirstAt,
		LastEventAt:   cnt.LastAt,
	}); err != nil {
		return false, 0, err
	}

	fp := frequencyFingerprint(rv.ID, user, start)
	if int(cnt.EventCount) <= p.Threshold {
		// Below threshold: nothing to raise. If an alert already exists (e.g.
		// threshold lowered), leave it — reopening/closing is an investigator
		// decision, never an automatic overwrite.
		return false, cnt.EventCount, nil
	}

	detail, _ := json.Marshal(map[string]any{
		"db_user":        user,
		"window_start":   start.UTC().Format(time.RFC3339Nano),
		"window_end":     end.UTC().Format(time.RFC3339Nano),
		"event_count":    cnt.EventCount,
		"threshold":      p.Threshold,
		"window_seconds": p.WindowSeconds,
	})
	title := fmt.Sprintf("frequency: %s ran %d actions in %s window", user, cnt.EventCount, window)
	alert, err := q.CreateAlert(ctx, db.CreateAlertParams{
		OrgID:         orgID,
		RuleVersionID: rv.ID,
		RuleType:      "frequency",
		Fingerprint:   fp,
		Status:        "pending",
		Title:         title,
		Detail:        detail,
		ResolvedBy:    pgtype.Int8{},
	})
	if err != nil {
		if !errors.Is(err, pgx.ErrNoRows) {
			return false, 0, err
		}
		// Fingerprint already exists — idempotent alert, but a late arrival may
		// have changed the authoritative count: append an evidence revision.
		existing, gErr := q.GetAlertByFingerprint(ctx, db.GetAlertByFingerprintParams{
			OrgID: orgID, Fingerprint: fp,
		})
		if gErr != nil {
			return false, 0, gErr
		}
		if aErr := appendEvidence(ctx, q, existing.ID, int(cnt.EventCount),
			orgID, user, start, end, p.Actions, detail); aErr != nil {
			return false, 0, aErr
		}
		return false, cnt.EventCount, nil
	}

	if err := linkWindowEvents(ctx, q, alert.ID, orgID, user, start, end, p.Actions, 1); err != nil {
		return false, 0, err
	}
	if err := q.InsertEvidenceRevision(ctx, db.InsertEvidenceRevisionParams{
		AlertID:    alert.ID,
		Seq:        1,
		EventCount: cnt.EventCount,
		Detail:     detail,
	}); err != nil {
		return false, 0, err
	}
	return true, cnt.EventCount, nil
}

func (e *Engine) createSensitiveAlert(
	ctx context.Context, q *db.Queries, orgID int64, rv db.RuleVersion,
	p svcrules.SensitiveHoursParams, ev db.Event, local time.Time,
) (bool, error) {
	fp := sensitiveFingerprint(rv.ID, ev.ID)
	detail, _ := json.Marshal(map[string]any{
		"event_id":      ev.ID,
		"db_user":       ev.DbUser,
		"schema":        ev.SchemaName,
		"table":         ev.TableName,
		"action":        ev.ActionCategory,
		"occurred_at":   ev.OccurredAt.Time.UTC().Format(time.RFC3339Nano),
		"local_time":    local.Format("15:04:05"),
		"allowed_hours": fmt.Sprintf("[%02d:00,%02d:00)", p.AllowedStartHour, p.AllowedEndHour),
	})
	title := fmt.Sprintf("sensitive_hours: %s accessed %s.%s at %s local",
		ev.DbUser, ev.SchemaName, ev.TableName, local.Format("15:04:05"))
	alert, err := q.CreateAlert(ctx, db.CreateAlertParams{
		OrgID:         orgID,
		RuleVersionID: rv.ID,
		RuleType:      "sensitive_hours",
		Fingerprint:   fp,
		Status:        "pending",
		Title:         title,
		Detail:        detail,
		ResolvedBy:    pgtype.Int8{},
	})
	if err != nil {
		// Per-event fingerprint already raised: idempotent no-op.
		if errors.Is(err, pgx.ErrNoRows) {
			return false, nil
		}
		return false, err
	}
	if err := q.InsertAlertEvent(ctx, db.InsertAlertEventParams{
		AlertID: alert.ID, EventID: ev.ID, AddedByRevision: 1,
	}); err != nil {
		return false, err
	}
	if err := q.InsertEvidenceRevision(ctx, db.InsertEvidenceRevisionParams{
		AlertID: alert.ID, Seq: 1, EventCount: 1, Detail: detail,
	}); err != nil {
		return false, err
	}
	return true, nil
}

// appendEvidence adds a revision when a recomputation changed the count. It
// never touches status, so investigator verdicts survive late arrivals.
func appendEvidence(ctx context.Context, q *db.Queries, alertID int64, count int,
	orgID int64, user string, start, end time.Time, actions []string, detail []byte,
) error {
	last, err := q.LatestEvidenceSeq(ctx, alertID)
	if err != nil {
		return err
	}
	if int(last.LatestCount) == count {
		return nil
	}
	newSeq := last.LatestSeq + 1
	if err := q.InsertEvidenceRevision(ctx, db.InsertEvidenceRevisionParams{
		AlertID: alertID, Seq: newSeq, EventCount: int32(count), Detail: detail,
	}); err != nil {
		return err
	}
	return linkWindowEvents(ctx, q, alertID, orgID, user, start, end, actions, newSeq)
}

func linkWindowEvents(ctx context.Context, q *db.Queries, alertID, orgID int64, user string, start, end time.Time, actions []string, revision int32) error {
	ids, err := q.EventIDsInWindow(ctx, db.EventIDsInWindowParams{
		OrgID:       orgID,
		DbUser:      user,
		WindowStart: ts(start),
		WindowEnd:   ts(end),
		Actions:     actionsOrNil(actions),
	})
	if err != nil {
		return err
	}
	for _, id := range ids {
		if err := q.InsertAlertEvent(ctx, db.InsertAlertEventParams{
			AlertID: alertID, EventID: id, AddedByRevision: revision,
		}); err != nil {
			return err
		}
	}
	return nil
}

func frequencyFingerprint(ruleVersionID int64, user string, start time.Time) string {
	return "freq:" + strconv.FormatInt(ruleVersionID, 10) + ":" + user + ":" +
		start.UTC().Format("20060102T150405Z")
}

func sensitiveFingerprint(ruleVersionID, eventID int64) string {
	return "sens:" + strconv.FormatInt(ruleVersionID, 10) + ":" + strconv.FormatInt(eventID, 10)
}
