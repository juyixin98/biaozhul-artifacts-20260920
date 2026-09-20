package api

import (
	"errors"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"signalboard/internal/db"
	"signalboard/internal/httpx"
)

type salesEventReq struct {
	EventID    string `json:"event_id"`
	Sku        string `json:"sku"`
	Qty        *int32 `json:"qty"`
	OccurredAt string `json:"occurred_at"`
}

// ingestSalesEvent records one sales event idempotently and updates the daily
// counter / sold-out state.
//
// Idempotency: (store_id, event_id) has a UNIQUE constraint and is inserted
// with ON CONFLICT DO NOTHING. That unique index — not application logic — is
// what makes a duplicate impossible to count twice under concurrency. A repeat
// of the identical payload is reported as a duplicate but counts once; the
// same ID reused with different sku/qty/occurred_at is a 409 conflict.
//
// Counting: the daily counter is upserted atomically (ON CONFLICT DO UPDATE
// qty = qty + EXCLUDED.qty). Concurrent increments serialize on the counter
// row lock, so none is lost.
//
// Late events: the day bucket is derived from occurred_at in the STORE's
// timezone, so an event that happened yesterday but arrives today is counted
// against yesterday and cannot disturb today's sold-out state.
func (s *Server) ingestSalesEvent(w http.ResponseWriter, r *http.Request) {
	storeID, ok := urlID(w, r, "storeID")
	if !ok {
		return
	}
	_, loc, ok := s.loadStore(w, r, storeID)
	if !ok {
		return
	}

	var req salesEventReq
	if !httpx.DecodeJSON(w, r, &req) {
		return
	}
	if req.EventID == "" {
		httpx.ErrorJSON(w, http.StatusBadRequest, "invalid_event", "event_id is required")
		return
	}
	if req.Sku == "" {
		httpx.ErrorJSON(w, http.StatusBadRequest, "invalid_event", "sku is required")
		return
	}
	if req.Qty == nil || *req.Qty <= 0 {
		httpx.ErrorJSON(w, http.StatusBadRequest, "invalid_event", "qty must be a positive integer")
		return
	}
	occurred, err := parseOccurredAt(req.OccurredAt)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusBadRequest, "invalid_event", err.Error())
		return
	}

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	defer func() { _ = tx.Rollback(r.Context()) }()
	qtx := s.q.WithTx(tx)

	_, insErr := qtx.InsertSalesEvent(r.Context(), db.InsertSalesEventParams{
		StoreID:    storeID,
		EventID:    req.EventID,
		Sku:        req.Sku,
		Qty:        *req.Qty,
		OccurredAt: ts(occurred),
	})

	duplicate := false
	if errors.Is(insErr, pgx.ErrNoRows) {
		// The event ID was already used. A replayed event must be re-read under
		// the same transaction to compare its full content.
		existing, gErr := qtx.GetSalesEvent(r.Context(), db.GetSalesEventParams{
			StoreID: storeID, EventID: req.EventID,
		})
		if gErr != nil {
			httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", gErr.Error())
			return
		}
		if !eventsEquivalent(existing, req, occurred) {
			httpx.ErrorJSON(w, http.StatusConflict, "event_id_conflict",
				"event_id "+req.EventID+" was already submitted with different content (sku/qty/occurred_at)")
			return
		}
		duplicate = true
	} else if insErr != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", insErr.Error())
		return
	}

	// Bucket by the actual local day of occurrence (handles late arrivals).
	y, m, dd := localDay(occurred, loc)
	day := date(y, m, dd)

	var counter db.DailySale
	var soldOut bool

	if !duplicate {
		counter, err = qtx.IncrementDailySales(r.Context(), db.IncrementDailySalesParams{
			StoreID:  storeID,
			SalesDay: day,
			Sku:      req.Sku,
			Qty:      int64(*req.Qty),
		})
		if err != nil {
			httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
			return
		}

		limit, lErr := qtx.GetDishLimit(r.Context(), db.GetDishLimitParams{
			StoreID: storeID, Sku: req.Sku,
		})
		switch {
		case lErr == nil:
			if limit.Valid && counter.Qty >= limit.Int64 {
				if mErr := qtx.MarkSoldOut(r.Context(), db.MarkSoldOutParams{
					StoreID: storeID, Sku: req.Sku, SalesDay: day,
				}); mErr != nil {
					httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", mErr.Error())
					return
				}
				soldOut = true
			}
		case errors.Is(lErr, pgx.ErrNoRows):
			// Unknown/inactive sku: counted, but there is no threshold to enforce.
		default:
			httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", lErr.Error())
			return
		}
	} else {
		// Echo the current counter for an idempotent replay.
		if rows, qErr := qtx.GetDailySales(r.Context(), db.GetDailySalesParams{
			StoreID: storeID, SalesDay: day,
		}); qErr == nil {
			for _, row := range rows {
				if row.Sku == req.Sku {
					counter = db.DailySale{StoreID: storeID, SalesDay: day, Sku: req.Sku, Qty: row.Qty}
					break
				}
			}
		}
	}

	if err := tx.Commit(r.Context()); err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}

	status := http.StatusCreated
	if duplicate {
		status = http.StatusOK
	}
	httpx.JSON(w, status, map[string]any{
		"event_id":    req.EventID,
		"sku":         req.Sku,
		"qty":         counter.Qty,
		"sales_day":   day.Time.Format("2006-01-02"),
		"sold_out":    soldOut,
		"duplicate":   duplicate,
		"occurred_at": occurred.Format(time.RFC3339Nano),
	})
}

// eventsEquivalent reports whether a replayed event matches the stored one.
// Only business fields are compared; received_at intentionally differs and is
// not part of identity.
func eventsEquivalent(stored db.SalesEvent, req salesEventReq, occurred time.Time) bool {
	return stored.Sku == req.Sku &&
		int32(stored.Qty) == *req.Qty &&
		tsTime(stored.OccurredAt).Equal(occurred)
}

// getDailySales returns counters and sold-out markers for a given local date
// (defaulting to today in the store's timezone).
func (s *Server) getDailySales(w http.ResponseWriter, r *http.Request) {
	storeID, ok := urlID(w, r, "storeID")
	if !ok {
		return
	}
	_, loc, ok := s.loadStore(w, r, storeID)
	if !ok {
		return
	}

	dayParam := r.URL.Query().Get("date")
	var day pgDate
	if dayParam != "" {
		t, err := time.Parse("2006-01-02", dayParam)
		if err != nil {
			httpx.ErrorJSON(w, http.StatusBadRequest, "invalid_date", "date must be YYYY-MM-DD")
			return
		}
		day = date(t.Year(), t.Month(), t.Day())
	} else {
		now := time.Now().In(loc)
		day = date(now.Date())
	}

	rows, err := s.q.GetDailySales(r.Context(), db.GetDailySalesParams{
		StoreID: storeID, SalesDay: day,
	})
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	soldOuts, err := s.q.ListSoldOutsForDay(r.Context(), db.ListSoldOutsForDayParams{
		StoreID: storeID, SalesDay: day,
	})
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{
		"date":      day.Time.Format("2006-01-02"),
		"sales":     rows,
		"sold_outs": soldOuts,
	})
}
