package api

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"signalboard/internal/db"
	"signalboard/internal/httpx"
)

type menuItemView struct {
	Sku          string `json:"sku"`
	Name         string `json:"name"`
	BasePrice    int64  `json:"base_price"`    // cents
	DisplayPrice int64  `json:"display_price"` // cents, after temporary price
	OnTemporary  bool   `json:"on_temporary_price"`
	SoldOut      bool   `json:"sold_out"`
	DisplayOrder int32  `json:"display_order"`
}

type menuView struct {
	StoreID     int64          `json:"store_id"`
	Version     int64          `json:"menu_version"`
	EffectiveAt string         `json:"effective_at"`
	LocalDay    string         `json:"local_day"`
	Items       []menuItemView `json:"items"`
}

// getScreenMenu serves the complete, currently-effective menu to a physical
// screen. It is the screen's reconnect path as well as its normal poll, so it
// always returns the FULL latest menu (never a delta).
//
// Consistency: all data is read inside one READ ONLY REPEATABLE READ
// transaction, giving a single snapshot. A response therefore cannot mix an
// old version with new prices or a fresh sold-out flag with stale items.
//
// Caching: the ETag is a digest of exactly the inputs that change what a
// screen must render — the published version number, the local day, the set
// of temporary prices in effect at the evaluated instant, and the sold-out
// set. It therefore stays stable across polls, but a publish, a price window
// crossing its boundary (no republish needed), a sold-out transition, or a
// cross-day recovery all force a new ETag and a full 200. If-None-Match yields
// 304 while nothing renderable changed.
func (s *Server) getScreenMenu(w http.ResponseWriter, r *http.Request) {
	screen := screenFromCtx(r.Context())
	storeID := screen.StoreID

	var view menuView
	var etag string

	err := s.snapshot(r.Context(), func(q *db.Queries) error {
		store, err := q.GetStore(r.Context(), storeID)
		if err != nil {
			return err
		}
		loc, err := time.LoadLocation(store.Timezone)
		if err != nil {
			return err
		}

		// One evaluation instant for this whole snapshot.
		now := nowFn()
		y, m, dd := now.In(loc).Date()
		day := date(y, m, dd)
		view = menuView{
			StoreID:     storeID,
			Version:     store.MenuVersion,
			EffectiveAt: now.Format(time.RFC3339Nano),
			LocalDay:    day.Time.Format("2006-01-02"),
			Items:       []menuItemView{},
		}

		// Today's sold-out markers (day-scoped => automatic cross-day recovery).
		markers, err := q.ListSoldOutsForDay(r.Context(), db.ListSoldOutsForDayParams{
			StoreID: storeID, SalesDay: day,
		})
		if err != nil {
			return err
		}
		soldOut := make(map[string]struct{}, len(markers))
		for _, mk := range markers {
			soldOut[mk.Sku] = struct{}{}
		}

		// No published menu yet: version 0 with an empty item list is valid.
		if store.MenuVersion == 0 {
			etag = computeETag(0, view.LocalDay, nil, soldOut)
			return nil
		}

		ver, err := q.GetVersionByNumber(r.Context(), db.GetVersionByNumberParams{
			StoreID: storeID, Version: store.MenuVersion,
		})
		if err != nil {
			return err
		}
		items, err := q.ListVersionItems(r.Context(), ver.ID)
		if err != nil {
			return err
		}
		// Temporary prices in effect at this instant. Half-open windows mean a
		// switch at a boundary needs no republish: the active set simply
		// changes as now crosses start_at/end_at.
		active, err := q.ListActiveTempPrices(r.Context(), db.ListActiveTempPricesParams{
			VersionID: ver.ID, StartAt: ts(now),
		})
		if err != nil {
			return err
		}
		priceBySku := make(map[string]int64, len(active))
		for _, tp := range active {
			priceBySku[tp.Sku] = tp.Price
		}

		view.Items = make([]menuItemView, 0, len(items))
		for _, it := range items {
			display := it.BasePrice
			onTemp := false
			if p, ok := priceBySku[it.Sku]; ok {
				display = p
				onTemp = true
			}
			_, isSoldOut := soldOut[it.Sku]
			view.Items = append(view.Items, menuItemView{
				Sku:          it.Sku,
				Name:         it.Name,
				BasePrice:    it.BasePrice,
				DisplayPrice: display,
				OnTemporary:  onTemp,
				SoldOut:      isSoldOut,
				DisplayOrder: it.DisplayOrder,
			})
		}

		etag = computeETag(view.Version, view.LocalDay, active, soldOut)
		return nil
	})

	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			httpx.ErrorJSON(w, http.StatusNotFound, "not_published", "menu version is missing")
			return
		}
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}

	w.Header().Set("ETag", etag)
	// Screens must revalidate; bodies are cheap to skip via 304.
	w.Header().Set("Cache-Control", "no-cache")

	if match := strings.TrimSpace(r.Header.Get("If-None-Match")); match != "" {
		if etagMatches(etag, match) {
			w.WriteHeader(http.StatusNotModified)
			return
		}
	}
	// If-Match is supported for completeness: a stale caller gets 412 rather
	// than a silently different state.
	if match := strings.TrimSpace(r.Header.Get("If-Match")); match != "" && match != "*" {
		if !etagMatches(etag, match) {
			httpx.ErrorJSON(w, http.StatusPreconditionFailed, "precondition_failed",
				"current menu state no longer matches If-Match")
			return
		}
	}

	httpx.JSON(w, http.StatusOK, view)
}

// screenHeartbeat marks the screen alive and reports whether it is considered
// online along with the current version, so a reconnecting screen knows it
// must pull the full menu.
func (s *Server) screenHeartbeat(w http.ResponseWriter, r *http.Request) {
	screen := screenFromCtx(r.Context())
	if err := s.q.HeartbeatScreen(r.Context(), db.HeartbeatScreenParams{
		ID: screen.ID, LastHeartbeat: ts(nowFn()),
	}); err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	store, err := s.q.GetStore(r.Context(), screen.StoreID)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{
		"screen_id":    screen.ID,
		"online":       true,
		"menu_version": store.MenuVersion,
		"fetch":        "GET /v1/screen/menu for the latest full menu",
	})
}

// screenStatus reports liveness for the authenticated screen.
func (s *Server) screenStatus(w http.ResponseWriter, r *http.Request) {
	screen := screenFromCtx(r.Context())
	online := false
	var lastHB any
	if screen.LastHeartbeat.Valid {
		hb := screen.LastHeartbeat.Time.UTC()
		online = nowFn().Sub(hb) <= s.cfg.ScreenOfflineAfter
		lastHB = hb.Format(time.RFC3339Nano)
	}
	store, err := s.q.GetStore(r.Context(), screen.StoreID)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{
		"screen_id":                 screen.ID,
		"online":                    online,
		"last_heartbeat":            lastHB,
		"offline_threshold_seconds": int64(s.cfg.ScreenOfflineAfter.Seconds()),
		"menu_version":              store.MenuVersion,
	})
}

// activePrice is the subset of a temporary price row used for the ETag.
type activePrice struct {
	Sku   string
	ID    int64
	Price int64
}

// computeETag hashes the render-affecting state. Price windows are identified
// by row id and price, and only those active at the evaluated instant are
// included, so crossing a time boundary changes the digest without a
// republish. The local day participates so cross-day recovery changes it too.
func computeETag(version int64, localDay string, active []db.ListActiveTempPricesRow, soldOut map[string]struct{}) string {
	var b strings.Builder
	b.WriteString("v")
	b.WriteString(itoa64(version))
	b.WriteString("|d")
	b.WriteString(localDay)

	prices := make([]activePrice, 0, len(active))
	for _, tp := range active {
		prices = append(prices, activePrice{Sku: tp.Sku, ID: tp.ID, Price: tp.Price})
	}
	sort.Slice(prices, func(i, j int) bool {
		if prices[i].Sku != prices[j].Sku {
			return prices[i].Sku < prices[j].Sku
		}
		return prices[i].ID < prices[j].ID
	})
	for _, p := range prices {
		b.WriteString("|p")
		b.WriteString(p.Sku)
		b.WriteString(":")
		b.WriteString(itoa64(p.ID))
		b.WriteString(":")
		b.WriteString(itoa64(p.Price))
	}

	skus := make([]string, 0, len(soldOut))
	for sku := range soldOut {
		skus = append(skus, sku)
	}
	sort.Strings(skus)
	for _, sku := range skus {
		b.WriteString("|s")
		b.WriteString(sku)
	}

	sum := sha256.Sum256([]byte(b.String()))
	return `"` + hex.EncodeToString(sum[:]) + `"`
}

// etagMatches compares against an If-None-Match/If-Match header, honoring the
// wildcard and comma-separated lists.
func etagMatches(etag, header string) bool {
	for _, part := range strings.Split(header, ",") {
		t := strings.TrimSpace(part)
		if t == "*" {
			return true
		}
		if t == etag {
			return true
		}
	}
	return false
}

func itoa64(v int64) string {
	return strconv.FormatInt(v, 10)
}
