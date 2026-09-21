package server

import (
	"net/http"
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	"signalboard/internal/db"
)

type salesEventRequest struct {
	ID         string    `json:"id"`
	ItemKey    string    `json:"item_key"`
	Quantity   int32     `json:"quantity"`
	OccurredAt time.Time `json:"occurred_at"`
}

// recordSalesEvent ingests one sales event idempotently.
//
// The event id is the primary key of sales_events. Insert and aggregate run
// in one transaction, so a retried or concurrently duplicated event is
// counted exactly once; the same id with different payload is a conflict.
// The daily bucket is derived from occurred_at in the store's timezone, so
// late-arriving events are attributed to the day they actually happened.
func (s *Server) recordSalesEvent(w http.ResponseWriter, r *http.Request) {
	storeID, ok := uuidParam(w, r, "storeID")
	if !ok {
		return
	}
	var req salesEventRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	eventID, err := parseUUID(req.ID)
	if err != nil {
		writeError(w, http.StatusBadRequest, "id must be a UUID")
		return
	}
	if !itemKeyRE.MatchString(req.ItemKey) {
		writeError(w, http.StatusBadRequest, "invalid item_key")
		return
	}
	if req.Quantity <= 0 {
		writeError(w, http.StatusBadRequest, "quantity must be > 0")
		return
	}
	if req.OccurredAt.IsZero() {
		writeError(w, http.StatusBadRequest, "occurred_at is required (ISO-8601 with offset)")
		return
	}

	ctx := r.Context()
	store, err := s.q.GetStore(ctx, storeID)
	if isNoRows(err) {
		writeError(w, http.StatusNotFound, "store not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	loc, err := time.LoadLocation(store.Timezone)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "store has invalid timezone")
		return
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer func() { _ = tx.Rollback(ctx) }()
	q := s.q.WithTx(tx)

	if _, err := q.InsertSalesEvent(ctx, db.InsertSalesEventParams{
		ID:         eventID,
		StoreID:    storeID,
		ItemKey:    req.ItemKey,
		Quantity:   req.Quantity,
		OccurredAt: req.OccurredAt,
	}); isNoRows(err) {
		// Id already existed: identical payload => duplicate (200), else conflict.
		existing, gerr := q.GetSalesEvent(ctx, eventID)
		if gerr != nil {
			writeError(w, http.StatusInternalServerError, gerr.Error())
			return
		}
		same := existing.StoreID == storeID &&
			existing.ItemKey == req.ItemKey &&
			existing.Quantity == req.Quantity &&
			existing.OccurredAt.Equal(req.OccurredAt)
		if !same {
			writeError(w, http.StatusConflict, "event id already exists with different content")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"id": req.ID, "status": "duplicate"})
		return
	} else if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	// Bucket by the store-local calendar day of occurred_at.
	local := req.OccurredAt.In(loc)
	day := time.Date(local.Year(), local.Month(), local.Day(), 0, 0, 0, 0, time.UTC)
	if err := q.AddDailySales(ctx, db.AddDailySalesParams{
		StoreID:  storeID,
		ItemKey:  req.ItemKey,
		Day:      day,
		Quantity: req.Quantity,
	}); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if err := tx.Commit(ctx); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"id": req.ID, "status": "recorded"})
}

// listDailySales returns per-item totals for one store-local day (?day=YYYY-MM-DD).
func (s *Server) listDailySales(w http.ResponseWriter, r *http.Request) {
	storeID, ok := uuidParam(w, r, "storeID")
	if !ok {
		return
	}
	dayStr := r.URL.Query().Get("day")
	if dayStr == "" {
		writeError(w, http.StatusBadRequest, "day query parameter is required (YYYY-MM-DD)")
		return
	}
	day, err := time.ParseInLocation("2006-01-02", dayStr, time.UTC)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid day, expected YYYY-MM-DD")
		return
	}
	rows, err := s.q.ListDailySales(r.Context(), db.ListDailySalesParams{StoreID: storeID, Day: day})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"day": dayStr, "sales": rows})
}

func parseUUID(s string) (pgtype.UUID, error) {
	var id pgtype.UUID
	err := id.Scan(s)
	return id, err
}
