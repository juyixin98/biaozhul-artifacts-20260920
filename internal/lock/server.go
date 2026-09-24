package lock

import (
	"errors"
	"net/http"
	"time"

	"fencingdemo/internal/httpx"
)

// Server exposes the lock Store over HTTP using only net/http.
type Server struct {
	store *Store
}

// NewServer builds the lock HTTP handler around the given store.
func NewServer(store *Store) *Server {
	return &Server{store: store}
}

// Handler registers all routes.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/lock/acquire", s.handleAcquire)
	mux.HandleFunc("/lock/renew", s.handleRenew)
	mux.HandleFunc("/lock/release", s.handleRelease)
	mux.HandleFunc("/lock/status", s.handleStatus)
	return mux
}

type leaseResponse struct {
	LeaseID   string    `json:"lease_id"`
	Token     int64     `json:"fencing_token"`
	IssuedAt  time.Time `json:"issued_at"`
	ExpiresAt time.Time `json:"expires_at"`
	TTLMillis int64     `json:"ttl_ms"`
}

func toResponse(l Lease, ttl time.Duration) leaseResponse {
	return leaseResponse{
		LeaseID:   l.ID,
		Token:     l.Token,
		IssuedAt:  l.IssuedAt.UTC(),
		ExpiresAt: l.Expires.UTC(),
		TTLMillis: ttl.Milliseconds(),
	}
}

type acquireRequest struct {
	LeaseID string `json:"lease_id"`
}

func (s *Server) handleAcquire(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		httpx.Errorf(w, http.StatusMethodNotAllowed, "use POST")
		return
	}
	var req acquireRequest
	if err := httpx.DecodeJSON(r, &req); err != nil {
		httpx.Errorf(w, http.StatusBadRequest, "invalid JSON body: %v", err)
		return
	}
	l, err := s.store.Acquire(req.LeaseID)
	if err != nil {
		writeLockError(w, err)
		return
	}
	_, ttl, _ := s.store.Status()
	httpx.JSON(w, http.StatusOK, toResponse(l, ttl))
}

type renewRequest struct {
	LeaseID string `json:"lease_id"`
}

func (s *Server) handleRenew(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		httpx.Errorf(w, http.StatusMethodNotAllowed, "use POST")
		return
	}
	var req renewRequest
	if err := httpx.DecodeJSON(r, &req); err != nil {
		httpx.Errorf(w, http.StatusBadRequest, "invalid JSON body: %v", err)
		return
	}
	l, err := s.store.Renew(req.LeaseID)
	if err != nil {
		writeLockError(w, err)
		return
	}
	_, ttl, _ := s.store.Status()
	httpx.JSON(w, http.StatusOK, toResponse(l, ttl))
}

type releaseRequest struct {
	LeaseID string `json:"lease_id"`
}

func (s *Server) handleRelease(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		httpx.Errorf(w, http.StatusMethodNotAllowed, "use POST")
		return
	}
	var req releaseRequest
	if err := httpx.DecodeJSON(r, &req); err != nil {
		httpx.Errorf(w, http.StatusBadRequest, "invalid JSON body: %v", err)
		return
	}
	if err := s.store.Release(req.LeaseID); err != nil {
		writeLockError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]string{"status": "released"})
}

type statusResponse struct {
	Held      bool           `json:"held"`
	Lease     *leaseResponse `json:"lease,omitempty"`
	NextToken int64          `json:"next_token"`
	TTLMillis int64          `json:"ttl_ms"`
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		httpx.Errorf(w, http.StatusMethodNotAllowed, "use GET")
		return
	}
	l, ttl, held := s.store.Status()
	resp := statusResponse{
		Held:      held,
		NextToken: s.store.NextToken(),
		TTLMillis: ttl.Milliseconds(),
	}
	if held {
		lr := toResponse(*l, ttl)
		resp.Lease = &lr
	}
	httpx.JSON(w, http.StatusOK, resp)
}

func writeLockError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, ErrHeld):
		httpx.Errorf(w, http.StatusConflict, err.Error())
	case errors.Is(err, ErrNotHolder):
		httpx.Errorf(w, http.StatusForbidden, err.Error())
	case errors.Is(err, ErrExpired):
		httpx.Errorf(w, http.StatusGone, err.Error())
	default:
		httpx.Errorf(w, http.StatusInternalServerError, err.Error())
	}
}
