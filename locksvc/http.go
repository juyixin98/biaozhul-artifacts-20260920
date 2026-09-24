package locksvc

import (
	"errors"
	"net/http"
	"time"

	"fencingdemo/internal/apiutil"
)

// HTTPHandler 把锁服务暴露为 HTTP 接口：
//
//	POST /v1/locks/{name}/acquire  {"holder":"a","ttl_ms":5000}
//	POST /v1/locks/{name}/release  {"holder":"a"}
//	GET  /v1/locks/{name}
//	GET  /v1/counter
func (s *Service) HTTPHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/locks/{name}/acquire", s.handleAcquire)
	mux.HandleFunc("POST /v1/locks/{name}/release", s.handleRelease)
	mux.HandleFunc("GET /v1/locks/{name}", s.handleGet)
	mux.HandleFunc("GET /v1/counter", func(w http.ResponseWriter, _ *http.Request) {
		apiutil.WriteJSON(w, http.StatusOK, map[string]uint64{"counter": s.Counter()})
	})
	return mux
}

type acquireRequest struct {
	Holder string `json:"holder"`
	TTLms  int64  `json:"ttl_ms"`
}

type releaseRequest struct {
	Holder string `json:"holder"`
}

type leaseResponse struct {
	Name      string    `json:"name"`
	Holder    string    `json:"holder"`
	Token     uint64    `json:"token"`
	ExpiresAt time.Time `json:"expires_at"`
}

func (s *Service) handleAcquire(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	var req acquireRequest
	if err := apiutil.DecodeJSON(r, &req); err != nil {
		apiutil.WriteError(w, http.StatusBadRequest, err.Error())
		return
	}
	if req.Holder == "" {
		apiutil.WriteError(w, http.StatusBadRequest, "holder must be non-empty")
		return
	}
	l, err := s.Acquire(name, req.Holder, time.Duration(req.TTLms)*time.Millisecond)
	switch {
	case errors.Is(err, ErrInvalidTTL):
		apiutil.WriteError(w, http.StatusBadRequest, err.Error())
	case errors.Is(err, ErrConflict):
		apiutil.WriteJSON(w, http.StatusConflict, map[string]any{
			"error":      err.Error(),
			"holder":     l.Holder,
			"expires_at": l.ExpiresAt,
		})
	case err != nil:
		apiutil.WriteError(w, http.StatusInternalServerError, err.Error())
	default:
		apiutil.WriteJSON(w, http.StatusOK, leaseResponse{
			Name: name, Holder: l.Holder, Token: l.Token, ExpiresAt: l.ExpiresAt,
		})
	}
}

func (s *Service) handleRelease(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	var req releaseRequest
	if err := apiutil.DecodeJSON(r, &req); err != nil {
		apiutil.WriteError(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := s.Release(name, req.Holder); err != nil {
		apiutil.WriteError(w, http.StatusConflict, err.Error())
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, map[string]string{"status": "released", "name": name})
}

func (s *Service) handleGet(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	l, ok := s.Lease(name)
	if !ok {
		apiutil.WriteError(w, http.StatusNotFound, "no active lease")
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, leaseResponse{
		Name: name, Holder: l.Holder, Token: l.Token, ExpiresAt: l.ExpiresAt,
	})
}
