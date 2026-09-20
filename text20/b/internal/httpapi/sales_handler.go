package httpapi

import (
	"net/http"
	"time"

	"signalboard/internal/service"
)

type salesEventReq struct {
	EventID    string `json:"event_id"`
	DishID     int64  `json:"dish_id"`
	Quantity   int32  `json:"quantity"`
	OccurredAt string `json:"occurred_at"` // RFC3339; attributed to store-local day
}

func (s *Server) ingestSales(w http.ResponseWriter, r *http.Request) {
	storeID, err := urlID(r, "storeID")
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	var req salesEventReq
	if !decode(w, r, &req) {
		return
	}
	occ, err := time.Parse(time.RFC3339, req.OccurredAt)
	if err != nil {
		writeError(w, http.StatusBadRequest, "occurred_at invalid RFC3339: "+err.Error())
		return
	}
	res, err := s.svc.IngestSalesEvent(r.Context(), storeID, service.SalesEventInput{
		EventID: req.EventID, DishID: req.DishID,
		Quantity: req.Quantity, OccurredAt: occ,
	})
	if err != nil {
		mapServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusAccepted, res)
}

func (s *Server) todaySales(w http.ResponseWriter, r *http.Request) {
	storeID, err := urlID(r, "storeID")
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	day, err := s.svc.SalesToday(r.Context(), storeID, time.Now())
	if err != nil {
		mapServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, day)
}
