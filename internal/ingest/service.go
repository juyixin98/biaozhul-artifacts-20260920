// Package ingest implements the per-minute activity snapshot intake.
//
// Guarantees:
//   - (workstation_id, minute) is idempotent. An identical re-send is reported
//     as a duplicate and processed once; a re-send whose payload differs is a
//     conflict and the WHOLE batch is rejected (no partial writes).
//   - any validation failure rolls the whole batch back.
//   - exempt departments, excluded apps and outside-window snapshots are
//     filtered before touching the raw table; they never reach storage.
//   - every stored row carries the policy_version and classification_version
//     that were actually applied.
package ingest

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jmoiron/sqlx"

	"desklens/internal/aggregate"
	"desklens/internal/model"
	"desklens/internal/pattern"
	"desklens/internal/store"
	"desklens/internal/timeutil"
)

// Principal is the workstation authenticated by its ingest API key.
type Principal struct {
	WorkstationID string
	EmployeeID    int64
	DepartmentID  int64
	Timezone      string
}

type Request struct {
	BatchID   *string          `json:"batch_id,omitempty"`
	Snapshots []model.Snapshot `json:"snapshots"`
}

// ItemOutcome reports what happened to one input snapshot, in input order.
type ItemOutcome struct {
	Index       int    `json:"index"`
	Workstation string `json:"workstation_id"`
	UTCTime     string `json:"utc_time"`
	AppName     string `json:"app_name"`
	Status      string `json:"status"` // accepted | duplicate | filtered
	Reason      string `json:"reason,omitempty"`
	Category    string `json:"category,omitempty"`
	RuleID      string `json:"rule_id,omitempty"`
}

type Outcome struct {
	BatchID       string        `json:"batch_id"`
	Accepted      int           `json:"accepted"`
	Duplicates    int           `json:"duplicates"`
	Filtered      int           `json:"filtered"`
	Items         []ItemOutcome `json:"items"`
	PolicyVersion int32         `json:"policy_version"`
	ClassVersion  int64         `json:"classification_version"`
}

// APIError carries an HTTP status plus stable machine-readable code.
type APIError struct {
	HTTPStatus int
	Code       string
	Message    string
}

func (e *APIError) Error() string { return fmt.Sprintf("%s: %s", e.Code, e.Message) }

const retentionLockKey int64 = 7701 // shared(ingest/rebuild) vs exclusive(purge)

type Service struct {
	DB    *sqlx.DB
	Store *store.Store
}

func New(db *sqlx.DB, st *store.Store) *Service { return &Service{DB: db, Store: st} }

// prepared is a normalized snapshot plus the decision reached for it.
type prepared struct {
	snap     model.Snapshot
	bucket   time.Time
	status   string // "" = candidate to insert, duplicate | filtered
	reason   string
	category string
	ruleID   string
}

// Ingest validates, filters, stores and aggregates one agent batch.
func (s *Service) Ingest(ctx context.Context, p Principal, req Request) (*Outcome, error) {
	if len(req.Snapshots) == 0 {
		return nil, &APIError{http.StatusBadRequest, "empty_batch", "snapshots must not be empty"}
	}

	batchID, err := resolveBatchID(req.BatchID)
	if err != nil {
		return nil, &APIError{http.StatusBadRequest, "invalid_batch_id", err.Error()}
	}

	// Idempotent replay: a previously completed batch returns its stored result
	// (rejected batches remain rejected).
	if prior, httpErr := s.lookupPriorBatch(ctx, batchID); httpErr != nil {
		return nil, httpErr
	} else if prior != nil {
		return prior, nil
	}

	loc, err := timeutil.LoadZone(p.Timezone)
	if err != nil {
		return nil, &APIError{http.StatusBadRequest, "invalid_timezone", err.Error()}
	}

	// --- Normalize + validate every item before any write -------------------
	items := make([]prepared, len(req.Snapshots))
	seen := make(map[string]int, len(req.Snapshots)) // workstation|minute -> first index

	now := time.Now().UTC().Add(2 * time.Minute)
	for i, snap := range req.Snapshots {
		bucket, vErr := validateSnapshot(snap, p, now)
		if vErr != nil {
			s.recordRejection(ctx, batchID, len(req.Snapshots), http.StatusBadRequest, "invalid_item", vErr.Error())
			return nil, vErr
		}
		key := snap.WorkstationID + "|" + bucket.Format(time.RFC3339)
		if first, ok := seen[key]; ok {
			if !samePayload(req.Snapshots[first], snap) {
				e := &APIError{http.StatusBadRequest, "duplicate_in_batch",
					fmt.Sprintf("conflicting snapshots for %s at %s", snap.WorkstationID, bucket.Format(time.RFC3339))}
				s.recordRejection(ctx, batchID, len(req.Snapshots), e.HTTPStatus, e.Code, e.Message)
				return nil, e
			}
			items[i] = prepared{snap: snap, bucket: bucket, status: "duplicate", reason: "duplicate_in_batch"}
			continue
		}
		seen[key] = i
		items[i] = prepared{snap: snap, bucket: bucket}
	}

	tx, err := s.DB.BeginTxx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback() //nolint:errcheck // no-op after commit

	// Serialize against raw-data purges for the whole write.
	if _, err := tx.ExecContext(ctx,
		`select pg_advisory_xact_lock_shared($1)`, retentionLockKey); err != nil {
		return nil, err
	}

	// Per-minute idempotency locks, acquired in a global order: two batches
	// racing the same workstation/minute serialize so the second sees the
	// first's committed row and reports a duplicate/conflict.
	minuteKeys := make([]aggregate.MinuteKey, 0, len(items))
	for _, it := range items {
		if it.status == "" {
			minuteKeys = append(minuteKeys, aggregate.MinuteKey{
				WorkstationID: it.snap.WorkstationID, Bucket: it.bucket})
		}
	}
	if err := aggregate.LockMinuteKeys(ctx, tx, minuteKeys); err != nil {
		return nil, err
	}

	policy, err := s.Store.GetCurrentPolicy(ctx, tx)
	if err != nil {
		if errors.Is(err, store.ErrNoPolicy) {
			return nil, &APIError{http.StatusServiceUnavailable, "no_policy", "no published policy"}
		}
		return nil, err
	}
	class, err := s.Store.GetClassification(ctx, tx)
	if err != nil {
		if errors.Is(err, store.ErrNoClassification) {
			return nil, &APIError{http.StatusServiceUnavailable, "no_classification", "no published classification"}
		}
		return nil, err
	}

	// --- DB-side idempotency / conflict check --------------------------------
	// This runs BEFORE policy evaluation: a minute that was already accepted is
	// a duplicate regardless of later policy changes, and a re-send with
	// different content is always a conflict. Policy publications must not turn
	// previously-accepted minutes into filtered re-sends.
	pending := make([]prepared, 0, len(items))
	for _, it := range items {
		if it.status == "" {
			pending = append(pending, it)
		}
	}
	fresh := make([]prepared, 0, len(pending))
	if len(pending) > 0 {
		existing, err := loadExisting(ctx, tx, pending)
		if err != nil {
			return nil, err
		}
		for _, c := range pending {
			if ex, ok := existing[rowKey(c.snap.WorkstationID, c.bucket)]; ok {
				if ex.appName == c.snap.AppName && ex.count == c.snap.ActivityCount {
					idx := indexOf(items, c)
					items[idx].status, items[idx].reason = "duplicate", "already_ingested"
				} else {
					e := &APIError{http.StatusConflict, "snapshot_conflict",
						fmt.Sprintf("existing snapshot for %s at %s has different content",
							c.snap.WorkstationID, c.bucket.Format(time.RFC3339))}
					s.recordRejection(ctx, batchID, len(req.Snapshots), e.HTTPStatus, e.Code, e.Message)
					return nil, e
				}
				continue
			}
			fresh = append(fresh, c)
		}
	}

	// Retention gate: genuinely new snapshots older than the oldest surviving
	// raw data would land inside a purged range; refuse rather than create
	// unrebuildable rows. Duplicates of stored rows are exempt (already stored).
	var earliest sql.NullTime
	if err := tx.GetContext(ctx, &earliest,
		`select earliest_snapshot from retention_state where id = 1`); err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	if earliest.Valid {
		for _, it := range fresh {
			if it.bucket.Before(earliest.Time) {
				e := &APIError{http.StatusUnprocessableEntity, "beyond_retention_boundary",
					fmt.Sprintf("snapshot %s at %s precedes retained raw-data boundary %s",
						it.snap.WorkstationID, it.bucket.Format(time.RFC3339), earliest.Time.Format(time.RFC3339))}
				s.recordRejection(ctx, batchID, len(req.Snapshots), e.HTTPStatus, e.Code, e.Message)
				return nil, e
			}
		}
	}

	// --- Privacy filtering + classification (new minutes only) --------------
	candidates := make([]prepared, 0, len(fresh))
	for _, c := range fresh {
		switch {
		case policy.ExemptDepartmentID[p.DepartmentID]:
			setItemStatus(items, c, "filtered", model.ReasonExemptDepartment)
		case pattern.Any(policy.ExcludedPatterns, c.snap.AppName):
			setItemStatus(items, c, "filtered", model.ReasonExcludedApp)
		case !timeutil.WithinWindow(timeutil.MinuteOfDay(c.bucket, loc), policy.StartMinute, policy.EndMinute):
			setItemStatus(items, c, "filtered", model.ReasonOutsideWindow)
		default:
			c.category, c.ruleID = classify(class.Rules, c.snap.AppName)
			idx := indexOf(items, c)
			items[idx].category, items[idx].ruleID = c.category, c.ruleID
			candidates = append(candidates, c)
		}
	}

	// --- Insert accepted raw rows --------------------------------------------
	buckets := make([]aggregate.Bucket, 0, len(candidates))
	seenBucket := map[aggregate.Bucket]bool{}
	for _, c := range candidates {
		var ruleID interface{}
		if c.ruleID != "" {
			ruleID = c.ruleID
		}
		if _, err := tx.ExecContext(ctx, `
			insert into activity_snapshots (
				workstation_id, employee_id, bucket_time, app_name, activity_count,
				policy_version, classification_version, category, matched_rule_id, batch_id)
			values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
			c.snap.WorkstationID, p.EmployeeID, c.bucket, c.snap.AppName, c.snap.ActivityCount,
			policy.Version, class.Version, c.category, ruleID, batchID); err != nil {
			var pgErr *pgconn.PgError
			if errors.As(err, &pgErr) && pgErr.Code == "23505" {
				// Lost a race with a concurrent insert for the same minute.
				e := &APIError{http.StatusConflict, "snapshot_conflict",
					"concurrent insert for " + c.snap.WorkstationID + " at " + c.bucket.Format(time.RFC3339)}
				s.recordRejection(ctx, batchID, len(req.Snapshots), e.HTTPStatus, e.Code, e.Message)
				return nil, e
			}
			return nil, err
		}
		b := aggregate.Bucket{
			EmployeeID:   p.EmployeeID,
			DepartmentID: p.DepartmentID,
			LocalDate:    timeutil.LocalDate(c.bucket, loc),
			WeekStart:    timeutil.WeekStart(c.bucket, loc),
		}
		if !seenBucket[b] {
			seenBucket[b] = true
			buckets = append(buckets, b)
		}
	}

	if err := aggregate.RecomputeAffected(ctx, tx, buckets); err != nil {
		return nil, err
	}

	out := buildOutcome(batchID, items, policy.Version, class.Version)
	if _, err := tx.ExecContext(ctx, `
		insert into ingest_batches
			(batch_id, status, item_count, accepted_count, duplicate_count, filtered_count)
		values ($1,'committed',$2,$3,$4,$5)`,
		batchID, len(items), out.Accepted, out.Duplicates, out.Filtered); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return out, nil
}

type existingRow struct {
	appName string
	count   int
}

func loadExisting(ctx context.Context, tx *sqlx.Tx, candidates []prepared) (map[string]existingRow, error) {
	// Batches are small; a VALUES list with pair-wise parameters is clear and
	// uses the primary key.
	q := `select workstation_id, bucket_time, app_name, activity_count
	      from activity_snapshots where (workstation_id, bucket_time) in (`
	args := make([]interface{}, 0, len(candidates)*2)
	for i, c := range candidates {
		if i > 0 {
			q += ","
		}
		q += fmt.Sprintf("($%d,$%d)", i*2+1, i*2+2)
		args = append(args, c.snap.WorkstationID, c.bucket)
	}
	q += ")"
	rows, err := tx.QueryxContext(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]existingRow{}
	for rows.Next() {
		var ws, app string
		var bt time.Time
		var cnt int
		if err := rows.Scan(&ws, &bt, &app, &cnt); err != nil {
			return nil, err
		}
		// timestamptz arrives in the session zone; keys are always built from
		// the UTC instant, so normalize before the lookup.
		out[rowKey(ws, bt.UTC())] = existingRow{app, cnt}
	}
	return out, rows.Err()
}

func rowKey(ws string, t time.Time) string { return ws + "|" + t.Format(time.RFC3339) }

func indexOf(items []prepared, target prepared) int {
	for i := range items {
		if items[i].snap.WorkstationID == target.snap.WorkstationID &&
			items[i].bucket.Equal(target.bucket) {
			return i
		}
	}
	return -1
}

func setItemStatus(items []prepared, target prepared, status, reason string) {
	idx := indexOf(items, target)
	if idx >= 0 {
		items[idx].status, items[idx].reason = status, reason
	}
}

func buildOutcome(batchID string, items []prepared, policyVer int32, classVer int64) *Outcome {
	o := &Outcome{BatchID: batchID, PolicyVersion: policyVer, ClassVersion: classVer,
		Items: make([]ItemOutcome, 0, len(items))}
	for i, it := range items {
		status := it.status
		if status == "" {
			status = "accepted"
		}
		switch status {
		case "accepted":
			o.Accepted++
		case "duplicate":
			o.Duplicates++
		case "filtered":
			o.Filtered++
		}
		o.Items = append(o.Items, ItemOutcome{
			Index:       i,
			Workstation: it.snap.WorkstationID,
			UTCTime:     it.bucket.Format(time.RFC3339),
			AppName:     it.snap.AppName,
			Status:      status,
			Reason:      it.reason,
			Category:    it.category,
			RuleID:      it.ruleID,
		})
	}
	return o
}

// lookupPriorBatch returns a replay outcome for a batch already processed.
func (s *Service) lookupPriorBatch(ctx context.Context, batchID string) (*Outcome, *APIError) {
	var (
		status, code, detail       string
		itemCount                  int
		accepted, duplicates, filt int
		httpStatus                 sql.NullInt64
	)
	err := s.DB.QueryRowxContext(ctx, `
		select status, item_count, accepted_count, duplicate_count, filtered_count,
		       http_status, coalesce(error_code,''), detail
		from ingest_batches where batch_id = $1`, batchID).Scan(
		&status, &itemCount, &accepted, &duplicates, &filt,
		&httpStatus, &code, &detail)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, &APIError{http.StatusInternalServerError, "batch_lookup_failed", err.Error()}
	}
	if status == "rejected" {
		hs := http.StatusConflict
		if httpStatus.Valid && httpStatus.Int64 > 0 {
			hs = int(httpStatus.Int64)
		}
		if code == "" {
			code = "previously_rejected"
		}
		return nil, &APIError{hs, code, "batch " + batchID + " was already rejected: " + detail}
	}
	return &Outcome{
		BatchID:    batchID,
		Accepted:   accepted,
		Duplicates: duplicates,
		Filtered:   filt,
		Items:      []ItemOutcome{},
	}, nil
}

func (s *Service) recordRejection(ctx context.Context, batchID string, itemCount, httpStatus int, code, detail string) {
	_, _ = s.DB.ExecContext(ctx, `
		insert into ingest_batches
			(batch_id, status, item_count, http_status, error_code, detail)
		values ($1,'rejected',$2,$3,$4,$5)
		on conflict (batch_id) do nothing`, batchID, itemCount, httpStatus, code, detail)
}

func resolveBatchID(id *string) (string, error) {
	if id == nil || *id == "" {
		return uuid.NewString(), nil
	}
	if _, err := uuid.Parse(*id); err != nil {
		return "", fmt.Errorf("batch_id must be a UUID: %w", err)
	}
	return *id, nil
}

func validateSnapshot(s model.Snapshot, p Principal, now time.Time) (time.Time, *APIError) {
	if s.WorkstationID == "" {
		return time.Time{}, &APIError{http.StatusBadRequest, "invalid_item", "workstation_id required"}
	}
	if s.WorkstationID != p.WorkstationID {
		return time.Time{}, &APIError{http.StatusForbidden, "workstation_mismatch",
			"API key is bound to workstation " + p.WorkstationID}
	}
	if s.EmployeeID != p.EmployeeID {
		return time.Time{}, &APIError{http.StatusForbidden, "employee_mismatch",
			"workstation is bound to a different employee"}
	}
	if s.UTCTime.IsZero() {
		return time.Time{}, &APIError{http.StatusBadRequest, "invalid_item", "utc_time required"}
	}
	bucket := s.UTCTime.UTC().Truncate(time.Minute)
	if bucket.After(now) {
		return time.Time{}, &APIError{http.StatusBadRequest, "future_snapshot",
			"snapshot time is more than 2 minutes in the future"}
	}
	if s.AppName == "" {
		return time.Time{}, &APIError{http.StatusBadRequest, "invalid_item", "app_name required"}
	}
	if len(s.AppName) > 256 {
		return time.Time{}, &APIError{http.StatusBadRequest, "invalid_item", "app_name too long"}
	}
	if s.ActivityCount < 0 {
		return time.Time{}, &APIError{http.StatusBadRequest, "invalid_item", "activity_count must be >= 0"}
	}
	return bucket, nil
}

func samePayload(a, b model.Snapshot) bool {
	return a.AppName == b.AppName && a.ActivityCount == b.ActivityCount &&
		a.EmployeeID == b.EmployeeID
}

// classify evaluates rules in stored order (priority ASC, rule_id ASC) and
// returns the first match. Unmatched apps are neutral with no rule id.
func classify(rules []model.Rule, app string) (string, string) {
	for _, r := range rules {
		if pattern.Match(r.Pattern, app) {
			return r.Category, r.RuleID
		}
	}
	return model.CategoryNeutral, ""
}
