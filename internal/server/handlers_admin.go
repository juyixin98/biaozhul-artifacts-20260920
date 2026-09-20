package server

import (
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"signalboard/internal/db"
)

// --- response DTOs ------------------------------------------------------------

type storeResp struct {
	ID             uuid.UUID `json:"id"`
	Name           string    `json:"name"`
	Timezone       string    `json:"timezone"`
	CurrentVersion int32     `json:"current_version"`
	CreatedAt      time.Time `json:"created_at"`
}

func toStoreResp(s db.Store) storeResp {
	return storeResp{s.ID, s.Name, s.Timezone, s.CurrentVersion, s.CreatedAt.Time}
}

type itemResp struct {
	ID               uuid.UUID `json:"id"`
	StoreID          uuid.UUID `json:"store_id"`
	Name             string    `json:"name"`
	Description      string    `json:"description"`
	Category         string    `json:"category"`
	PriceCents       int32     `json:"price_cents"`
	SoldOutThreshold int32     `json:"sold_out_threshold"`
	Position         int32     `json:"position"`
	CreatedAt        time.Time `json:"created_at"`
	UpdatedAt        time.Time `json:"updated_at"`
}

func toItemResp(i db.Item) itemResp {
	return itemResp{i.ID, i.StoreID, i.Name, i.Description, i.Category,
		i.PriceCents, i.SoldOutThreshold, i.Position, i.CreatedAt.Time, i.UpdatedAt.Time}
}

// --- stores -------------------------------------------------------------------

type createStoreReq struct {
	Name     string `json:"name"`
	Timezone string `json:"timezone"`
}

func (s *Server) createStore(w http.ResponseWriter, r *http.Request) {
	var req createStoreReq
	if !decodeJSON(w, r, &req) {
		return
	}
	req.Name = strings.TrimSpace(req.Name)
	if req.Name == "" {
		writeErr(w, http.StatusBadRequest, "validation", "name is required")
		return
	}
	if _, err := time.LoadLocation(req.Timezone); err != nil {
		writeErr(w, http.StatusBadRequest, "validation", "invalid IANA timezone: "+req.Timezone)
		return
	}
	st, err := s.q.CreateStore(r.Context(), db.CreateStoreParams{Name: req.Name, Timezone: req.Timezone})
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, toStoreResp(st))
}

func (s *Server) listStores(w http.ResponseWriter, r *http.Request) {
	stores, err := s.q.ListStores(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]storeResp, len(stores))
	for i, st := range stores {
		out[i] = toStoreResp(st)
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) getStore(w http.ResponseWriter, r *http.Request) {
	id, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	st, err := s.q.GetStore(r.Context(), id)
	if noRows(err) {
		writeErr(w, http.StatusNotFound, "not_found", "store not found")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, toStoreResp(st))
}

// --- draft items ----------------------------------------------------------------

type itemReq struct {
	Name             string `json:"name"`
	Description      string `json:"description"`
	Category         string `json:"category"`
	PriceCents       int32  `json:"price_cents"`
	SoldOutThreshold int32  `json:"sold_out_threshold"`
	Position         int32  `json:"position"`
}

func (ir *itemReq) validate() string {
	if strings.TrimSpace(ir.Name) == "" {
		return "name is required"
	}
	if ir.PriceCents < 0 {
		return "price_cents must be >= 0"
	}
	if ir.SoldOutThreshold < 0 {
		return "sold_out_threshold must be >= 0"
	}
	return ""
}

func (s *Server) createItem(w http.ResponseWriter, r *http.Request) {
	storeID, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	var req itemReq
	if !decodeJSON(w, r, &req) {
		return
	}
	if msg := req.validate(); msg != "" {
		writeErr(w, http.StatusBadRequest, "validation", msg)
		return
	}
	item, err := s.q.CreateItem(r.Context(), db.CreateItemParams{
		StoreID: storeID, Name: req.Name, Description: req.Description,
		Category: req.Category, PriceCents: req.PriceCents,
		SoldOutThreshold: req.SoldOutThreshold, Position: req.Position,
	})
	if isFKViolation(err) {
		writeErr(w, http.StatusNotFound, "not_found", "store not found")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, toItemResp(item))
}

func (s *Server) listDraftItems(w http.ResponseWriter, r *http.Request) {
	storeID, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	items, err := s.q.ListDraftItems(r.Context(), storeID)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]itemResp, len(items))
	for i, it := range items {
		out[i] = toItemResp(it)
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) updateItem(w http.ResponseWriter, r *http.Request) {
	storeID, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	itemID, ok := pathUUID(w, r, "itemID")
	if !ok {
		return
	}
	var req itemReq
	if !decodeJSON(w, r, &req) {
		return
	}
	if msg := req.validate(); msg != "" {
		writeErr(w, http.StatusBadRequest, "validation", msg)
		return
	}
	item, err := s.q.UpdateItem(r.Context(), db.UpdateItemParams{
		ID: itemID, StoreID: storeID, Name: req.Name, Description: req.Description,
		Category: req.Category, PriceCents: req.PriceCents,
		SoldOutThreshold: req.SoldOutThreshold, Position: req.Position,
	})
	if noRows(err) {
		writeErr(w, http.StatusNotFound, "not_found", "item not found")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, toItemResp(item))
}

func (s *Server) deleteItem(w http.ResponseWriter, r *http.Request) {
	storeID, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	itemID, ok := pathUUID(w, r, "itemID")
	if !ok {
		return
	}
	if err := s.q.DeleteItem(r.Context(), db.DeleteItemParams{ID: itemID, StoreID: storeID}); err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// --- batch import ---------------------------------------------------------------

type batchImportReq struct {
	Items []itemReq `json:"items"`
}

func (s *Server) batchImportItems(w http.ResponseWriter, r *http.Request) {
	storeID, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	var req batchImportReq
	if !decodeJSON(w, r, &req) {
		return
	}
	if len(req.Items) == 0 {
		writeErr(w, http.StatusBadRequest, "validation", "items must not be empty")
		return
	}
	if len(req.Items) > MaxBatchImportItems {
		writeErr(w, http.StatusBadRequest, "validation",
			fmt.Sprintf("batch import limited to %d items, got %d", MaxBatchImportItems, len(req.Items)))
		return
	}
	for i := range req.Items {
		if msg := req.Items[i].validate(); msg != "" {
			writeErr(w, http.StatusBadRequest, "validation",
				fmt.Sprintf("items[%d]: %s", i, msg))
			return
		}
	}

	created := make([]db.Item, 0, len(req.Items))
	err := s.withTx(r.Context(), pgx.ReadCommitted, func(q *db.Queries) error {
		// Fail fast with 404 if the store does not exist; the lock also
		// keeps the batch consistent with concurrent publishes.
		if _, err := q.LockStore(r.Context(), storeID); err != nil {
			return err
		}
		for _, ir := range req.Items {
			item, err := q.CreateItem(r.Context(), db.CreateItemParams{
				StoreID: storeID, Name: ir.Name, Description: ir.Description,
				Category: ir.Category, PriceCents: ir.PriceCents,
				SoldOutThreshold: ir.SoldOutThreshold, Position: ir.Position,
			})
			if err != nil {
				return err // any failure rolls the whole batch back
			}
			created = append(created, item)
		}
		return nil
	})
	if noRows(err) {
		writeErr(w, http.StatusNotFound, "not_found", "store not found")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]itemResp, len(created))
	for i, it := range created {
		out[i] = toItemResp(it)
	}
	writeJSON(w, http.StatusCreated, map[string]any{"imported": len(out), "items": out})
}

// --- publishing -----------------------------------------------------------------

type publishReq struct {
	ExpectedVersion int32 `json:"expected_version"`
}

type publishResp struct {
	StoreID     uuid.UUID `json:"store_id"`
	Version     int32     `json:"version"`
	ItemCount   int       `json:"item_count"`
	PublishedAt time.Time `json:"published_at"`
}

var errVersionConflict = errors.New("version conflict")

func (s *Server) publish(w http.ResponseWriter, r *http.Request) {
	storeID, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	var req publishReq
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.ExpectedVersion < 0 {
		writeErr(w, http.StatusBadRequest, "validation", "expected_version must be >= 0")
		return
	}

	var resp publishResp
	err := s.withTx(r.Context(), pgx.ReadCommitted, func(q *db.Queries) error {
		// Locking the store row serializes concurrent publishes: only one
		// transaction can hold the lock, so the expected_version check and
		// the version bump are atomic.
		st, err := q.LockStore(r.Context(), storeID)
		if err != nil {
			return err
		}
		if st.CurrentVersion != req.ExpectedVersion {
			return errVersionConflict
		}
		next := st.CurrentVersion + 1
		mv, err := q.CreateMenuVersion(r.Context(), db.CreateMenuVersionParams{StoreID: storeID, Version: next})
		if err != nil {
			return err
		}
		if err := q.SnapshotDraftItems(r.Context(), db.SnapshotDraftItemsParams{VersionID: mv.ID, StoreID: storeID}); err != nil {
			return err
		}
		if err := q.SetStoreVersion(r.Context(), db.SetStoreVersionParams{ID: storeID, CurrentVersion: next}); err != nil {
			return err
		}
		items, err := q.ListVersionItems(r.Context(), mv.ID)
		if err != nil {
			return err
		}
		resp = publishResp{StoreID: storeID, Version: next, ItemCount: len(items), PublishedAt: mv.CreatedAt.Time}
		return nil
	})
	if noRows(err) {
		writeErr(w, http.StatusNotFound, "not_found", "store not found")
		return
	}
	if errors.Is(err, errVersionConflict) {
		writeErr(w, http.StatusConflict, "version_conflict",
			"expected_version does not match the store's current version")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, resp)
}

func (s *Server) listVersions(w http.ResponseWriter, r *http.Request) {
	storeID, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	versions, err := s.q.ListVersions(r.Context(), storeID)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]map[string]any, len(versions))
	for i, v := range versions {
		out[i] = map[string]any{"id": v.ID, "version": v.Version, "created_at": v.CreatedAt.Time}
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) getVersion(w http.ResponseWriter, r *http.Request) {
	storeID, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	n, err := strconv.Atoi(chi.URLParam(r, "version"))
	if err != nil || n < 1 {
		writeErr(w, http.StatusBadRequest, "bad_request", "invalid version")
		return
	}
	mv, err := s.q.GetVersion(r.Context(), db.GetVersionParams{StoreID: storeID, Version: int32(n)})
	if noRows(err) {
		writeErr(w, http.StatusNotFound, "not_found", "version not found")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	items, err := s.q.ListVersionItems(r.Context(), mv.ID)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]map[string]any, len(items))
	for i, it := range items {
		out[i] = map[string]any{
			"item_id": it.ItemID, "name": it.Name, "description": it.Description,
			"category": it.Category, "price_cents": it.PriceCents,
			"sold_out_threshold": it.SoldOutThreshold, "position": it.Position,
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"store_id": mv.StoreID, "version": mv.Version, "created_at": mv.CreatedAt.Time, "items": out,
	})
}

// --- temporary prices -----------------------------------------------------------

type tempPriceReq struct {
	ItemID     uuid.UUID `json:"item_id"`
	PriceCents int32     `json:"price_cents"`
	StartsAt   time.Time `json:"starts_at"` // RFC3339, any UTC offset; stored as UTC
	EndsAt     time.Time `json:"ends_at"`
}

type tempPriceResp struct {
	ID         uuid.UUID `json:"id"`
	StoreID    uuid.UUID `json:"store_id"`
	ItemID     uuid.UUID `json:"item_id"`
	PriceCents int32     `json:"price_cents"`
	StartsAt   time.Time `json:"starts_at"`
	EndsAt     time.Time `json:"ends_at"`
	CreatedAt  time.Time `json:"created_at"`
}

func toTempPriceResp(t db.CreateTempPriceRow) tempPriceResp {
	return tempPriceResp{t.ID, t.StoreID, t.ItemID, t.PriceCents, t.StartsAt.Time, t.EndsAt.Time, t.CreatedAt.Time}
}

func (s *Server) createTempPrice(w http.ResponseWriter, r *http.Request) {
	storeID, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	var req tempPriceReq
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.PriceCents < 0 {
		writeErr(w, http.StatusBadRequest, "validation", "price_cents must be >= 0")
		return
	}
	if req.StartsAt.IsZero() || req.EndsAt.IsZero() || !req.EndsAt.After(req.StartsAt) {
		writeErr(w, http.StatusBadRequest, "validation", "require starts_at < ends_at (half-open interval)")
		return
	}
	if _, err := s.q.GetItem(r.Context(), db.GetItemParams{ID: req.ItemID, StoreID: storeID}); noRows(err) {
		writeErr(w, http.StatusNotFound, "not_found", "item not found in this store")
		return
	} else if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	tp, err := s.q.CreateTempPrice(r.Context(), db.CreateTempPriceParams{
		StoreID: storeID, ItemID: req.ItemID, PriceCents: req.PriceCents,
		StartsAt: timestamptz(req.StartsAt), EndsAt: timestamptz(req.EndsAt),
	})
	if isExclusionViolation(err) {
		writeErr(w, http.StatusConflict, "overlap",
			"temporary price window overlaps an existing window for this item")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, toTempPriceResp(tp))
}

func (s *Server) listTempPrices(w http.ResponseWriter, r *http.Request) {
	storeID, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	rows, err := s.q.ListTempPrices(r.Context(), storeID)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]tempPriceResp, len(rows))
	for i, t := range rows {
		out[i] = tempPriceResp{t.ID, t.StoreID, t.ItemID, t.PriceCents, t.StartsAt.Time, t.EndsAt.Time, t.CreatedAt.Time}
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) deleteTempPrice(w http.ResponseWriter, r *http.Request) {
	storeID, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	id, ok := pathUUID(w, r, "tempPriceID")
	if !ok {
		return
	}
	if err := s.q.DeleteTempPrice(r.Context(), db.DeleteTempPriceParams{ID: id, StoreID: storeID}); err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// --- sales events ---------------------------------------------------------------

type salesEventReq struct {
	EventID    string    `json:"event_id"`
	ItemID     uuid.UUID `json:"item_id"`
	Quantity   int32     `json:"quantity"`
	OccurredAt time.Time `json:"occurred_at"`
}

type salesEventResp struct {
	EventID       string    `json:"event_id"`
	ItemID        uuid.UUID `json:"item_id"`
	SaleDate      string    `json:"sale_date"`
	DailyQuantity int64     `json:"daily_quantity"`
	Duplicate     bool      `json:"duplicate"`
}

func (s *Server) createSalesEvent(w http.ResponseWriter, r *http.Request) {
	storeID, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	var req salesEventReq
	if !decodeJSON(w, r, &req) {
		return
	}
	if strings.TrimSpace(req.EventID) == "" {
		writeErr(w, http.StatusBadRequest, "validation", "event_id is required")
		return
	}
	if req.Quantity <= 0 {
		writeErr(w, http.StatusBadRequest, "validation", "quantity must be > 0")
		return
	}
	if req.OccurredAt.IsZero() {
		writeErr(w, http.StatusBadRequest, "validation", "occurred_at is required")
		return
	}

	var resp salesEventResp
	var conflict bool
	err := s.withTx(r.Context(), pgx.ReadCommitted, func(q *db.Queries) error {
		st, err := q.LockStore(r.Context(), storeID)
		if err != nil {
			return err
		}
		if _, err := q.GetItem(r.Context(), db.GetItemParams{ID: req.ItemID, StoreID: storeID}); err != nil {
			return err
		}
		loc, err := time.LoadLocation(st.Timezone)
		if err != nil {
			return fmt.Errorf("store timezone: %w", err)
		}
		// Late events are attributed to the day they actually occurred,
		// in the store's local timezone.
		saleDay := req.OccurredAt.In(loc)
		saleDate := saleDay.Format("2006-01-02")
		hash := salesContentHash(req)

		eventID, err := q.InsertSalesEvent(r.Context(), db.InsertSalesEventParams{
			StoreID: storeID, EventID: req.EventID, ItemID: req.ItemID,
			Quantity: req.Quantity, OccurredAt: timestamptz(req.OccurredAt),
			SaleDate: pgdate(saleDay), ContentHash: hash,
		})
		if noRows(err) {
			// The event id already exists: identical payload -> idempotent
			// replay; different payload -> conflict.
			existing, gerr := q.GetSalesEvent(r.Context(), db.GetSalesEventParams{StoreID: storeID, EventID: req.EventID})
			if gerr != nil {
				return gerr
			}
			if existing.ContentHash != hash {
				conflict = true
				return nil
			}
			qty, gerr := currentDailyQty(r, q, storeID, req.ItemID, existing.SaleDate.Time)
			if gerr != nil {
				return gerr
			}
			resp = salesEventResp{EventID: req.EventID, ItemID: req.ItemID,
				SaleDate: existing.SaleDate.Time.Format("2006-01-02"), DailyQuantity: qty, Duplicate: true}
			return nil
		}
		if err != nil {
			return err
		}
		_ = eventID
		qty, err := q.AddDailySales(r.Context(), db.AddDailySalesParams{
			StoreID: storeID, ItemID: req.ItemID, SaleDate: pgdate(saleDay), Quantity: int64(req.Quantity),
		})
		if err != nil {
			return err
		}
		resp = salesEventResp{EventID: req.EventID, ItemID: req.ItemID,
			SaleDate: saleDate, DailyQuantity: qty, Duplicate: false}
		return nil
	})
	if noRows(err) {
		writeErr(w, http.StatusNotFound, "not_found", "store or item not found")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	if conflict {
		writeErr(w, http.StatusConflict, "event_conflict",
			"event_id already recorded with different content")
		return
	}
	status := http.StatusCreated
	if resp.Duplicate {
		status = http.StatusOK
	}
	writeJSON(w, status, resp)
}

func currentDailyQty(r *http.Request, q *db.Queries, storeID, itemID uuid.UUID, day time.Time) (int64, error) {
	rows, err := q.GetDailySales(r.Context(), db.GetDailySalesParams{StoreID: storeID, SaleDate: pgdate(day)})
	if err != nil {
		return 0, err
	}
	for _, row := range rows {
		if row.ItemID == itemID {
			return row.Quantity, nil
		}
	}
	return 0, nil
}

func (s *Server) getDailySales(w http.ResponseWriter, r *http.Request) {
	storeID, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	date := r.URL.Query().Get("date")
	if date == "" {
		date = s.now().Format("2006-01-02")
	}
	day, err := time.Parse("2006-01-02", date)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad_request", "date must be YYYY-MM-DD")
		return
	}
	rows, err := s.q.GetDailySales(r.Context(), db.GetDailySalesParams{StoreID: storeID, SaleDate: pgdate(day)})
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]map[string]any, len(rows))
	for i, row := range rows {
		out[i] = map[string]any{"item_id": row.ItemID, "quantity": row.Quantity}
	}
	writeJSON(w, http.StatusOK, map[string]any{"store_id": storeID, "date": date, "items": out})
}

// --- screens (admin) --------------------------------------------------------------

type screenResp struct {
	ID              uuid.UUID  `json:"id"`
	StoreID         uuid.UUID  `json:"store_id"`
	Name            string     `json:"name"`
	LastHeartbeatAt *time.Time `json:"last_heartbeat_at"`
	Online          bool       `json:"online"`
	CreatedAt       time.Time  `json:"created_at"`
}

func (s *Server) createScreen(w http.ResponseWriter, r *http.Request) {
	storeID, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	var req struct {
		Name string `json:"name"`
	}
	if !decodeJSON(w, r, &req) {
		return
	}
	if strings.TrimSpace(req.Name) == "" {
		writeErr(w, http.StatusBadRequest, "validation", "name is required")
		return
	}
	token := "sb_scr_" + uuid.NewString()
	sc, err := s.q.CreateScreen(r.Context(), db.CreateScreenParams{
		StoreID: storeID, Name: req.Name, TokenHash: hashToken(token),
	})
	if isFKViolation(err) {
		writeErr(w, http.StatusNotFound, "not_found", "store not found")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"id": sc.ID, "store_id": sc.StoreID, "name": sc.Name,
		"token": token, // returned exactly once
		"created_at": sc.CreatedAt.Time,
	})
}

func (s *Server) listScreens(w http.ResponseWriter, r *http.Request) {
	storeID, ok := pathUUID(w, r, "storeID")
	if !ok {
		return
	}
	rows, err := s.q.ListScreens(r.Context(), storeID)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	now := s.now()
	out := make([]screenResp, len(rows))
	for i, sc := range rows {
		out[i] = screenResp{
			ID: sc.ID, StoreID: sc.StoreID, Name: sc.Name,
			Online:    sc.LastHeartbeatAt.Valid && now.Sub(sc.LastHeartbeatAt.Time) < s.offlineAfter,
			CreatedAt: sc.CreatedAt.Time,
		}
		if sc.LastHeartbeatAt.Valid {
			t := sc.LastHeartbeatAt.Time
			out[i].LastHeartbeatAt = &t
		}
	}
	writeJSON(w, http.StatusOK, out)
}

// --- error classification ---------------------------------------------------------

func pgErr(err error) *pgconn.PgError {
	var pe *pgconn.PgError
	if errors.As(err, &pe) {
		return pe
	}
	return nil
}

func isFKViolation(err error) bool {
	pe := pgErr(err)
	return pe != nil && pe.Code == "23503"
}

func isExclusionViolation(err error) bool {
	pe := pgErr(err)
	return pe != nil && pe.Code == "23P01"
}
