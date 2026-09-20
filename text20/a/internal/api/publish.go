package api

import (
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"

	"signalboard/internal/db"
	"signalboard/internal/httpx"
)

type tempPriceReq struct {
	Sku     string `json:"sku"`
	Price   *int64 `json:"price"` // cents
	StartAt string `json:"start_at"`
	EndAt   string `json:"end_at"`
}

type publishReq struct {
	ExpectedVersion *int64         `json:"expected_version"`
	Note            string         `json:"note"`
	TemporaryPrices []tempPriceReq `json:"temporary_prices"`
}

type parsedWindow struct {
	Sku     string
	Price   int64
	StartAt time.Time
	EndAt   time.Time
}

// publishMenu creates a new immutable menu version for a store.
//
// Concurrency: the stores row is locked FOR UPDATE for the whole transaction.
// Two publishes carrying the same expected_version serialize on that lock; the
// first commits and bumps menu_version, the second then observes the new value
// and fails with 409 version_conflict. Only one can succeed.
func (s *Server) publishMenu(w http.ResponseWriter, r *http.Request) {
	storeID, ok := urlID(w, r, "storeID")
	if !ok {
		return
	}
	if _, _, ok := s.loadStore(w, r, storeID); !ok {
		return
	}

	var req publishReq
	if !httpx.DecodeJSON(w, r, &req) {
		return
	}
	if req.ExpectedVersion == nil {
		httpx.ErrorJSON(w, http.StatusBadRequest, "expected_version_required",
			"expected_version must equal the store's current menu_version")
		return
	}

	// Parse and validate every temporary-price window before opening the tx.
	windows, err := parseWindows(req.TemporaryPrices)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusBadRequest, "invalid_temporary_price", err.Error())
		return
	}

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	defer func() { _ = tx.Rollback(r.Context()) }()
	qtx := s.q.WithTx(tx)

	// Row lock pins the current version for this transaction.
	locked, err := qtx.LockStoreForPublish(r.Context(), storeID)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	currentVersion := locked.MenuVersion
	if *req.ExpectedVersion != currentVersion {
		httpx.ErrorJSON(w, http.StatusConflict, "version_conflict",
			fmt.Sprintf("expected_version %d but current menu_version is %d; reload and retry",
				*req.ExpectedVersion, currentVersion))
		return
	}

	newVersion := currentVersion + 1
	ver, err := qtx.CreateMenuVersion(r.Context(), db.CreateMenuVersionParams{
		StoreID: storeID, Version: newVersion, Note: req.Note,
	})
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}

	// Immutable snapshot of the currently active dishes. Editing dishes later
	// never touches this version.
	dishes, err := qtx.ListActiveDishesForPublish(r.Context(), storeID)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	copyRows := make([]db.CopyVersionItemsParams, 0, len(dishes))
	known := make(map[string]struct{}, len(dishes))
	for _, d := range dishes {
		known[d.Sku] = struct{}{}
		copyRows = append(copyRows, db.CopyVersionItemsParams{
			VersionID:    ver.ID,
			Sku:          d.Sku,
			Name:         d.Name,
			BasePrice:    d.BasePrice,
			DisplayOrder: d.SortOrder,
		})
	}
	if len(copyRows) > 0 {
		if _, err := qtx.CopyVersionItems(r.Context(), copyRows); err != nil {
			httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
			return
		}
	}

	// Attach temporary prices to this exact publish.
	for i, win := range windows {
		if _, exists := known[win.Sku]; !exists {
			httpx.ErrorJSON(w, http.StatusBadRequest, "unknown_sku",
				fmt.Sprintf("temporary_prices[%d] references sku %q which is not an active dish in this publish",
					i, win.Sku))
			return
		}
		err := qtx.InsertTemporaryPrice(r.Context(), db.InsertTemporaryPriceParams{
			StoreID:   storeID,
			VersionID: ver.ID,
			Sku:       win.Sku,
			Price:     win.Price,
			StartAt:   ts(win.StartAt),
			EndAt:     ts(win.EndAt),
		})
		if err != nil {
			if isExclusionViolation(err) {
				httpx.ErrorJSON(w, http.StatusConflict, "overlapping_temporary_price",
					fmt.Sprintf("temporary_prices[%d] overlaps an existing window for sku %q", i, win.Sku))
			} else {
				httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
			}
			return
		}
	}

	if err := qtx.BumpStoreVersion(r.Context(), db.BumpStoreVersionParams{
		ID: storeID, MenuVersion: newVersion,
	}); err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}

	httpx.JSON(w, http.StatusCreated, map[string]any{
		"store_id":              storeID,
		"version":               newVersion,
		"published_at":          ver.PublishedAt.Time.UTC().Format(time.RFC3339Nano),
		"item_count":            len(copyRows),
		"temporary_price_count": len(windows),
		"note":                  req.Note,
	})
}

func parseWindows(in []tempPriceReq) ([]parsedWindow, error) {
	out := make([]parsedWindow, 0, len(in))
	for i, w := range in {
		if w.Price == nil {
			return nil, fmt.Errorf("temporary_prices[%d]: price is required (integer cents)", i)
		}
		if *w.Price < 0 {
			return nil, fmt.Errorf("temporary_prices[%d]: price must be >= 0", i)
		}
		start, err := parseOccurredAt(w.StartAt)
		if err != nil {
			return nil, fmt.Errorf("temporary_prices[%d]: %v", i, err)
		}
		end, err := parseOccurredAt(w.EndAt)
		if err != nil {
			return nil, fmt.Errorf("temporary_prices[%d]: %v", i, err)
		}
		if !start.Before(end) {
			return nil, fmt.Errorf("temporary_prices[%d]: start_at must be strictly before end_at (half-open [start,end))", i)
		}
		out = append(out, parsedWindow{
			Sku: w.Sku, Price: *w.Price, StartAt: start, EndAt: end,
		})
	}

	// Reject overlaps inside the request itself. Windows are half-open, so
	// adjacent windows ([a,b) followed by [b,c)) do NOT overlap.
	bySku := map[string][]parsedWindow{}
	for _, w := range out {
		bySku[w.Sku] = append(bySku[w.Sku], w)
	}
	for sku, ws := range bySku {
		sort.Slice(ws, func(i, j int) bool { return ws[i].StartAt.Before(ws[j].StartAt) })
		for i := 1; i < len(ws); i++ {
			if ws[i].StartAt.Before(ws[i-1].EndAt) {
				return nil, fmt.Errorf("overlapping temporary prices for sku %q: [%s,%s) overlaps [%s,%s)",
					sku,
					ws[i-1].StartAt.Format(time.RFC3339), ws[i-1].EndAt.Format(time.RFC3339),
					ws[i].StartAt.Format(time.RFC3339), ws[i].EndAt.Format(time.RFC3339))
			}
		}
	}
	return out, nil
}

func (s *Server) listVersions(w http.ResponseWriter, r *http.Request) {
	storeID, ok := urlID(w, r, "storeID")
	if !ok {
		return
	}
	if _, _, ok := s.loadStore(w, r, storeID); !ok {
		return
	}
	versions, err := s.q.ListVersions(r.Context(), storeID)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	httpx.JSON(w, http.StatusOK, versions)
}

// getVersion returns one immutable published version with its full contents.
func (s *Server) getVersion(w http.ResponseWriter, r *http.Request) {
	storeID, ok := urlID(w, r, "storeID")
	if !ok {
		return
	}
	version, err := strconv.ParseInt(chi.URLParam(r, "version"), 10, 64)
	if err != nil || version <= 0 {
		httpx.ErrorJSON(w, http.StatusBadRequest, "invalid_version", "version must be a positive integer")
		return
	}
	if _, _, ok := s.loadStore(w, r, storeID); !ok {
		return
	}
	ver, err := s.q.GetVersionByNumber(r.Context(), db.GetVersionByNumberParams{
		StoreID: storeID, Version: version,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		httpx.ErrorJSON(w, http.StatusNotFound, "version_not_found", "menu version does not exist")
		return
	}
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	items, err := s.q.ListVersionItems(r.Context(), ver.ID)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	prices, err := s.q.ListTempPricesForVersion(r.Context(), ver.ID)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{
		"version":          ver,
		"items":            items,
		"temporary_prices": prices,
	})
}
