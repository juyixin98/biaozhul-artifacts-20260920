package httpapi

import (
	"net/http"

	"signalboard/internal/service"
)

type draftItemDTO struct {
	DishID int64  `json:"dish_id"`
	Price  *int64 `json:"price,omitempty"`
}

type draftReq struct {
	Items []draftItemDTO `json:"items"`
}

func toInputs(dtos []draftItemDTO) []service.DraftItemInput {
	out := make([]service.DraftItemInput, 0, len(dtos))
	for _, d := range dtos {
		out = append(out, service.DraftItemInput{DishID: d.DishID, Price: d.Price})
	}
	return out
}

func (s *Server) getDraft(w http.ResponseWriter, r *http.Request) {
	storeID, err := urlID(r, "storeID")
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	d, err := s.svc.GetDraft(r.Context(), storeID)
	if err != nil {
		mapServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, d)
}

func (s *Server) replaceDraft(w http.ResponseWriter, r *http.Request) {
	s.putDraft(w, r)
}

// batchImport behaves identically to replacing the draft but signals the
// all-or-nothing contract in the error response.
func (s *Server) batchImport(w http.ResponseWriter, r *http.Request) {
	s.putDraft(w, r)
}

func (s *Server) putDraft(w http.ResponseWriter, r *http.Request) {
	storeID, err := urlID(r, "storeID")
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	var req draftReq
	if !decode(w, r, &req) {
		return
	}
	d, err := s.svc.ReplaceDraft(r.Context(), storeID, toInputs(req.Items))
	if err != nil {
		mapServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, d)
}
