package server

import (
	"net/http"
	"time"

	"signalboard/internal/db"
)

type storeResponse struct {
	ID             string    `json:"id"`
	Name           string    `json:"name"`
	Timezone       string    `json:"timezone"`
	CurrentVersion int32     `json:"current_version"`
	CreatedAt      time.Time `json:"created_at"`
}

func toStoreResponse(s db.Store) storeResponse {
	return storeResponse{
		ID:             uuidString(s.ID),
		Name:           s.Name,
		Timezone:       s.Timezone,
		CurrentVersion: s.CurrentVersion,
		CreatedAt:      s.CreatedAt,
	}
}

type createStoreRequest struct {
	Name     string `json:"name"`
	Timezone string `json:"timezone"`
}

func (s *Server) createStore(w http.ResponseWriter, r *http.Request) {
	var req createStoreRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.Name == "" {
		writeError(w, http.StatusBadRequest, "name is required")
		return
	}
	if _, err := time.LoadLocation(req.Timezone); err != nil {
		writeError(w, http.StatusBadRequest, "timezone must be an IANA name such as Asia/Shanghai")
		return
	}
	ctx := r.Context()
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer func() { _ = tx.Rollback(ctx) }()
	q := s.q.WithTx(tx)
	store, err := q.CreateStore(ctx, db.CreateStoreParams{Name: req.Name, Timezone: req.Timezone})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if err := q.EnsureDraft(ctx, store.ID); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if err := tx.Commit(ctx); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, toStoreResponse(store))
}

func (s *Server) getStore(w http.ResponseWriter, r *http.Request) {
	storeID, ok := uuidParam(w, r, "storeID")
	if !ok {
		return
	}
	store, err := s.q.GetStore(r.Context(), storeID)
	if isNoRows(err) {
		writeError(w, http.StatusNotFound, "store not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, toStoreResponse(store))
}

func (s *Server) listVersions(w http.ResponseWriter, r *http.Request) {
	storeID, ok := uuidParam(w, r, "storeID")
	if !ok {
		return
	}
	versions, err := s.q.ListVersions(r.Context(), storeID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := make([]map[string]any, 0, len(versions))
	for _, v := range versions {
		out = append(out, map[string]any{
			"id":           uuidString(v.ID),
			"version":      v.Version,
			"published_at": v.PublishedAt,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"versions": out})
}
