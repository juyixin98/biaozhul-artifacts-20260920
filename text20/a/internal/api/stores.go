package api

import (
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"

	"signalboard/internal/db"
	"signalboard/internal/httpx"
)

type storeReq struct {
	Name     string `json:"name"`
	Timezone string `json:"timezone"`
}

func (s *Server) createStore(w http.ResponseWriter, r *http.Request) {
	var req storeReq
	if !httpx.DecodeJSON(w, r, &req) {
		return
	}
	req.Name = strings.TrimSpace(req.Name)
	if req.Name == "" {
		httpx.ErrorJSON(w, http.StatusBadRequest, "invalid_store", "name is required")
		return
	}
	if _, err := time.LoadLocation(req.Timezone); err != nil {
		httpx.ErrorJSON(w, http.StatusBadRequest, "invalid_timezone",
			"timezone must be an IANA zone such as Asia/Shanghai or America/New_York")
		return
	}
	store, err := s.q.CreateStore(r.Context(), db.CreateStoreParams{
		Name: req.Name, Timezone: req.Timezone,
	})
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	httpx.JSON(w, http.StatusCreated, store)
}

func (s *Server) listStores(w http.ResponseWriter, r *http.Request) {
	stores, err := s.q.ListStores(r.Context())
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	httpx.JSON(w, http.StatusOK, stores)
}

func (s *Server) getStore(w http.ResponseWriter, r *http.Request) {
	storeID, ok := urlID(w, r, "storeID")
	if !ok {
		return
	}
	store, err := s.q.GetStore(r.Context(), storeID)
	if errors.Is(err, pgx.ErrNoRows) {
		httpx.ErrorJSON(w, http.StatusNotFound, "store_not_found", "store does not exist")
		return
	}
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	httpx.JSON(w, http.StatusOK, store)
}

// urlID parses a positive integer path parameter.
func urlID(w http.ResponseWriter, r *http.Request, key string) (int64, bool) {
	raw := chi.URLParam(r, key)
	id, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || id <= 0 {
		httpx.ErrorJSON(w, http.StatusBadRequest, "invalid_id", key+" must be a positive integer")
		return 0, false
	}
	return id, true
}

// loadStore fetches a store and parses its timezone, sending an error response
// on failure.
func (s *Server) loadStore(w http.ResponseWriter, r *http.Request, storeID int64) (db.Store, *time.Location, bool) {
	store, err := s.q.GetStore(r.Context(), storeID)
	if errors.Is(err, pgx.ErrNoRows) {
		httpx.ErrorJSON(w, http.StatusNotFound, "store_not_found", "store does not exist")
		return db.Store{}, nil, false
	}
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return db.Store{}, nil, false
	}
	loc, err := time.LoadLocation(store.Timezone)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "invalid_timezone",
			"stored timezone is not loadable: "+store.Timezone)
		return db.Store{}, nil, false
	}
	return store, loc, true
}
