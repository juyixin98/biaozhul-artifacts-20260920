// Package api wires the Chi HTTP surface to the rollout service.
package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"

	"github.com/example/rollout/internal/config"
	"github.com/example/rollout/internal/eval"
	"github.com/example/rollout/internal/models"
	"github.com/example/rollout/internal/store"
)

// Server holds dependencies for all handlers.
type Server struct {
	Cfg    config.Config
	Store  store.Store
	Rel    *eval.Releaser
	Router http.Handler
}

func NewServer(cfg config.Config, st store.Store, rel *eval.Releaser) *Server {
	s := &Server{Cfg: cfg, Store: st, Rel: rel}
	s.Router = s.routes()
	return s
}

func (s *Server) routes() http.Handler {
	r := chi.NewRouter()

	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "time": time.Now().UTC().Format(time.RFC3339Nano)})
	})

	// Authenticated, idempotent mutating surface.
	r.Group(func(r chi.Router) {
		r.Use(hmacAuth(s.Cfg.HMACKeyID, s.Cfg.HMACSecret, s.Store))
		r.Use(idempotency(s.Store))

		r.Post("/api/thresholds", s.createThreshold)
		r.Post("/api/releases", s.createRelease)
		r.Post("/api/releases/{id}/commands/{cmd}", s.runCommand)
	})

	// Read surface.
	r.Get("/api/thresholds", s.listThresholds)
	r.Get("/api/thresholds/latest", s.latestThreshold)
	r.Get("/api/releases", s.listReleases)
	r.Get("/api/releases/{id}", s.getRelease)
	r.Get("/api/releases/{id}/evaluate", s.evaluate)
	r.Get("/api/releases/{id}/events", s.getEvents)
	r.Get("/api/releases/{id}/observations", s.getObservations)

	return r
}

// ---------- thresholds ----------

type thresholdInput struct {
	ErrorRateUpper *float64 `json:"error_rate_upper"`
	LatencyMeanMS  *float64 `json:"latency_mean_ms"`
	LatencyP95MS   *float64 `json:"latency_p95_ms"`
	Description    string   `json:"description"`
}

func (s *Server) createThreshold(w http.ResponseWriter, r *http.Request) {
	body, err := requestBody(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	var in thresholdInput
	if err := json.Unmarshal(body, &in); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	cur, _ := s.Store.GetLatestThreshold(r.Context())
	spec := cur.Spec
	if in.ErrorRateUpper != nil {
		if *in.ErrorRateUpper <= 0 || *in.ErrorRateUpper >= 1 {
			writeError(w, http.StatusBadRequest, "error_rate_upper must be in (0,1)")
			return
		}
		spec.ErrorRateUpper = *in.ErrorRateUpper
	}
	if in.LatencyMeanMS != nil {
		if *in.LatencyMeanMS <= 0 {
			writeError(w, http.StatusBadRequest, "latency_mean_ms must be positive")
			return
		}
		spec.LatencyMeanMS = *in.LatencyMeanMS
	}
	if in.LatencyP95MS != nil {
		spec.LatencyP95MS = *in.LatencyP95MS
	}
	tv, err := s.Store.CreateThreshold(r.Context(), spec, in.Description)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, tv)
}

func (s *Server) listThresholds(w http.ResponseWriter, r *http.Request) {
	// Read via latest + iterate versions stored.
	latest, err := s.Store.GetLatestThreshold(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := []models.ThresholdVersion{}
	for v := 1; v <= latest.Version; v++ {
		t, err := s.Store.GetThreshold(r.Context(), v)
		if err == nil {
			out = append(out, t)
		}
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) latestThreshold(w http.ResponseWriter, r *http.Request) {
	tv, err := s.Store.GetLatestThreshold(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, tv)
}

// ---------- releases ----------

func (s *Server) createRelease(w http.ResponseWriter, r *http.Request) {
	body, err := requestBody(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	var in eval.CreateReleaseInput
	if err := json.Unmarshal(body, &in); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	rel, err := s.Rel.CreateRelease(r.Context(), in, s.Cfg.ObservationMS, s.Cfg.MinSamples, s.Cfg.StubURL+"/metrics")
	if err != nil {
		s.mapErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, rel)
}

func (s *Server) listReleases(w http.ResponseWriter, r *http.Request) {
	rs, err := s.Store.ListReleases(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, rs)
}

func (s *Server) getRelease(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	rel, err := s.Store.GetRelease(r.Context(), id)
	if err != nil {
		s.mapErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rel)
}

func (s *Server) evaluate(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	rel, obs, err := s.Rel.Probe(r.Context(), id)
	if err != nil {
		s.mapErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"release":                   rel,
		"frozen_threshold_version":  rel.ThresholdVersion,
		"frozen_threshold_snapshot": rel.ThresholdSpec,
		"observation":               obs,
	})
}

func (s *Server) getEvents(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	evs, err := s.Store.ListEvents(r.Context(), id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, evs)
}

func (s *Server) getObservations(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	obs, err := s.Store.ListObservations(r.Context(), id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, obs)
}

// ---------- commands ----------

func (s *Server) runCommand(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	cmd := models.Command(chi.URLParam(r, "cmd"))
	body, err := requestBody(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	var in eval.CommandInput
	if len(body) > 0 {
		if err := json.Unmarshal(body, &in); err != nil {
			writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
	}
	res, err := s.Rel.RunCommand(r.Context(), id, cmd, in.ExpectedGeneration)
	if err != nil {
		s.mapErr(w, err)
		return
	}
	if res.Reject != nil {
		writeJSON(w, http.StatusConflict, map[string]any{
			"applied":   false,
			"release":   res.Release,
			"rejection": res.Reject,
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"applied": true,
		"release": res.Release,
		"verdict": res.Verdict,
	})
}

func (s *Server) mapErr(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, store.ErrNotFound):
		writeError(w, http.StatusNotFound, err.Error())
	case errors.Is(err, store.ErrConflict):
		writeError(w, http.StatusConflict, err.Error())
	case errors.Is(err, eval.ErrBadInput):
		writeError(w, http.StatusBadRequest, err.Error())
	default:
		writeError(w, http.StatusInternalServerError, err.Error())
	}
}
