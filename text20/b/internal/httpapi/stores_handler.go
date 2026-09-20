package httpapi

import (
	"net/http"
)

type createStoreReq struct {
	Name     string `json:"name"`
	Timezone string `json:"timezone"`
}

func (s *Server) createStore(w http.ResponseWriter, r *http.Request) {
	var req createStoreReq
	if !decode(w, r, &req) {
		return
	}
	st, err := s.svc.CreateStore(r.Context(), req.Name, req.Timezone)
	if err != nil {
		mapServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, st)
}

func (s *Server) listStores(w http.ResponseWriter, r *http.Request) {
	stores, err := s.svc.ListStores(r.Context())
	if err != nil {
		mapServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, stores)
}

func (s *Server) getStore(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "storeID")
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	st, err := s.svc.GetStore(r.Context(), id)
	if err != nil {
		mapServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, st)
}
