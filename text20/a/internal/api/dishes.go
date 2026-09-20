package api

import (
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5"

	"signalboard/internal/db"
	"signalboard/internal/httpx"
)

type dishReq struct {
	Sku        string `json:"sku"`
	Name       string `json:"name"`
	BasePrice  *int64 `json:"base_price"` // cents
	Active     *bool  `json:"active"`
	DailyLimit *int64 `json:"daily_limit"` // nullable: nil keeps/means no limit
	SortOrder  *int32 `json:"sort_order"`
}

func (d dishReq) boolOr(def bool) bool {
	if d.Active != nil {
		return *d.Active
	}
	return def
}

func (d dishReq) int32Or(def int32) int32 {
	if d.SortOrder != nil {
		return *d.SortOrder
	}
	return def
}

func validateDishFields(req dishReq) error {
	if strings.TrimSpace(req.Sku) == "" {
		return errors.New("sku is required")
	}
	if len(req.Sku) > 128 {
		return errors.New("sku must be at most 128 characters")
	}
	if strings.TrimSpace(req.Name) == "" {
		return errors.New("name is required")
	}
	if req.BasePrice == nil {
		return errors.New("base_price is required (integer cents)")
	}
	if *req.BasePrice < 0 {
		return errors.New("base_price must be >= 0 cents")
	}
	if req.DailyLimit != nil && *req.DailyLimit <= 0 {
		return errors.New("daily_limit must be a positive integer or omitted")
	}
	return nil
}

func (s *Server) createDish(w http.ResponseWriter, r *http.Request) {
	storeID, ok := urlID(w, r, "storeID")
	if !ok {
		return
	}
	if _, _, ok := s.loadStore(w, r, storeID); !ok {
		return
	}
	var req dishReq
	if !httpx.DecodeJSON(w, r, &req) {
		return
	}
	if err := validateDishFields(req); err != nil {
		httpx.ErrorJSON(w, http.StatusBadRequest, "invalid_dish", err.Error())
		return
	}
	dish, err := s.q.CreateDish(r.Context(), db.CreateDishParams{
		StoreID:    storeID,
		Sku:        strings.TrimSpace(req.Sku),
		Name:       strings.TrimSpace(req.Name),
		BasePrice:  *req.BasePrice,
		Active:     req.boolOr(true),
		DailyLimit: limitArg(req.DailyLimit),
		SortOrder:  req.int32Or(0),
	})
	if err != nil {
		writeWriteError(w, err)
		return
	}
	httpx.JSON(w, http.StatusCreated, dish)
}

func (s *Server) listDishes(w http.ResponseWriter, r *http.Request) {
	storeID, ok := urlID(w, r, "storeID")
	if !ok {
		return
	}
	if _, _, ok := s.loadStore(w, r, storeID); !ok {
		return
	}
	dishes, err := s.q.ListDishes(r.Context(), storeID)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	httpx.JSON(w, http.StatusOK, dishes)
}

func (s *Server) updateDish(w http.ResponseWriter, r *http.Request) {
	storeID, ok := urlID(w, r, "storeID")
	if !ok {
		return
	}
	dishID, ok := urlID(w, r, "dishID")
	if !ok {
		return
	}
	if _, _, ok := s.loadStore(w, r, storeID); !ok {
		return
	}
	existing, err := s.q.GetDish(r.Context(), db.GetDishParams{ID: dishID, StoreID: storeID})
	if errors.Is(err, pgx.ErrNoRows) {
		httpx.ErrorJSON(w, http.StatusNotFound, "dish_not_found", "dish does not exist in this store")
		return
	}
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}

	var req dishReq
	if !httpx.DecodeJSON(w, r, &req) {
		return
	}
	if err := validateDishFields(req); err != nil {
		httpx.ErrorJSON(w, http.StatusBadRequest, "invalid_dish", err.Error())
		return
	}
	updated, err := s.q.UpdateDish(r.Context(), db.UpdateDishParams{
		ID:         existing.ID,
		Name:       strings.TrimSpace(req.Name),
		BasePrice:  *req.BasePrice,
		Active:     req.boolOr(existing.Active),
		DailyLimit: limitArg(req.DailyLimit),
		SortOrder:  req.int32Or(existing.SortOrder),
	})
	if err != nil {
		writeWriteError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, updated)
}

func (s *Server) deleteDish(w http.ResponseWriter, r *http.Request) {
	storeID, ok := urlID(w, r, "storeID")
	if !ok {
		return
	}
	dishID, ok := urlID(w, r, "dishID")
	if !ok {
		return
	}
	if err := s.q.DeleteDish(r.Context(), db.DeleteDishParams{ID: dishID, StoreID: storeID}); err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

type importItem struct {
	Sku        string `json:"sku"`
	Name       string `json:"name"`
	BasePrice  *int64 `json:"base_price"`
	Active     *bool  `json:"active"`
	DailyLimit *int64 `json:"daily_limit"`
	SortOrder  *int32 `json:"sort_order"`
}

type importReq struct {
	Items []importItem `json:"items"`
}

// MaxImportItems bounds a single import batch.
const MaxImportItems = 500

func (s *Server) importDishes(w http.ResponseWriter, r *http.Request) {
	storeID, ok := urlID(w, r, "storeID")
	if !ok {
		return
	}
	if _, _, ok := s.loadStore(w, r, storeID); !ok {
		return
	}

	var req importReq
	if !httpx.DecodeJSON(w, r, &req) {
		return
	}
	if len(req.Items) == 0 {
		httpx.ErrorJSON(w, http.StatusBadRequest, "empty_batch", "items must contain at least one dish")
		return
	}
	if len(req.Items) > MaxImportItems {
		httpx.ErrorJSON(w, http.StatusBadRequest, "batch_too_large",
			fmt.Sprintf("at most %d items per import, got %d", MaxImportItems, len(req.Items)))
		return
	}

	// Validate the whole batch up front and reject duplicate SKUs within the
	// batch so its meaning is unambiguous. Nothing is written yet.
	seen := make(map[string]int, len(req.Items))
	for i, it := range req.Items {
		d := dishReq{
			Sku: it.Sku, Name: it.Name, BasePrice: it.BasePrice,
			Active: it.Active, DailyLimit: it.DailyLimit, SortOrder: it.SortOrder,
		}
		if err := validateDishFields(d); err != nil {
			httpx.ErrorJSON(w, http.StatusBadRequest, "invalid_item",
				fmt.Sprintf("item %d (sku=%q): %s", i, it.Sku, err.Error()))
			return
		}
		key := strings.TrimSpace(it.Sku)
		if first, dup := seen[key]; dup {
			httpx.ErrorJSON(w, http.StatusBadRequest, "duplicate_sku",
				fmt.Sprintf("sku %q appears at items %d and %d", key, first, i))
			return
		}
		seen[key] = i
	}

	// One transaction for the whole batch: any constraint or DB error rolls
	// back every upsert, so an import is all-or-nothing.
	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	defer func() { _ = tx.Rollback(r.Context()) }()
	qtx := s.q.WithTx(tx)

	upserted := make([]db.Dish, 0, len(req.Items))
	for i, it := range req.Items {
		dish, err := qtx.UpsertDish(r.Context(), db.UpsertDishParams{
			StoreID:    storeID,
			Sku:        strings.TrimSpace(it.Sku),
			Name:       strings.TrimSpace(it.Name),
			BasePrice:  *it.BasePrice,
			Active:     it.Active == nil || *it.Active,
			DailyLimit: limitArg(it.DailyLimit),
			SortOrder: func() int32 {
				if it.SortOrder != nil {
					return *it.SortOrder
				}
				return int32(i)
			}(),
		})
		if err != nil {
			httpx.ErrorJSON(w, http.StatusBadRequest, "import_failed",
				fmt.Sprintf("item %d (sku=%q) failed; whole batch rolled back: %v", i, it.Sku, err))
			return
		}
		upserted = append(upserted, dish)
	}
	if err := tx.Commit(r.Context()); err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{
		"imported": len(upserted),
		"items":    upserted,
	})
}

// limitArg converts an optional nullable limit into a pgtype.Int8.
func limitArg(p *int64) (out pgInt8) {
	if p == nil {
		return pgInt8{Valid: false}
	}
	return pgInt8{Int64: *p, Valid: true}
}

func writeWriteError(w http.ResponseWriter, err error) {
	if isUniqueViolation(err) {
		httpx.ErrorJSON(w, http.StatusConflict, "sku_conflict", "a dish with this sku already exists in the store")
		return
	}
	httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
}
