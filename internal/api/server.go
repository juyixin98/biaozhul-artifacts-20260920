// Package api exposes the scaling decision engine over HTTP.
package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"scaler-stable-window/internal/clock"
	"scaler-stable-window/internal/hpa"
	"scaler-stable-window/internal/store"
)

type Server struct {
	svc *hpa.Service
	st  *store.Store
	clk *clock.Clock
	mux *http.ServeMux
}

func NewServer(svc *hpa.Service, st *store.Store, clk *clock.Clock) *Server {
	s := &Server{svc: svc, st: st, clk: clk, mux: http.NewServeMux()}
	s.routes()
	return s
}

func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("GET /healthz", s.health)
	s.mux.HandleFunc("GET /clock", s.getClock)
	s.mux.HandleFunc("POST /clock/set", s.setClock)
	s.mux.HandleFunc("POST /clock/advance", s.advanceClock)

	s.mux.HandleFunc("PUT /scalers/{id}/config", s.putConfig)
	s.mux.HandleFunc("GET /scalers/{id}/config", s.getConfig)
	s.mux.HandleFunc("GET /scalers/{id}/configs", s.listConfigs)
	s.mux.HandleFunc("POST /scalers/{id}/decide", s.decide)
	s.mux.HandleFunc("GET /scalers/{id}/decisions", s.listDecisions)
	s.mux.HandleFunc("GET /scalers/{id}/recommendations", s.listRecommendations)
}

type errBody struct {
	Error  string `json:"error"`
	Reason string `json:"reason"`
}

func writeErr(w http.ResponseWriter, status int, reason string, err error) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(errBody{Error: err.Error(), Reason: reason})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{"ok": true, "time": s.clk.Now()})
}

// ---------------- clock ----------------

func (s *Server) getClock(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{
		"manual": s.clk.IsManual(),
		"now":    s.clk.Now().Format(time.RFC3339Nano),
	})
}

type setClockReq struct {
	Now time.Time `json:"now"`
}

func (s *Server) setClock(w http.ResponseWriter, r *http.Request) {
	var req setClockReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, 400, "bad_json", err)
		return
	}
	if req.Now.IsZero() {
		writeErr(w, 400, "missing_now", errors.New("now is required (RFC3339)"))
		return
	}
	if err := s.clk.Set(req.Now); err != nil {
		writeErr(w, 409, "clock_not_manual", err)
		return
	}
	writeJSON(w, 200, map[string]any{"manual": true, "now": s.clk.Now().Format(time.RFC3339Nano)})
}

type advanceClockReq struct {
	Seconds int64 `json:"seconds"`
}

func (s *Server) advanceClock(w http.ResponseWriter, r *http.Request) {
	var req advanceClockReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, 400, "bad_json", err)
		return
	}
	if req.Seconds < 0 {
		writeErr(w, 400, "negative_advance", errors.New("seconds must be >= 0"))
		return
	}
	newTime, err := s.clk.Advance(time.Duration(req.Seconds) * time.Second)
	if err != nil {
		writeErr(w, 409, "clock_not_manual", err)
		return
	}
	writeJSON(w, 200, map[string]any{"manual": true, "now": newTime.Format(time.RFC3339Nano)})
}

// ---------------- config ----------------

type putConfigReq struct {
	TargetUtilization                   int     `json:"targetUtilization"`
	TolerancePct                        float64 `json:"tolerancePct"`
	MinReplicas                         int     `json:"minReplicas"`
	MaxReplicas                         int     `json:"maxReplicas"`
	ScaleDownStabilizationWindowSeconds int     `json:"scaleDownStabilizationWindowSeconds"`
	ScaleUpStabilizationWindowSeconds   int     `json:"scaleUpStabilizationWindowSeconds"`
	MetricFreshnessSeconds              int     `json:"metricFreshnessSeconds"`
}

func (s *Server) putConfig(w http.ResponseWriter, r *http.Request) {
	scalerID := r.PathValue("id")
	var req putConfigReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, 400, "bad_json", err)
		return
	}
	// targetUtilization is mandatory: an explicit 0 must never be silently
	// turned into a default (it would later divide by zero), and it must
	// never be interpreted as "no metrics".
	if req.TargetUtilization == 0 {
		writeErr(w, 422, hpa.ReasonZeroTargetValue,
			errors.New("targetUtilization is required and must be in (0,100]"))
		return
	}

	cfg := hpa.WithDefaults(hpa.Config{
		ScalerID:                            scalerID,
		TargetUtilization:                   req.TargetUtilization,
		TolerancePct:                        req.TolerancePct,
		MinReplicas:                         req.MinReplicas,
		MaxReplicas:                         req.MaxReplicas,
		ScaleDownStabilizationWindowSeconds: req.ScaleDownStabilizationWindowSeconds,
		ScaleUpStabilizationWindowSeconds:   req.ScaleUpStabilizationWindowSeconds,
		MetricFreshnessSeconds:              req.MetricFreshnessSeconds,
	})
	if err := hpa.Validate(&cfg); err != nil {
		writeErr(w, 422, "invalid_config", err)
		return
	}
	cfg.Fingerprint = hpa.FingerprintConfig(cfg)

	// Versioning: monotonically increasing per scaler. A content-identical
	// PUT still advances the version and thereby opens a fresh window; the
	// fingerprint lets callers see the policy itself did not change.
	var nextVersion int64 = 1
	if prev, ok, err := s.st.LatestConfig(scalerID); err != nil {
		writeErr(w, 500, "store_error", err)
		return
	} else if ok {
		nextVersion = prev.Version + 1
	}
	cfg.Version = nextVersion
	cfg.CreatedAt = s.clk.Now()

	cfgJSON := map[string]any{
		"targetUtilization":                   cfg.TargetUtilization,
		"tolerancePct":                        cfg.TolerancePct,
		"minReplicas":                         cfg.MinReplicas,
		"maxReplicas":                         cfg.MaxReplicas,
		"scaleDownStabilizationWindowSeconds": cfg.ScaleDownStabilizationWindowSeconds,
		"scaleUpStabilizationWindowSeconds":   cfg.ScaleUpStabilizationWindowSeconds,
		"metricFreshnessSeconds":              cfg.MetricFreshnessSeconds,
	}
	if err := s.st.SaveConfig(scalerID, cfg.Fingerprint, cfg.Version, cfgJSON, cfg.CreatedAt); err != nil {
		writeErr(w, 500, "store_error", err)
		return
	}
	writeJSON(w, 201, configResp{
		ScalerID:                            cfg.ScalerID,
		Version:                             cfg.Version,
		Fingerprint:                         cfg.Fingerprint,
		CreatedAt:                           cfg.CreatedAt,
		TargetUtilization:                   cfg.TargetUtilization,
		TolerancePct:                        cfg.TolerancePct,
		MinReplicas:                         cfg.MinReplicas,
		MaxReplicas:                         cfg.MaxReplicas,
		ScaleDownStabilizationWindowSeconds: cfg.ScaleDownStabilizationWindowSeconds,
		ScaleUpStabilizationWindowSeconds:   cfg.ScaleUpStabilizationWindowSeconds,
		MetricFreshnessSeconds:              cfg.MetricFreshnessSeconds,
	})
}

type configResp struct {
	ScalerID                            string    `json:"scalerId"`
	Version                             int64     `json:"version"`
	Fingerprint                         string    `json:"fingerprint"`
	CreatedAt                           time.Time `json:"createdAt"`
	TargetUtilization                   int       `json:"targetUtilization"`
	TolerancePct                        float64   `json:"tolerancePct"`
	MinReplicas                         int       `json:"minReplicas"`
	MaxReplicas                         int       `json:"maxReplicas"`
	ScaleDownStabilizationWindowSeconds int       `json:"scaleDownStabilizationWindowSeconds"`
	ScaleUpStabilizationWindowSeconds   int       `json:"scaleUpStabilizationWindowSeconds"`
	MetricFreshnessSeconds              int       `json:"metricFreshnessSeconds"`
}

func loadConfig(st *store.Store, scalerID string) (hpa.Config, bool, error) {
	snap, ok, err := st.LatestConfig(scalerID)
	if err != nil || !ok {
		return hpa.Config{}, ok, err
	}
	cfg := hpa.WithDefaults(hpa.Config{
		ScalerID:                            scalerID,
		Version:                             snap.Version,
		Fingerprint:                         snap.Fingerprint,
		TargetUtilization:                   asInt(snap.Body["targetUtilization"]),
		TolerancePct:                        asFloat(snap.Body["tolerancePct"]),
		MinReplicas:                         asInt(snap.Body["minReplicas"]),
		MaxReplicas:                         asInt(snap.Body["maxReplicas"]),
		ScaleDownStabilizationWindowSeconds: asInt(snap.Body["scaleDownStabilizationWindowSeconds"]),
		ScaleUpStabilizationWindowSeconds:   asInt(snap.Body["scaleUpStabilizationWindowSeconds"]),
		MetricFreshnessSeconds:              asInt(snap.Body["metricFreshnessSeconds"]),
	})
	return cfg, true, nil
}

func (s *Server) getConfig(w http.ResponseWriter, r *http.Request) {
	scalerID := r.PathValue("id")
	cfg, ok, err := loadConfig(s.st, scalerID)
	if err != nil {
		writeErr(w, 500, "store_error", err)
		return
	}
	if !ok {
		writeErr(w, 404, "config_not_found", fmt.Errorf("no config for scaler %q", scalerID))
		return
	}
	writeJSON(w, 200, configRespFromCfg(cfg))
}

func configRespFromCfg(cfg hpa.Config) configResp {
	return configResp{
		ScalerID:                            cfg.ScalerID,
		Version:                             cfg.Version,
		Fingerprint:                         cfg.Fingerprint,
		CreatedAt:                           cfg.CreatedAt,
		TargetUtilization:                   cfg.TargetUtilization,
		TolerancePct:                        cfg.TolerancePct,
		MinReplicas:                         cfg.MinReplicas,
		MaxReplicas:                         cfg.MaxReplicas,
		ScaleDownStabilizationWindowSeconds: cfg.ScaleDownStabilizationWindowSeconds,
		ScaleUpStabilizationWindowSeconds:   cfg.ScaleUpStabilizationWindowSeconds,
		MetricFreshnessSeconds:              cfg.MetricFreshnessSeconds,
	}
}

func (s *Server) listConfigs(w http.ResponseWriter, r *http.Request) {
	scalerID := r.PathValue("id")
	snaps, err := s.st.ListConfigs(scalerID)
	if err != nil {
		writeErr(w, 500, "store_error", err)
		return
	}
	writeJSON(w, 200, map[string]any{"scalerId": scalerID, "versions": snaps})
}

// ---------------- decision ----------------

func (s *Server) decide(w http.ResponseWriter, r *http.Request) {
	scalerID := r.PathValue("id")
	var req hpa.DecisionRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, 400, "bad_json", err)
		return
	}
	req.ScalerID = scalerID

	cfg, ok, err := loadConfig(s.st, scalerID)
	if err != nil {
		writeErr(w, 500, "store_error", err)
		return
	}
	if !ok {
		writeErr(w, 404, "config_not_found", fmt.Errorf("put a config before deciding for %q", scalerID))
		return
	}
	if req.ConfigVersion != 0 && req.ConfigVersion != cfg.Version {
		writeErr(w, 409, hpa.ReasonConfigVersionReset,
			fmt.Errorf("%w: request v%d, active v%d",
				hpa.ErrConfigVersionMismatch, req.ConfigVersion, cfg.Version))
		return
	}
	if cfg.TargetUtilization == 0 {
		writeErr(w, 422, hpa.ReasonZeroTargetValue, hpa.ErrZeroTarget)
		return
	}
	req.ConfigVersion = cfg.Version

	d, err := s.svc.Decide(r.Context(), cfg, req, s.clk.Now())
	if err != nil {
		s.decideErr(w, r, scalerID, req, err)
		return
	}
	writeJSON(w, 200, d)
}

// decideErr maps engine errors to HTTP status codes and implements idempotent
// replay for duplicate event times.
func (s *Server) decideErr(w http.ResponseWriter, r *http.Request, scalerID string, req hpa.DecisionRequest, err error) {
	switch {
	case errors.Is(err, hpa.ErrMetricRegression):
		writeErr(w, 409, "metric_timestamp_regressed", err)
	case errors.Is(err, hpa.ErrFutureMetric):
		writeErr(w, 422, "metric_timestamp_future", err)
	case errors.Is(err, hpa.ErrInstanceCountMismatch),
		errors.Is(err, hpa.ErrNegativeReplicas),
		errors.Is(err, hpa.ErrZeroMetricTimestamp):
		writeErr(w, 422, "invalid_request", err)
	case errors.Is(err, store.ErrDecisionExists):
		// Same event time replayed: return the stored decision unchanged.
		if body, ok, gerr := s.st.GetDecision(r.Context(), scalerID, req.MetricTimestamp); gerr == nil && ok {
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("X-Idempotent-Replay", "true")
			w.WriteHeader(200)
			_, _ = w.Write(append(body, '\n'))
			return
		}
		writeErr(w, 500, "replay_lookup_failed", err)
	default:
		writeErr(w, 500, "decision_failed", err)
	}
}

func (s *Server) listDecisions(w http.ResponseWriter, r *http.Request) {
	scalerID := r.PathValue("id")
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	rows, err := s.st.ListDecisions(r.Context(), scalerID, limit)
	if err != nil {
		writeErr(w, 500, "store_error", err)
		return
	}
	writeJSON(w, 200, map[string]any{"scalerId": scalerID, "decisions": rows})
}

func (s *Server) listRecommendations(w http.ResponseWriter, r *http.Request) {
	scalerID := r.PathValue("id")
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	rows, err := s.st.ListRecommendations(r.Context(), scalerID, limit)
	if err != nil {
		writeErr(w, 500, "store_error", err)
		return
	}
	writeJSON(w, 200, map[string]any{"scalerId": scalerID, "recommendations": rows})
}

// ---------------- JSON number helpers (encoding/json decodes to float64) ----

func asInt(v any) int {
	switch x := v.(type) {
	case float64:
		return int(x)
	case int:
		return x
	case json.Number:
		n, _ := x.Int64()
		return int(n)
	}
	return 0
}

func asFloat(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case int:
		return float64(x)
	case json.Number:
		f, _ := x.Float64()
		return f
	}
	return 0
}
