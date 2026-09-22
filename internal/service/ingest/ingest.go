// Package ingest handles batch event ingestion and runs detection inside the
// same transaction, so detection can never observe a partially-rolled batch.
package ingest

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"dams/internal/db"
	"dams/internal/platform/canonical"
	"dams/internal/platform/hash"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

const (
	MaxBatchSize = 2000
)

// ErrConflict means an event with the same (source, source_event_id) exists
// but its content differs. The whole batch must be rejected.
var ErrConflict = errors.New("event content conflict")

// ErrBatchTooLarge etc. are returned as typed validation errors.
type ValidationError struct{ Msg string }

func (e *ValidationError) Error() string { return e.Msg }

type Event struct {
	SourceEventID  string    `json:"source_event_id"`
	DbUser         string    `json:"db_user"`
	OccurredAt     time.Time `json:"occurred_at"`
	ActionCategory string    `json:"action_category"`
	SchemaName     string    `json:"schema_name"`
	TableName      string    `json:"table_name"`
	RowCount       int64     `json:"row_count"`
	ClientIP       string    `json:"client_ip,omitempty"`
	SQLText        string    `json:"sql_text,omitempty"`
}

type BatchRequest struct {
	Source string  `json:"source"`
	Events []Event `json:"events"`
}

type BatchResult struct {
	BatchID       uuid.UUID `json:"batch_id"`
	Received      int       `json:"received"`
	Inserted      int       `json:"inserted"`
	Duplicates    int       `json:"duplicates"`
	AlertsCreated int       `json:"alerts_created"`
}

// detector is satisfied by the detection engine; indirection keeps ingest
// focused on persistence.
type Detector interface {
	OnEvents(ctx context.Context, tx pgx.Tx, orgID int64, evs []db.Event, loc *time.Location) (int, error)
}

type Service struct {
	pool     pgxQuerier
	detector Detector
}

// split into interface for testability
type pgxQuerier interface {
	Begin(context.Context) (pgx.Tx, error)
}

func New(pool interface {
	Begin(context.Context) (pgx.Tx, error)
}, d Detector) *Service {
	return &Service{pool: pool, detector: d}
}

// ContentHash hashes the canonical payload of the event. It covers every
// semantic field but not transport metadata; identical replays hash equal.
func ContentHash(e Event) string {
	payload := map[string]any{
		"source_event_id": e.SourceEventID,
		"db_user":         e.DbUser,
		"occurred_at":     e.OccurredAt.UTC().Format(time.RFC3339Nano),
		"action_category": e.ActionCategory,
		"schema_name":     e.SchemaName,
		"table_name":      e.TableName,
		"row_count":       e.RowCount,
		"client_ip":       e.ClientIP,
		"sql_text":        e.SQLText,
	}
	return hash.SHA256Hex(canonical.MustJSON(payload))
}

func Validate(req *BatchRequest) error {
	if req.Source == "" {
		return &ValidationError{Msg: "source is required"}
	}
	if len(req.Events) == 0 {
		return &ValidationError{Msg: "events must not be empty"}
	}
	if len(req.Events) > MaxBatchSize {
		return &ValidationError{Msg: fmt.Sprintf("batch exceeds maximum of %d events", MaxBatchSize)}
	}
	seen := make(map[string]struct{}, len(req.Events))
	validActions := map[string]bool{
		"select": true, "insert": true, "update": true, "delete": true,
		"ddl": true, "grant": true, "login": true, "other": true,
	}
	for i, e := range req.Events {
		if e.SourceEventID == "" {
			return &ValidationError{Msg: fmt.Sprintf("events[%d].source_event_id is required", i)}
		}
		if _, dup := seen[e.SourceEventID]; dup {
			return &ValidationError{Msg: fmt.Sprintf("duplicate source_event_id %q within batch", e.SourceEventID)}
		}
		seen[e.SourceEventID] = struct{}{}
		if e.DbUser == "" {
			return &ValidationError{Msg: fmt.Sprintf("events[%d].db_user is required", i)}
		}
		if e.OccurredAt.IsZero() {
			return &ValidationError{Msg: fmt.Sprintf("events[%d].occurred_at is required", i)}
		}
		if !validActions[e.ActionCategory] {
			return &ValidationError{Msg: fmt.Sprintf("events[%d].action_category invalid", i)}
		}
		if e.RowCount < 0 {
			return &ValidationError{Msg: fmt.Sprintf("events[%d].row_count must be >= 0", i)}
		}
	}
	return nil
}

// Ingest processes one batch in a single transaction. Pre-existing IDs are
// classified first: identical content = duplicate replay (skipped, not
// recounted), differing content = conflict (entire batch rolls back).
// Concurrent retries rely on the unique index: the second transaction waits
// on the row lock and then sees the row via the post-insert recheck, so no
// event is ever counted twice.
func (s *Service) Ingest(ctx context.Context, orgID int64, req *BatchRequest) (*BatchResult, error) {
	if err := Validate(req); err != nil {
		return nil, err
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	q := db.New(tx)

	source, err := q.GetOrCreateSource(ctx, db.GetOrCreateSourceParams{
		OrgID:      orgID,
		SourceName: req.Source,
	})
	if err != nil {
		return nil, err
	}
	org, err := q.GetOrganization(ctx, orgID)
	if err != nil {
		return nil, err
	}
	loc, err := time.LoadLocation(org.Timezone)
	if err != nil {
		return nil, fmt.Errorf("org timezone %q: %w", org.Timezone, err)
	}

	ids := make([]string, len(req.Events))
	hashes := make(map[string]string, len(req.Events))
	for i, e := range req.Events {
		ids[i] = e.SourceEventID
		hashes[e.SourceEventID] = ContentHash(e)
	}

	// Phase 1: classify existing rows.
	existing, err := q.GetExistingEvents(ctx, db.GetExistingEventsParams{
		OrgID: orgID, SourceID: source.ID, EventIds: ids,
	})
	if err != nil {
		return nil, err
	}
	var dupCount int
	for _, ex := range existing {
		if ex.ContentHash != hashes[ex.SourceEventID] {
			return nil, fmt.Errorf("%w: source_event_id=%q", ErrConflict, ex.SourceEventID)
		}
		dupCount++
	}

	// Phase 2: insert only unknown rows.
	inserted := make([]db.Event, 0, len(req.Events)-dupCount)
	batchUUID := uuid.New()
	batchUUIDVal := pgtype.UUID{Bytes: batchUUID, Valid: true}
	for _, e := range req.Events {
		if _, known := indexByID(existing, e.SourceEventID); known {
			continue
		}
		params := db.InsertEventParams{
			OrgID:          orgID,
			SourceID:       source.ID,
			SourceEventID:  e.SourceEventID,
			DbUser:         e.DbUser,
			OccurredAt:     pgtype.Timestamptz{Time: e.OccurredAt.UTC(), Valid: true},
			ActionCategory: e.ActionCategory,
			SchemaName:     e.SchemaName,
			TableName:      e.TableName,
			RowCount:       e.RowCount,
			ClientIp:       e.ClientIP,
			SqlText:        e.SQLText,
			ContentHash:    hashes[e.SourceEventID],
			BatchID:        batchUUIDVal,
		}
		row, err := q.InsertEvent(ctx, params)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				// Lost a race to a concurrent retry. Re-read to distinguish.
				got, gErr := q.GetExistingEvents(ctx, db.GetExistingEventsParams{
					OrgID: orgID, SourceID: source.ID,
					EventIds: []string{e.SourceEventID},
				})
				if gErr != nil {
					return nil, gErr
				}
				if len(got) == 1 && got[0].ContentHash == params.ContentHash {
					dupCount++
					continue
				}
				return nil, fmt.Errorf("%w: source_event_id=%q", ErrConflict, e.SourceEventID)
			}
			return nil, mapInsertErr(err, e.SourceEventID)
		}
		inserted = append(inserted, row)
	}

	// Phase 3: detection over genuinely new events only — replays never
	// recount and never generate duplicate alerts.
	var alertsCreated int
	if len(inserted) > 0 {
		alertsCreated, err = s.detector.OnEvents(ctx, tx, orgID, inserted, loc)
		if err != nil {
			return nil, err
		}
	}

	if _, err := q.CreateIngestBatch(ctx, db.CreateIngestBatchParams{
		ID:             batchUUID,
		OrgID:          orgID,
		SourceID:       source.ID,
		ReceivedCount:  int32(len(req.Events)),
		InsertedCount:  int32(len(inserted)),
		DuplicateCount: int32(dupCount),
		Status:         "accepted",
	}); err != nil {
		return nil, err
	}

	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return &BatchResult{
		BatchID:       batchUUID,
		Received:      len(req.Events),
		Inserted:      len(inserted),
		Duplicates:    dupCount,
		AlertsCreated: alertsCreated,
	}, nil
}

func mapInsertErr(err error, id string) error {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" {
		// Unique violation despite precheck: a concurrent transaction won and
		// committed between our SELECT and INSERT. Rollback is the safe answer;
		// the client retries and then sees duplicates.
		return fmt.Errorf("concurrent insert for source_event_id=%q, retry the batch: %w", id, ErrConflict)
	}
	return err
}

func indexByID(rows []db.GetExistingEventsRow, id string) (db.GetExistingEventsRow, bool) {
	for _, r := range rows {
		if r.SourceEventID == id {
			return r, true
		}
	}
	return db.GetExistingEventsRow{}, false
}

// MarshalCanonical is exposed for tooling/tests.
func MarshalCanonical(v any) ([]byte, error) { return canonical.JSON(v) }

var _ = json.Marshal
