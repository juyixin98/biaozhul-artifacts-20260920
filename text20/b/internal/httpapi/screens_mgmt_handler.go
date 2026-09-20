package httpapi

import (
	"net/http"
	"time"
)

type registerScreenReq struct {
	Name string `json:"name"`
}

type registerScreenResp struct {
	ID      int64  `json:"id"`
	StoreID int64  `json:"store_id"`
	Name    string `json:"name"`
	Token   string `json:"token"` // shown once
}

func (s *Server) registerScreen(w http.ResponseWriter, r *http.Request) {
	storeID, err := urlID(r, "storeID")
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	var req registerScreenReq
	if !decode(w, r, &req) {
		return
	}
	id, token, err := s.svc.RegisterScreen(r.Context(), storeID, req.Name)
	if err != nil {
		mapServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, registerScreenResp{
		ID: id, StoreID: storeID, Name: req.Name, Token: token,
	})
}

func (s *Server) listScreens(w http.ResponseWriter, r *http.Request) {
	storeID, err := urlID(r, "storeID")
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	screens, err := s.svc.ListScreens(r.Context(), storeID, time.Now())
	if err != nil {
		mapServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, screens)
}
