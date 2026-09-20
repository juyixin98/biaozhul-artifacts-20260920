package httpapi

import (
	"fmt"
	"net/http"
	"strconv"
	"time"

	"signalboard/internal/service"
)

type tempPriceDTO struct {
	DishID   int64  `json:"dish_id"`
	Price    int64  `json:"price"`
	StartsAt string `json:"starts_at"` // RFC3339 with offset
	EndsAt   string `json:"ends_at"`
}

type publishReq struct {
	ExpectedVersion int64          `json:"expected_version"`
	PublishedBy     string         `json:"published_by"`
	TempPrices      []tempPriceDTO `json:"temp_prices"`
}

func parseTempPrices(dtos []tempPriceDTO) ([]service.TempPriceInput, error) {
	out := make([]service.TempPriceInput, 0, len(dtos))
	for i, d := range dtos {
		st, err := time.Parse(time.RFC3339, d.StartsAt)
		if err != nil {
			return nil, fmt.Errorf("temp_prices[%d].starts_at invalid RFC3339: %v", i, err)
		}
		en, err := time.Parse(time.RFC3339, d.EndsAt)
		if err != nil {
			return nil, fmt.Errorf("temp_prices[%d].ends_at invalid RFC3339: %v", i, err)
		}
		out = append(out, service.TempPriceInput{
			DishID: d.DishID, Price: d.Price, StartsAt: st, EndsAt: en,
		})
	}
	return out, nil
}

func (s *Server) publish(w http.ResponseWriter, r *http.Request) {
	storeID, err := urlID(r, "storeID")
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	var req publishReq
	if !decode(w, r, &req) {
		return
	}
	temps, err := parseTempPrices(req.TempPrices)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	v, err := s.svc.Publish(r.Context(), storeID, req.ExpectedVersion,
		req.PublishedBy, temps)
	if err != nil {
		mapServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, v)
}

func (s *Server) getPublishedMenu(w http.ResponseWriter, r *http.Request) {
	storeID, err := urlID(r, "storeID")
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	version := int64(0)
	if v := r.URL.Query().Get("version"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil || n <= 0 {
			writeError(w, http.StatusBadRequest, "invalid version")
			return
		}
		version = n
	}
	m, err := s.svc.GetPublishedMenu(r.Context(), storeID, version)
	if err != nil {
		mapServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, m)
}

type tempPricesReq struct {
	TempPrices []tempPriceDTO `json:"temp_prices"`
}

func (s *Server) scheduleTempPrices(w http.ResponseWriter, r *http.Request) {
	storeID, err := urlID(r, "storeID")
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	var req tempPricesReq
	if !decode(w, r, &req) {
		return
	}
	temps, err := parseTempPrices(req.TempPrices)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	m, err := s.svc.ScheduleTempPrices(r.Context(), storeID, temps)
	if err != nil {
		mapServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, m)
}
