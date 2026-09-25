// Package server exposes the histogram backend over HTTP.
package server

import (
	"encoding/json"
	"errors"
	"math/rand"
	"net/http"
	"strconv"
	"time"

	"histmerge/internal/histogram"
	"histmerge/internal/store"
	"histmerge/internal/synth"
)

// Server wires the store to HTTP handlers.
type Server struct {
	st  *store.Store
	mux *http.ServeMux
}

func New(st *store.Store) *Server {
	s := &Server{st: st, mux: http.NewServeMux()}
	s.mux.HandleFunc("POST /v1/histograms", s.handleIngest)
	s.mux.HandleFunc("GET /v1/histograms", s.handleList)
	s.mux.HandleFunc("GET /v1/histograms/{name}", s.handleGet)
	s.mux.HandleFunc("POST /v1/merge", s.handleMerge)
	s.mux.HandleFunc("GET /v1/quantile", s.handleQuantile)
	s.mux.HandleFunc("POST /v1/synth", s.handleSynth)
	return s
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) { s.mux.ServeHTTP(w, r) }

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, err error) {
	writeJSON(w, code, map[string]string{"error": err.Error()})
}

// handleIngest stores one cumulative histogram posted as JSON.
func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	var h histogram.Histogram
	if err := json.NewDecoder(r.Body).Decode(&h); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if err := s.st.Put(&h); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusCreated, &h)
}

func (s *Server) handleList(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"names": s.st.List()})
}

func (s *Server) handleGet(w http.ResponseWriter, r *http.Request) {
	h, err := s.st.Get(r.PathValue("name"))
	if err != nil {
		code := http.StatusInternalServerError
		if errors.Is(err, store.ErrNotFound) {
			code = http.StatusNotFound
		}
		writeErr(w, code, err)
		return
	}
	writeJSON(w, http.StatusOK, h)
}

type mergeRequest struct {
	Names []string `json:"names"`
}

// handleMerge merges the named stored histograms, shrinking to common
// coarser boundaries when the boundary sets differ.
func (s *Server) handleMerge(w http.ResponseWriter, r *http.Request) {
	var req mergeRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if len(req.Names) == 0 {
		writeErr(w, http.StatusBadRequest, errors.New("merge: names must not be empty"))
		return
	}
	hs := make([]*histogram.Histogram, 0, len(req.Names))
	for _, n := range req.Names {
		h, err := s.st.Get(n)
		if err != nil {
			writeErr(w, http.StatusNotFound, err)
			return
		}
		hs = append(hs, h)
	}
	m, err := histogram.MergeAll(hs)
	if err != nil {
		writeErr(w, http.StatusUnprocessableEntity, err)
		return
	}
	writeJSON(w, http.StatusOK, m)
}

// handleQuantile answers /v1/quantile?name=X&q=0.95 with the bucket
// interval and the interpolated estimate.
func (s *Server) handleQuantile(w http.ResponseWriter, r *http.Request) {
	name := r.URL.Query().Get("name")
	q, err := strconv.ParseFloat(r.URL.Query().Get("q"), 64)
	if err != nil {
		writeErr(w, http.StatusBadRequest, errors.New("quantile: bad or missing q parameter"))
		return
	}
	h, err := s.st.Get(name)
	if err != nil {
		writeErr(w, http.StatusNotFound, err)
		return
	}
	est, err := h.Quantile(q)
	if err != nil {
		writeErr(w, http.StatusUnprocessableEntity, err)
		return
	}
	writeJSON(w, http.StatusOK, est)
}

// handleSynth seeds the store with synthetic histograms: three services,
// one of them on coarser boundaries, to exercise the merge paths.
func (s *Server) handleSynth(w http.ResponseWriter, r *http.Request) {
	seed := time.Now().UnixNano()
	if v := r.URL.Query().Get("seed"); v != "" {
		if parsed, err := strconv.ParseInt(v, 10, 64); err == nil {
			seed = parsed
		}
	}
	rng := rand.New(rand.NewSource(seed))
	gen := []*histogram.Histogram{
		synth.Generate(rng, "svc-a.http_duration", synth.LatencyBounds, 0.08, 5000),
		synth.Generate(rng, "svc-b.http_duration", synth.LatencyBounds, 0.3, 3000),
		synth.Generate(rng, "svc-c.http_duration", synth.CoarserBounds, 0.15, 4000),
	}
	for _, h := range gen {
		if err := s.st.Put(h); err != nil {
			writeErr(w, http.StatusInternalServerError, err)
			return
		}
	}
	writeJSON(w, http.StatusCreated, map[string]any{"seed": seed, "names": s.st.List()})
}
