package server

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"signalboard/internal/db"
)

func timestamptz(t time.Time) pgtype.Timestamptz {
	return pgtype.Timestamptz{Time: t.UTC(), Valid: true}
}

func pgdate(t time.Time) pgtype.Date {
	return pgtype.Date{Time: t, Valid: true}
}

// salesContentHash identifies the payload carried by an event id, so a
// replay with different content can be rejected as a conflict.
func salesContentHash(req salesEventReq) string {
	sum := sha256.Sum256([]byte(fmt.Sprintf("%s|%d|%s",
		req.ItemID, req.Quantity, req.OccurredAt.UTC().Format(time.RFC3339Nano))))
	return hex.EncodeToString(sum[:])
}

// --- screen menu ------------------------------------------------------------------

type tempPriceInfo struct {
	PriceCents int32     `json:"price_cents"`
	StartsAt   time.Time `json:"starts_at"`
	EndsAt     time.Time `json:"ends_at"`
}

type menuItem struct {
	ItemID         uuid.UUID      `json:"item_id"`
	Name           string         `json:"name"`
	Description    string         `json:"description"`
	Category       string         `json:"category"`
	Position       int32          `json:"position"`
	BasePriceCents int32          `json:"base_price_cents"`
	PriceCents     int32          `json:"price_cents"` // effective price right now
	TempPrice      *tempPriceInfo `json:"temp_price"`
	SoldOut        bool           `json:"sold_out"`
}

type screenMenu struct {
	StoreID   uuid.UUID  `json:"store_id"`
	Version   int32      `json:"version"`
	Items     []menuItem `json:"items"`
}

// menuETag hashes everything a screen renders, so any change — publish,
// temp-price activation/expiry, sold-out flip — yields a different ETag.
func menuETag(m screenMenu) string {
	payload, _ := json.Marshal(m)
	sum := sha256.Sum256(payload)
	return `"` + hex.EncodeToString(sum[:16]) + `"`
}

func (s *Server) getScreenMenu(w http.ResponseWriter, r *http.Request) {
	screen := r.Context().Value(ctxScreen).(db.Screen)
	now := s.now()

	var menu screenMenu
	err := s.withTx(r.Context(), pgx.RepeatableRead, func(q *db.Queries) error {
		// One REPEATABLE READ snapshot for version items, temp prices and
		// sales totals: a response can never mix pre- and post-change state.
		st, err := q.GetStore(r.Context(), screen.StoreID)
		if err != nil {
			return err
		}
		loc, err := time.LoadLocation(st.Timezone)
		if err != nil {
			return fmt.Errorf("store timezone: %w", err)
		}
		today := now.In(loc)

		menu = screenMenu{StoreID: screen.StoreID, Version: st.CurrentVersion, Items: []menuItem{}}
		if st.CurrentVersion == 0 {
			return nil
		}
		rows, err := q.GetCurrentVersionItems(r.Context(), db.GetCurrentVersionItemsParams{
			StoreID: screen.StoreID, Version: st.CurrentVersion,
		})
		if err != nil {
			return err
		}
		activeTPs, err := q.ListActiveTempPrices(r.Context(), db.ListActiveTempPricesParams{
			StoreID: screen.StoreID, Now: timestamptz(now),
		})
		if err != nil {
			return err
		}
		tpByItem := make(map[uuid.UUID]db.ListActiveTempPricesRow, len(activeTPs))
		for _, tp := range activeTPs {
			tpByItem[tp.ItemID] = tp
		}
		sales, err := q.GetDailySales(r.Context(), db.GetDailySalesParams{
			StoreID: screen.StoreID, SaleDate: pgdate(today),
		})
		if err != nil {
			return err
		}
		qtyByItem := make(map[uuid.UUID]int64, len(sales))
		for _, row := range sales {
			qtyByItem[row.ItemID] = row.Quantity
		}

		for _, it := range rows {
			mi := menuItem{
				ItemID: it.ItemID, Name: it.Name, Description: it.Description,
				Category: it.Category, Position: it.Position,
				BasePriceCents: it.PriceCents, PriceCents: it.PriceCents,
			}
			if tp, ok := tpByItem[it.ItemID]; ok {
				mi.PriceCents = tp.PriceCents
				mi.TempPrice = &tempPriceInfo{
					PriceCents: tp.PriceCents,
					StartsAt:   tp.StartsAt.Time,
					EndsAt:     tp.EndsAt.Time,
				}
			}
			if it.SoldOutThreshold > 0 && qtyByItem[it.ItemID] >= int64(it.SoldOutThreshold) {
				mi.SoldOut = true
			}
			menu.Items = append(menu.Items, mi)
		}
		return nil
	})
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}

	etag := menuETag(menu)
	w.Header().Set("ETag", etag)
	w.Header().Set("Cache-Control", "private, must-revalidate")
	if matchETag(r.Header.Get("If-None-Match"), etag) {
		w.WriteHeader(http.StatusNotModified)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"store_id":     menu.StoreID,
		"version":      menu.Version,
		"generated_at": now.UTC(),
		"items":        menu.Items,
	})
}

// matchETag implements If-None-Match comparison for strong ETags.
func matchETag(header, etag string) bool {
	if header == "" {
		return false
	}
	for _, part := range strings.Split(header, ",") {
		part = strings.TrimSpace(part)
		if part == "*" || part == etag {
			return true
		}
	}
	return false
}

// --- heartbeat --------------------------------------------------------------------

func (s *Server) postHeartbeat(w http.ResponseWriter, r *http.Request) {
	screen := r.Context().Value(ctxScreen).(db.Screen)
	if err := s.q.TouchHeartbeat(r.Context(), screen.ID); err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":      "ok",
		"server_time": s.now().UTC(),
	})
}
