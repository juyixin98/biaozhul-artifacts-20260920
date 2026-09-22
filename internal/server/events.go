package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	sqlcgen "dams.local/dams/internal/db/sqlc"
	"dams.local/dams/internal/detection"
)

const (
	maxBatchSize = 2000
	maxBatchBody = 8 << 20 // 8 MiB
)

// allowedActionCategories is the closed set of operation classifications.
var allowedActionCategories = map[string]bool{
	"select": true, "insert": true, "update": true, "delete": true,
	"ddl": true, "grant": true, "other": true,
}

type eventIn struct {
	EventID    string    `json:"event_id"`
	Source     string    `json:"source"`
	DbUser     string    `json:"db_user"`
	OccurredAt time.Time `json:"occurred_at"`
	Action     string    `json:"action"`
	Schema     string    `json:"schema"`
	Table      string    `json:"table"`
	RowCount   int64     `json:"row_count"`
}

type batchRequest struct {
	// Source may be set once at batch level; per-event source overrides it.
	Source string    `json:"source"`
	Events []eventIn `json:"events"`
}

type batchResponse struct {
	Received   int                 `json:"received"`
	Duplicates int                 `json:"duplicates"`
	Detection  []detection.Outcome `json:"detection,omitempty"`
}

func (s *Server) handleBatchEvents(w http.ResponseWriter, r *http.Request) {
	org := currentOrg(r)
	var req batchRequest
	if err := decodeJSON(w, r, &req, maxBatchBody); err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", "invalid JSON body: "+err.Error(), nil)
		return
	}
	if len(req.Events) == 0 {
		writeError(w, http.StatusBadRequest, "empty_batch", "events must contain at least one item", nil)
		return
	}
	if len(req.Events) > maxBatchSize {
		writeError(w, http.StatusUnprocessableEntity, "batch_too_large",
			fmt.Sprintf("a batch accepts at most %d events, got %d", maxBatchSize, len(req.Events)), nil)
		return
	}

	// Normalize + validate the entire batch first. Any failure rejects the
	// whole request (nothing is inserted).
	type normEvent struct {
		e    eventIn
		hash string
	}
	events := make([]normEvent, 0, len(req.Events))
	byKey := make(map[string]int, len(req.Events)) // source\x00eventID -> index in events
	sources := make(map[string]struct{})

	for i, ev := range req.Events {
		if ev.Source == "" {
			ev.Source = req.Source
		}
		if problems := validateEvent(ev); len(problems) > 0 {
			writeError(w, http.StatusUnprocessableEntity, "invalid_event",
				fmt.Sprintf("event at index %d is invalid", i), problems)
			return
		}
		key := ev.Source + "\x00" + ev.EventID
		hash, err := contentHash(ev)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
			return
		}
		if prior, dup := byKey[key]; dup {
			if events[prior].hash != hash {
				writeError(w, http.StatusConflict, "id_conflict",
					fmt.Sprintf("event id %q from source %q appears twice in the batch with different content",
						ev.EventID, ev.Source), nil)
				return
			}
			// Exact duplicate inside the same batch: collapse, do not count twice.
			continue
		}
		byKey[key] = len(events)
		events = append(events, normEvent{e: ev, hash: hash})
		sources[ev.Source] = struct{}{}
	}

	loc, err := time.LoadLocation(org.Timezone)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "bad_timezone",
			"organization timezone invalid: "+err.Error(), nil)
		return
	}

	var inserted []sqlcgen.Event
	var dupCount int
	var outcomes []detection.Outcome

	err = runTx(r.Context(), s.pool, func(q *sqlcgen.Queries, tx pgx.Tx) error {
		inserted = inserted[:0]
		dupCount = 0
		sourceIDs := make(map[string]int64, len(sources))
		for sk := range sources {
			src, err := q.CreateSource(r.Context(), sqlcgen.CreateSourceParams{
				OrgID: org.ID, SourceKey: sk, Name: sk,
			})
			if err != nil {
				return fmt.Errorf("register source: %w", err)
			}
			sourceIDs[sk] = src.ID
		}

		for _, ne := range events {
			existing, gErr := q.GetEventBySourceEvent(r.Context(),
				sqlcgen.GetEventBySourceEventParams{
					SourceID: sourceIDs[ne.e.Source],
					EventID:  ne.e.EventID,
				})
			if gErr == nil {
				// Concurrent/retried delivery. Same id, different content is
				// a hard conflict that rolls back the entire batch; identical
				// content is a no-op duplicate and must never be counted again.
				if existing.ContentHash != ne.hash {
					return &conflictError{
						message: fmt.Sprintf("event id %q already exists for source %q with different content",
							ne.e.EventID, ne.e.Source),
						detail: map[string]any{
							"source": ne.e.Source, "event_id": ne.e.EventID,
						},
					}
				}
				dupCount++
				continue
			}
			if !errors.Is(gErr, pgx.ErrNoRows) {
				return gErr
			}

			ev, insErr := q.InsertEvent(r.Context(), sqlcgen.InsertEventParams{
				OrgID:       org.ID,
				SourceID:    sourceIDs[ne.e.Source],
				SourceKey:   ne.e.Source,
				EventID:     ne.e.EventID,
				DbUser:      ne.e.DbUser,
				OccurredAt:  pgTimestamptz(ne.e.OccurredAt),
				Action:      ne.e.Action,
				SchemaName:  ne.e.Schema,
				TableName:   ne.e.Table,
				RowCount:    ne.e.RowCount,
				ContentHash: ne.hash,
			})
			if insErr != nil {
				var pgErr *pgconn.PgError
				if errors.As(insErr, &pgErr) && pgErr.Code == "23505" {
					// Raced with a concurrent insert: re-read and compare.
					existing2, rErr := q.GetEventBySourceEvent(r.Context(),
						sqlcgen.GetEventBySourceEventParams{
							SourceID: sourceIDs[ne.e.Source],
							EventID:  ne.e.EventID,
						})
					if rErr != nil {
						return rErr
					}
					if existing2.ContentHash != ne.hash {
						return &conflictError{
							message: fmt.Sprintf("event id %q already exists for source %q with different content",
								ne.e.EventID, ne.e.Source),
							detail: map[string]any{"source": ne.e.Source, "event_id": ne.e.EventID},
						}
					}
					dupCount++
					continue
				}
				return insErr
			}
			inserted = append(inserted, ev)
		}

		rules, err := q.GetCurrentRules(r.Context(), org.ID)
		if err != nil {
			return err
		}
		if len(inserted) > 0 && len(rules) > 0 {
			outs, err := detection.EvaluateBatch(r.Context(), tx, q, org.ID, loc, rules, inserted)
			if err != nil {
				return fmt.Errorf("detection: %w", err)
			}
			outcomes = outs
		}
		return nil
	})

	if err != nil {
		var ce *conflictError
		if errors.As(err, &ce) {
			writeError(w, http.StatusConflict, "id_conflict", ce.message, ce.detail)
			return
		}
		writeError(w, http.StatusInternalServerError, "ingest_failed", err.Error(), nil)
		return
	}

	writeJSON(w, http.StatusOK, batchResponse{
		Received:   len(inserted),
		Duplicates: dupCount,
		Detection:  outcomes,
	})
}

func validateEvent(ev eventIn) []string {
	var problems []string
	if ev.EventID == "" {
		problems = append(problems, "event_id is required")
	}
	if ev.Source == "" {
		problems = append(problems, "source is required (per event or batch-level)")
	}
	if ev.DbUser == "" {
		problems = append(problems, "db_user is required")
	}
	if ev.OccurredAt.IsZero() {
		problems = append(problems, "occurred_at is required (RFC3339)")
	}
	if !allowedActionCategories[ev.Action] {
		problems = append(problems, "action must be one of: select, insert, update, delete, ddl, grant, other")
	}
	if ev.RowCount < 0 {
		problems = append(problems, "row_count must be >= 0")
	}
	return problems
}

// contentHash builds the canonical fingerprint of an event payload. The
// timestamp is reduced to UTC unix-nanos, so the same instant expressed in two
// UTC offsets hashes identically; key order is fixed explicitly.
func contentHash(ev eventIn) (string, error) {
	payload := struct {
		EventID    string `json:"event_id"`
		Source     string `json:"source"`
		DbUser     string `json:"db_user"`
		OccurredNS int64  `json:"occurred_unix_nano"`
		Action     string `json:"action"`
		Schema     string `json:"schema"`
		Table      string `json:"table"`
		RowCount   int64  `json:"row_count"`
	}{
		EventID:    ev.EventID,
		Source:     ev.Source,
		DbUser:     ev.DbUser,
		OccurredNS: ev.OccurredAt.UTC().UnixNano(),
		Action:     ev.Action,
		Schema:     ev.Schema,
		Table:      ev.Table,
		RowCount:   ev.RowCount,
	}
	b, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	return sha256Hex(b), nil
}

func pgTimestamptz(t time.Time) pgtypeTimestamptz {
	return pgtypeTimestamptz{Time: t.UTC(), Valid: true}
}
