// Package ingest handles batch event intake: validation, idempotent insert
// (duplicate reports are not booked twice), same-id/different-content conflict
// detection, and enforcement of the explicit backfill horizon.
package ingest

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"anomalywatch/internal/config"
	"anomalywatch/internal/models"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

// newToken returns a random 16-byte hex token identifying one batch.
func newToken() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

// EventIn is one event in a batch request.
type EventIn struct {
	EventID    string         `json:"event_id" binding:"required"`
	EmployeeID uint64         `json:"employee_id" binding:"required"`
	EventType  string         `json:"event_type" binding:"required"`
	OccurredAt time.Time      `json:"occurred_at" binding:"required"`
	Metadata   models.JSONMap `json:"metadata"`
}

// BatchRequest is a batch upload (at most cfg.MaxEventsPerBatch rows).
type BatchRequest struct {
	Source string    `json:"source"`
	Events []EventIn `json:"events" binding:"required"`
}

// ItemResult reports the outcome of one row.
type ItemResult struct {
	EventID string `json:"event_id"`
	Status  string `json:"status"` // inserted | duplicate | conflict | rejected
	Detail  string `json:"detail,omitempty"`
}

// BatchResult summarizes a batch.
type BatchResult struct {
	Received  int          `json:"received"`
	Inserted  int          `json:"inserted"`
	Duplicate int          `json:"duplicate"`
	Conflict  int          `json:"conflict"`
	Rejected  int          `json:"rejected"`
	Items     []ItemResult `json:"items"`
}

// ConflictError marks a batch that contained same-id/different-content rows
// (HTTP 409). Valid rows may still have been committed.
type ConflictError struct{ Result *BatchResult }

func (e *ConflictError) Error() string {
	return fmt.Sprintf("%d conflicting event(s)", e.Result.Conflict)
}

// RejectError marks a batch rejected wholesale (HTTP 400/422); nothing booked.
type RejectError struct {
	Reason string
}

func (e *RejectError) Error() string { return e.Reason }

// Service ingests events.
type Service struct {
	db  *gorm.DB
	cfg config.Config
}

func New(db *gorm.DB, cfg config.Config) *Service {
	return &Service{db: db, cfg: cfg}
}

// Ingest validates and books one batch. Semantics:
//   - Batch-level violations (empty, oversized, unknown enum, bad employee,
//     event outside the allowed backfill window, blank event_id) reject the
//     entire batch.
//   - Exact duplicates (same source+id and same content) are accepted but not
//     booked twice and reported as "duplicate".
//   - Same source+id with different content is a conflict: the incoming row is
//     not booked; valid other rows are still committed. Returns *ConflictError.
func (s *Service) Ingest(req *BatchRequest, now time.Time) (*BatchResult, error) {
	// DATETIME(3) only preserves milliseconds; truncate now so the value we
	// classify against after the insert round-trips exactly (otherwise an
	// inserted row would be misreported as a duplicate).
	now = now.UTC().Truncate(time.Millisecond)
	res := &BatchResult{Received: len(req.Events)}

	if len(req.Events) == 0 {
		return nil, &RejectError{Reason: "batch contains no events"}
	}
	if len(req.Events) > s.cfg.MaxEventsPerBatch {
		return nil, &RejectError{
			Reason: fmt.Sprintf("batch size %d exceeds maximum %d", len(req.Events), s.cfg.MaxEventsPerBatch),
		}
	}

	source := strings.TrimSpace(req.Source)
	if source == "" {
		source = "api"
	}

	// Pre-validate all rows; a single invalid row rejects the whole batch.
	empIDs := map[uint64]bool{}
	seenInBatch := map[string]bool{}
	oldest := now.Add(-s.cfg.MaxBackfillAge)
	newest := now.Add(s.cfg.MaxFutureDelay)
	for i, in := range req.Events {
		if strings.TrimSpace(in.EventID) == "" {
			return nil, &RejectError{Reason: fmt.Sprintf("events[%d].event_id is required", i)}
		}
		switch in.EventType {
		case models.EventTypeLogin, models.EventTypeFileDownload, models.EventTypeUSB:
		default:
			return nil, &RejectError{Reason: fmt.Sprintf("events[%d].event_type %q invalid", i, in.EventType)}
		}
		t := in.OccurredAt.UTC()
		if t.Before(oldest) {
			return nil, &RejectError{Reason: fmt.Sprintf(
				"events[%d].occurred_at %s is before the allowed backfill horizon (%s)",
				i, t.Format(time.RFC3339), oldest.Format(time.RFC3339))}
		}
		if t.After(newest) {
			return nil, &RejectError{Reason: fmt.Sprintf(
				"events[%d].occurred_at %s is more than %s in the future",
				i, t.Format(time.RFC3339), s.cfg.MaxFutureDelay)}
		}
		key := source + "|" + in.EventID
		if seenInBatch[key] {
			return nil, &RejectError{Reason: fmt.Sprintf(
				"events[%d].event_id %q repeated within the same batch", i, in.EventID)}
		}
		seenInBatch[key] = true
		empIDs[in.EmployeeID] = true
	}

	ids := make([]uint64, 0, len(empIDs))
	for id := range empIDs {
		ids = append(ids, id)
	}
	var known int64
	if err := s.db.Model(&models.Employee{}).Where("id IN ?", ids).Count(&known).Error; err != nil {
		return nil, err
	}
	if int(known) != len(ids) {
		return nil, &RejectError{Reason: "batch references unknown employee_id"}
	}

	// Compare against existing rows (single indexed query per source/id set).
	eventIDs := make([]string, 0, len(req.Events))
	res.Items = make([]ItemResult, len(req.Events))
	for i, in := range req.Events {
		eventIDs = append(eventIDs, in.EventID)
		res.Items[i] = ItemResult{EventID: in.EventID}
	}
	var existing []models.Event
	if err := s.db.Where("source = ? AND event_id IN ?", source, eventIDs).Find(&existing).Error; err != nil {
		return nil, err
	}
	existingByID := map[string]models.Event{}
	for _, ev := range existing {
		existingByID[ev.EventID] = ev
	}
	// Classify rows already stored.
	for i, in := range req.Events {
		prev, ok := existingByID[in.EventID]
		if !ok {
			continue
		}
		if prev.ContentHash == ContentHash(in) {
			res.Items[i].Status = "duplicate"
			res.Duplicate++
		} else {
			res.Items[i].Status = "conflict"
			res.Items[i].Detail = "event already reported with different content"
			res.Conflict++
		}
	}

	// Build in deterministic order; rows that lose a concurrent race on the
	// unique (source, event_id) key are detected by re-reading affected IDs.
	// Track which (event_id) were new in this batch.
	pendingIDs := map[string]bool{}
	var pending []EventIn
	for _, in := range req.Events {
		if _, exists := existingByID[in.EventID]; !exists {
			pendingIDs[in.EventID] = true
			pending = append(pending, in)
		}
	}
	sort.Slice(pending, func(i, j int) bool { return pending[i].EventID < pending[j].EventID })

	var toInsert []models.Event
	token := newToken()
	for _, in := range pending {
		toInsert = append(toInsert, models.Event{
			Source:      source,
			EventID:     in.EventID,
			EmployeeID:  in.EmployeeID,
			EventType:   in.EventType,
			OccurredAt:  in.OccurredAt.UTC(),
			Metadata:    in.Metadata,
			ContentHash: ContentHash(in),
			IngestToken: token,
			Processed:   false,
			ReceivedAt:  now,
		})
	}

	if len(toInsert) > 0 {
		if err := s.db.Transaction(func(tx *gorm.DB) error {
			// INSERT IGNORE: races on the unique (source, event_id) key are
			// resolved by re-reading affected IDs below instead of erroring.
			return tx.Clauses(clause.OnConflict{DoNothing: true}).Create(&toInsert).Error
		}); err != nil {
			return nil, err
		}

		// Re-read to resolve races with concurrent batches.
		var afterRows []models.Event
		if err := s.db.Where("source = ? AND event_id IN ?", source, eventIDs).Find(&afterRows).Error; err != nil {
			return nil, err
		}
		after := map[string]models.Event{}
		for _, ev := range afterRows {
			after[ev.EventID] = ev
		}
		incoming := map[string]EventIn{}
		for _, in := range req.Events {
			incoming[in.EventID] = in
		}
		for _, id := range sortedKeysOf(pendingIDs) {
			ev := after[id]
			it := itemByID(res, id)
			if it == nil {
				continue
			}
			// A row carrying this batch's token was inserted by this batch;
			// anything else lost the unique-key race to a concurrent batch.
			if ev.IngestToken == token {
				it.Status = "inserted"
				res.Inserted++
			} else if ev.ContentHash == ContentHash(incoming[id]) {
				// A concurrent identical batch won the unique-key race.
				it.Status = "duplicate"
				res.Duplicate++
			} else {
				it.Status = "conflict"
				it.Detail = "event already reported with different content"
				res.Conflict++
			}
		}
	}

	if res.Conflict > 0 {
		return res, &ConflictError{Result: res}
	}
	return res, nil
}

func itemByID(res *BatchResult, id string) *ItemResult {
	for i := range res.Items {
		if res.Items[i].EventID == id {
			return &res.Items[i]
		}
	}
	return nil
}

func sortedKeysOf(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// ContentHash computes a stable hash over the *content* of an event, excluding
// the reporting identity (event_id/source) and reception time. Metadata keys
// are sorted so JSON key ordering never produces a false conflict.
func ContentHash(in EventIn) string {
	metaCanonical := canonicalJSON(in.Metadata)
	raw := strings.Join([]string{
		fmt.Sprintf("employee=%d", in.EmployeeID),
		"type=" + in.EventType,
		"at=" + in.OccurredAt.UTC().Format(time.RFC3339Nano),
		"meta=" + metaCanonical,
	}, "|")
	sum := sha256.Sum256([]byte(raw))
	return hex.EncodeToString(sum[:])
}

func canonicalJSON(v any) string {
	b, err := json.Marshal(sortKeys(v))
	if err != nil {
		return ""
	}
	return string(b)
}

// sortKeys recursively normalizes a decoded JSON value so objects have stable
// key order (encoding/json marshals Go maps with sorted keys already; this
// handles nested []any/map[string]any uniformly).
func sortKeys(v any) any {
	switch t := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		out := make(map[string]any, len(t))
		for _, k := range keys {
			out[k] = sortKeys(t[k])
		}
		return out
	case []any:
		for i := range t {
			t[i] = sortKeys(t[i])
		}
		return t
	default:
		return v
	}
}
