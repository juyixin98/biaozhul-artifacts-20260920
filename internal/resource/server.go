package resource

import (
	"errors"
	"net/http"

	"fencingdemo/internal/httpx"
)

// Server exposes the resource Store over HTTP.
type Server struct {
	store *Store
}

// NewServer builds the resource HTTP handler.
func NewServer(store *Store) *Server {
	return &Server{store: store}
}

// Handler registers all routes.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/resource/write", s.handleWrite)
	mux.HandleFunc("/resource/read", s.handleRead)
	return mux
}

type writeRequest struct {
	FencingToken int64  `json:"fencing_token"`
	LeaseID      string `json:"lease_id,omitempty"`
	Value        string `json:"value"`
}

type writeResponse struct {
	Accepted      bool  `json:"accepted"`
	Version       int64 `json:"version"`
	HighWaterMark int64 `json:"high_water_mark"`
	ObservedToken int64 `json:"observed_token"`
}

func (s *Server) handleWrite(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		httpx.Errorf(w, http.StatusMethodNotAllowed, "use POST")
		return
	}
	var req writeRequest
	if err := httpx.DecodeJSON(r, &req); err != nil {
		httpx.Errorf(w, http.StatusBadRequest, "invalid JSON body: %v", err)
		return
	}
	version, hw, err := s.store.Write(req.FencingToken, req.Value)
	if err != nil {
		if errors.Is(err, ErrStaleWrite) {
			httpx.JSON(w, http.StatusConflict, writeResponse{
				Accepted:      false,
				HighWaterMark: hw,
				ObservedToken: req.FencingToken,
			})
			return
		}
		if errors.Is(err, ErrBadToken) {
			httpx.Errorf(w, http.StatusBadRequest, err.Error())
			return
		}
		httpx.Errorf(w, http.StatusInternalServerError, err.Error())
		return
	}
	httpx.JSON(w, http.StatusOK, writeResponse{
		Accepted:      true,
		Version:       version,
		HighWaterMark: hw,
		ObservedToken: req.FencingToken,
	})
}

type readResponse struct {
	Value         string `json:"value"`
	Version       int64  `json:"version"`
	HighWaterMark int64  `json:"high_water_mark"`
}

func (s *Server) handleRead(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		httpx.Errorf(w, http.StatusMethodNotAllowed, "use GET")
		return
	}
	value, version, hw := s.store.Read()
	httpx.JSON(w, http.StatusOK, readResponse{
		Value:         value,
		Version:       version,
		HighWaterMark: hw,
	})
}
