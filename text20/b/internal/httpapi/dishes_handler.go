package httpapi

import (
	"net/http"
)

type createDishReq struct {
	Name      string `json:"name"`
	BasePrice int64  `json:"base_price"`
}

func (s *Server) createDish(w http.ResponseWriter, r *http.Request) {
	storeID, err := urlID(r, "storeID")
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	var req createDishReq
	if !decode(w, r, &req) {
		return
	}
	d, err := s.svc.CreateDish(r.Context(), storeID, req.Name, req.BasePrice)
	if err != nil {
		mapServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, d)
}

func (s *Server) listDishes(w http.ResponseWriter, r *http.Request) {
	storeID, err := urlID(r, "storeID")
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	ds, err := s.svc.ListDishes(r.Context(), storeID)
	if err != nil {
		mapServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, ds)
}

type priceReq struct {
	Price int64 `json:"price"`
}

func (s *Server) setDishPrice(w http.ResponseWriter, r *http.Request) {
	storeID, dishID, ok := storeDishIDs(w, r)
	if !ok {
		return
	}
	var req priceReq
	if !decode(w, r, &req) {
		return
	}
	if err := s.svc.SetDishPrice(r.Context(), storeID, dishID, req.Price); err != nil {
		mapServiceError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

type activeReq struct {
	IsActive bool `json:"is_active"`
}

func (s *Server) setDishActive(w http.ResponseWriter, r *http.Request) {
	storeID, dishID, ok := storeDishIDs(w, r)
	if !ok {
		return
	}
	var req activeReq
	if !decode(w, r, &req) {
		return
	}
	if err := s.svc.SetDishActive(r.Context(), storeID, dishID, req.IsActive); err != nil {
		mapServiceError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

type thresholdReq struct {
	Threshold int64 `json:"threshold"`
}

func (s *Server) setThreshold(w http.ResponseWriter, r *http.Request) {
	storeID, dishID, ok := storeDishIDs(w, r)
	if !ok {
		return
	}
	var req thresholdReq
	if !decode(w, r, &req) {
		return
	}
	if err := s.svc.SetThreshold(r.Context(), storeID, dishID, req.Threshold); err != nil {
		mapServiceError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func storeDishIDs(w http.ResponseWriter, r *http.Request) (storeID, dishID int64, ok bool) {
	storeID, err := urlID(r, "storeID")
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return 0, 0, false
	}
	dishID, err = urlID(r, "dishID")
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return 0, 0, false
	}
	return storeID, dishID, true
}
