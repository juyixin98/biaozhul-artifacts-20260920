package server

import (
	"net/http"
	"time"

	"signalboard/internal/db"
)

type draftItemBody struct {
	Name             string `json:"name"`
	PriceCents       int32  `json:"price_cents"`
	SoldOutThreshold int32  `json:"sold_out_threshold"`
	Position         int32  `json:"position"`
}

type tempPriceBody struct {
	ItemKey    string    `json:"item_key"`
	PriceCents int32     `json:"price_cents"`
	StartsAt   time.Time `json:"starts_at"`
	EndsAt     time.Time `json:"ends_at"`
}

func validItemBody(w http.ResponseWriter, key string, b draftItemBody) bool {
	switch {
	case !itemKeyRE.MatchString(key):
		writeError(w, http.StatusBadRequest, "item_key must match [a-z0-9][a-z0-9-]{0,63}")
	case b.Name == "":
		writeError(w, http.StatusBadRequest, "name is required")
	case b.PriceCents < 0:
		writeError(w, http.StatusBadRequest, "price_cents must be >= 0")
	case b.SoldOutThreshold < 0:
		writeError(w, http.StatusBadRequest, "sold_out_threshold must be >= 0")
	default:
		return true
	}
	return false
}

func (s *Server) getDraft(w http.ResponseWriter, r *http.Request) {
	storeID, ok := uuidParam(w, r, "storeID")
	if !ok {
		return
	}
	if _, err := s.q.GetStore(r.Context(), storeID); isNoRows(err) {
		writeError(w, http.StatusNotFound, "store not found")
		return
	}
	items, err := s.q.ListDraftItems(r.Context(), storeID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	tps, err := s.q.ListDraftTempPrices(r.Context(), storeID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"items":       items,
		"temp_prices": tps,
	})
}

func (s *Server) upsertDraftItem(w http.ResponseWriter, r *http.Request) {
	storeID, ok := uuidParam(w, r, "storeID")
	if !ok {
		return
	}
	itemKey := pathParam(r, "itemKey")
	var body draftItemBody
	if !decodeJSON(w, r, &body) {
		return
	}
	if !validItemBody(w, itemKey, body) {
		return
	}
	ctx := r.Context()
	if _, err := s.q.GetStore(ctx, storeID); isNoRows(err) {
		writeError(w, http.StatusNotFound, "store not found")
		return
	}
	if err := s.q.EnsureDraft(ctx, storeID); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	item, err := s.q.UpsertDraftItem(ctx, db.UpsertDraftItemParams{
		StoreID:          storeID,
		ItemKey:          itemKey,
		Name:             body.Name,
		PriceCents:       body.PriceCents,
		SoldOutThreshold: body.SoldOutThreshold,
		Position:         body.Position,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (s *Server) deleteDraftItem(w http.ResponseWriter, r *http.Request) {
	storeID, ok := uuidParam(w, r, "storeID")
	if !ok {
		return
	}
	n, err := s.q.DeleteDraftItem(r.Context(), db.DeleteDraftItemParams{
		StoreID: storeID,
		ItemKey: pathParam(r, "itemKey"),
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if n == 0 {
		writeError(w, http.StatusNotFound, "draft item not found")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) createDraftTempPrice(w http.ResponseWriter, r *http.Request) {
	storeID, ok := uuidParam(w, r, "storeID")
	if !ok {
		return
	}
	var body tempPriceBody
	if !decodeJSON(w, r, &body) {
		return
	}
	if !itemKeyRE.MatchString(body.ItemKey) {
		writeError(w, http.StatusBadRequest, "invalid item_key")
		return
	}
	if body.PriceCents < 0 {
		writeError(w, http.StatusBadRequest, "price_cents must be >= 0")
		return
	}
	if !body.StartsAt.Before(body.EndsAt) {
		writeError(w, http.StatusBadRequest, "starts_at must be before ends_at (half-open [starts_at, ends_at))")
		return
	}
	ctx := r.Context()
	if _, err := s.q.GetDraftItem(ctx, db.GetDraftItemParams{StoreID: storeID, ItemKey: body.ItemKey}); isNoRows(err) {
		writeError(w, http.StatusBadRequest, "unknown item_key: add the draft item first")
		return
	}
	tp, err := s.q.CreateDraftTempPrice(ctx, db.CreateDraftTempPriceParams{
		StoreID:    storeID,
		ItemKey:    body.ItemKey,
		PriceCents: body.PriceCents,
		StartsAt:   body.StartsAt,
		EndsAt:     body.EndsAt,
	})
	if err != nil {
		s.writeTempPriceError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, tp)
}

// writeTempPriceError maps constraint violations from temp-price inserts.
func (s *Server) writeTempPriceError(w http.ResponseWriter, err error) {
	switch pgErrorCode(err) {
	case "23P01": // exclusion_violation: overlapping band for the same item
		writeError(w, http.StatusConflict, "temp price overlaps an existing band for this item")
	case "23514": // check_violation
		writeError(w, http.StatusBadRequest, "invalid temp price: "+err.Error())
	case "23503": // foreign_key_violation
		writeError(w, http.StatusBadRequest, "unknown item_key or store")
	default:
		writeError(w, http.StatusInternalServerError, err.Error())
	}
}

func (s *Server) deleteDraftTempPrice(w http.ResponseWriter, r *http.Request) {
	storeID, ok := uuidParam(w, r, "storeID")
	if !ok {
		return
	}
	tpID, ok := uuidParam(w, r, "tempPriceID")
	if !ok {
		return
	}
	n, err := s.q.DeleteDraftTempPrice(r.Context(), db.DeleteDraftTempPriceParams{StoreID: storeID, ID: tpID})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if n == 0 {
		writeError(w, http.StatusNotFound, "temp price not found")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

type importRequest struct {
	Items []struct {
		ItemKey string `json:"item_key"`
		draftItemBody
	} `json:"items"`
	TempPrices []tempPriceBody `json:"temp_prices"`
}

// importDraft applies a batch of up to maxImportItems items (plus temp
// prices) atomically: any single error rolls the whole batch back.
func (s *Server) importDraft(w http.ResponseWriter, r *http.Request) {
	storeID, ok := uuidParam(w, r, "storeID")
	if !ok {
		return
	}
	var req importRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if len(req.Items) == 0 && len(req.TempPrices) == 0 {
		writeError(w, http.StatusBadRequest, "nothing to import")
		return
	}
	if len(req.Items) > maxImportItems {
		writeError(w, http.StatusBadRequest, "too many items: max 500 per import")
		return
	}
	seen := map[string]bool{}
	for _, it := range req.Items {
		if !validItemBody(w, it.ItemKey, it.draftItemBody) {
			return
		}
		if seen[it.ItemKey] {
			writeError(w, http.StatusBadRequest, "duplicate item_key in batch: "+it.ItemKey)
			return
		}
		seen[it.ItemKey] = true
	}
	for _, tp := range req.TempPrices {
		if !itemKeyRE.MatchString(tp.ItemKey) {
			writeError(w, http.StatusBadRequest, "invalid item_key in temp_prices")
			return
		}
		if tp.PriceCents < 0 || !tp.StartsAt.Before(tp.EndsAt) {
			writeError(w, http.StatusBadRequest, "invalid temp price in batch (price_cents/starts_at/ends_at)")
			return
		}
	}

	ctx := r.Context()
	if _, err := s.q.GetStore(ctx, storeID); isNoRows(err) {
		writeError(w, http.StatusNotFound, "store not found")
		return
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer func() { _ = tx.Rollback(ctx) }()
	q := s.q.WithTx(tx)

	if err := q.EnsureDraft(ctx, storeID); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	for _, it := range req.Items {
		if _, err := q.UpsertDraftItem(ctx, db.UpsertDraftItemParams{
			StoreID:          storeID,
			ItemKey:          it.ItemKey,
			Name:             it.Name,
			PriceCents:       it.PriceCents,
			SoldOutThreshold: it.SoldOutThreshold,
			Position:         it.Position,
		}); err != nil {
			writeError(w, http.StatusBadRequest, "item "+it.ItemKey+": "+err.Error())
			return
		}
	}
	for _, tp := range req.TempPrices {
		if _, err := q.CreateDraftTempPrice(ctx, db.CreateDraftTempPriceParams{
			StoreID:    storeID,
			ItemKey:    tp.ItemKey,
			PriceCents: tp.PriceCents,
			StartsAt:   tp.StartsAt,
			EndsAt:     tp.EndsAt,
		}); err != nil {
			s.writeTempPriceError(w, err)
			return
		}
	}
	if err := tx.Commit(ctx); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"imported_items":       len(req.Items),
		"imported_temp_prices": len(req.TempPrices),
	})
}
