package server

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"time"

	"signalboard/internal/db"
)

type menuItem struct {
	ItemKey         string `json:"item_key"`
	Name            string `json:"name"`
	Position        int32  `json:"position"`
	BasePriceCents  int32  `json:"base_price_cents"`
	PriceCents      int32  `json:"price_cents"` // effective price (active temp band wins)
	TempPriceActive bool   `json:"temp_price_active"`
	SoldOut         bool   `json:"sold_out"`
}

type menuResponse struct {
	StoreID     string     `json:"store_id"`
	Version     int32      `json:"version"`
	GeneratedAt time.Time  `json:"generated_at"`
	Items       []menuItem `json:"items"`
}

// buildMenu converts one GetLiveMenu snapshot (a single query, so a single
// consistent database view) into the response body.
func buildMenu(storeID string, rows []db.GetLiveMenuRow, includeSoldToday bool) (menuResponse, []map[string]any) {
	resp := menuResponse{
		StoreID:     storeID,
		GeneratedAt: time.Now().UTC(),
		Items:       make([]menuItem, 0, len(rows)),
	}
	admin := make([]map[string]any, 0, len(rows))
	for _, r := range rows {
		resp.Version = r.Version
		resp.Items = append(resp.Items, menuItem{
			ItemKey:         r.ItemKey,
			Name:            r.Name,
			Position:        r.Position,
			BasePriceCents:  r.BasePriceCents,
			PriceCents:      r.EffectivePriceCents,
			TempPriceActive: r.TempPriceActive,
			SoldOut:         r.SoldOut,
		})
		if includeSoldToday {
			admin = append(admin, map[string]any{
				"item_key":           r.ItemKey,
				"name":               r.Name,
				"position":           r.Position,
				"base_price_cents":   r.BasePriceCents,
				"price_cents":        r.EffectivePriceCents,
				"temp_price_active":  r.TempPriceActive,
				"sold_out_threshold": r.SoldOutThreshold,
				"sold_today":         r.SoldToday,
				"sold_out":           r.SoldOut,
			})
		}
	}
	return resp, admin
}

// menuETag is computed over the version and the visible item state only
// (never over GeneratedAt), so it changes exactly when what a screen would
// render changes: publish, price/temp-price change, or sold-out flip.
func menuETag(resp menuResponse) string {
	payload, _ := json.Marshal(struct {
		Version int32      `json:"version"`
		Items   []menuItem `json:"items"`
	}{resp.Version, resp.Items})
	sum := sha256.Sum256(payload)
	return `"` + hex.EncodeToString(sum[:16]) + `"`
}

// serveMenu writes the menu with ETag / If-None-Match handling.
func serveMenu(w http.ResponseWriter, r *http.Request, resp menuResponse) {
	etag := menuETag(resp)
	w.Header().Set("ETag", etag)
	// no-cache: screens may store the body but must revalidate every time.
	w.Header().Set("Cache-Control", "no-cache")
	if inm := r.Header.Get("If-None-Match"); inm == etag || inm == "*" {
		w.WriteHeader(http.StatusNotModified)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// getLiveMenu is the admin live preview (includes sold_today counters).
func (s *Server) getLiveMenu(w http.ResponseWriter, r *http.Request) {
	storeID, ok := uuidParam(w, r, "storeID")
	if !ok {
		return
	}
	if _, err := s.q.GetStore(r.Context(), storeID); isNoRows(err) {
		writeError(w, http.StatusNotFound, "store not found")
		return
	}
	rows, err := s.q.GetLiveMenu(r.Context(), storeID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	resp, admin := buildMenu(uuidString(storeID), rows, true)
	writeJSON(w, http.StatusOK, map[string]any{
		"store_id": resp.StoreID,
		"version":  resp.Version,
		"items":    admin,
	})
}

// screenMenu serves the full current menu to an authenticated screen.
// Reconnecting screens always get the complete latest state here.
func (s *Server) screenMenu(w http.ResponseWriter, r *http.Request) {
	screen := screenFromCtx(r)
	rows, err := s.q.GetLiveMenu(r.Context(), screen.StoreID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	resp, _ := buildMenu(uuidString(screen.StoreID), rows, false)
	serveMenu(w, r, resp)
}
