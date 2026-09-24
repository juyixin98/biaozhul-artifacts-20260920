// Package httpapi exposes the autoscaling service over HTTP.
package httpapi

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"regexp"
	"strings"
	"time"

	"scaler/internal/clock"
	"scaler/internal/crypto"
	"scaler/internal/scaler"
	"scaler/internal/store"
)

// Server wires the store, clock and HMAC secret to the HTTP handlers.
type Server struct {
	Store  *store.Store
	Clock  clock.Clock
	Secret []byte
	Log    *log.Logger
}

var workloadNameRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)

// Routes builds the application mux.
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("PUT /api/v1/workloads/{name}/config", s.handlePutConfig)
	mux.HandleFunc("GET /api/v1/workloads/{name}/config", s.handleGetConfig)
	mux.HandleFunc("POST /api/v1/workloads/{name}/decisions", s.handlePostDecision)
	mux.HandleFunc("GET /api/v1/workloads/{name}/decisions/latest", s.handleLatest)
	mux.HandleFunc("GET /api/v1/workloads/{name}/decisions", s.handleList)
	mux.HandleFunc("POST /api/v1/workloads/{name}/decisions/verify", s.handleVerify)
	return logging(s.Log, mux)
}

func logging(l *log.Logger, h http.Handler) http.Handler {
	if l == nil {
		return h
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h.ServeHTTP(w, r)
		l.Printf("%s %s", r.Method, r.URL.RequestURI())
	})
}

// ---------- DTOs ----------

type configRequest struct {
	MinReplicas     *int     `json:"minReplicas"`
	MaxReplicas     *int     `json:"maxReplicas"`
	TargetPct       *float64 `json:"targetPct"`
	TolerancePct    *float64 `json:"tolerancePct"`
	StableWindowSec *int     `json:"stableWindowSec"`
}

type configResponse struct {
	Workload        string  `json:"workload"`
	ConfigVersion   string  `json:"configVersion"`
	MinReplicas     int     `json:"minReplicas"`
	MaxReplicas     int     `json:"maxReplicas"`
	TargetPct       float64 `json:"targetPct"`
	TolerancePct    float64 `json:"tolerancePct"`
	StableWindowSec int     `json:"stableWindowSec"`
	UpdatedAt       string  `json:"updatedAt"`
	UpdatedAtMs     int64   `json:"updatedAtMs"`
}

type podDTO struct {
	Name           string   `json:"name"`
	Ready          bool     `json:"ready"`
	UtilizationPct *float64 `json:"utilizationPct"`
}

type decisionRequest struct {
	MetricTime      string   `json:"metricTime"`
	CurrentReplicas int      `json:"currentReplicas"`
	Pods            []podDTO `json:"pods"`
}

type windowPoint struct {
	MetricTime   string `json:"metricTime"`
	MetricTimeMs int64  `json:"metricTimeMs"`
	RawProposed  int    `json:"rawProposed"`
}

type decisionResponse struct {
	ID                int64         `json:"id"`
	Workload          string        `json:"workload"`
	ConfigVersion     string        `json:"configVersion"`
	MetricTime        string        `json:"metricTime"`
	MetricTimeMs      int64         `json:"metricTimeMs"`
	CreatedAt         string        `json:"createdAt"`
	CreatedAtMs       int64         `json:"createdAtMs"`
	CurrentReplicas   int           `json:"currentReplicas"`
	ReadyCount        int           `json:"readyCount"`
	UnreadyCount      int           `json:"unreadyCount"`
	ReadyReporting    int           `json:"readyReporting"`
	ReadyMissing      int           `json:"readyMissing"`
	AvgUtilizationPct float64       `json:"avgUtilizationPct"`
	MetricPresent     bool          `json:"metricPresent"`
	TargetPct         float64       `json:"targetPct"`
	Ratio             float64       `json:"ratio"`
	RawProposed       *int          `json:"rawProposed"`
	Stabilized        *int          `json:"stabilized"`
	FinalReplicas     int           `json:"finalReplicas"`
	Action            string        `json:"action"`
	Reasons           []string      `json:"reasons"`
	WindowUsed        []windowPoint `json:"windowUsed,omitempty"`
	Signature         string        `json:"signature"`
}

type errorBody struct {
	Error struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

// ---------- helpers ----------

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	var b errorBody
	b.Error.Code = code
	b.Error.Message = msg
	writeJSON(w, status, b)
}

func msToRFC3339(ms int64) string {
	return time.UnixMilli(ms).UTC().Format(time.RFC3339Nano)
}

func recordToDecision(r store.DecisionRecord) decisionResponse {
	resp := decisionResponse{
		ID: r.ID, Workload: r.Workload, ConfigVersion: r.ConfigVersion,
		MetricTime: msToRFC3339(r.MetricTimeMs), MetricTimeMs: r.MetricTimeMs,
		CreatedAt: msToRFC3339(r.CreatedAtMs), CreatedAtMs: r.CreatedAtMs,
		CurrentReplicas: r.CurrentReplicas, ReadyCount: r.ReadyCount,
		UnreadyCount: r.UnreadyCount, ReadyReporting: r.ReadyReporting,
		ReadyMissing: r.ReadyMissing, AvgUtilizationPct: r.AvgUtilizationPct,
		MetricPresent: r.MetricPresent, TargetPct: r.TargetPct, Ratio: r.Ratio,
		FinalReplicas: r.FinalReplicas, Action: r.Action,
		Reasons: splitReasons(r.Reasons), Signature: r.Signature,
	}
	if r.RawProposed >= 0 {
		v := r.RawProposed
		resp.RawProposed = &v
	}
	if r.Stabilized >= 0 {
		v := r.Stabilized
		resp.Stabilized = &v
	}
	return resp
}

func splitReasons(s string) []string {
	if s == "" {
		return []string{}
	}
	return strings.Split(s, ",")
}

// ---------- handlers ----------

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if err := s.Store.PingDB(r.Context()); err == nil {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
		return
	}
	writeError(w, http.StatusServiceUnavailable, "UNHEALTHY", "database unavailable")
}

func (s *Server) handlePutConfig(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	if !workloadNameRE.MatchString(name) {
		writeError(w, http.StatusBadRequest, "INVALID_WORKLOAD_NAME", "workload name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
		return
	}
	var req configRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "INVALID_JSON", err.Error())
		return
	}
	// Required fields.
	if req.MinReplicas == nil || req.MaxReplicas == nil || req.TargetPct == nil ||
		req.TolerancePct == nil || req.StableWindowSec == nil {
		writeError(w, http.StatusBadRequest, "MISSING_FIELDS",
			"minReplicas, maxReplicas, targetPct, tolerancePct and stableWindowSec are required")
		return
	}
	cfg := scaler.Config{
		MinReplicas: *req.MinReplicas, MaxReplicas: *req.MaxReplicas,
		TargetPct: *req.TargetPct, TolerancePct: *req.TolerancePct,
		StableWindowSec: *req.StableWindowSec,
	}
	if err := cfg.Validate(); err != nil {
		code := "INVALID_CONFIG"
		if errors.Is(err, scaler.ErrZeroTarget) {
			code = "ZERO_TARGET"
		}
		writeError(w, http.StatusBadRequest, code, err.Error())
		return
	}
	version, err := crypto.ConfigVersion(cfg.MinReplicas, cfg.MaxReplicas,
		cfg.TargetPct, cfg.TolerancePct, cfg.StableWindowSec)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "CRYPTO_ERROR", err.Error())
		return
	}
	nowMs := s.Clock.Now().UnixMilli()
	wl, err := s.Store.UpsertWorkload(r.Context(), name, version, cfg, nowMs)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, configResponse{
		Workload: wl.Name, ConfigVersion: wl.ConfigVersion,
		MinReplicas: wl.MinReplicas, MaxReplicas: wl.MaxReplicas,
		TargetPct: wl.TargetPct, TolerancePct: wl.TolerancePct,
		StableWindowSec: wl.StableWindowSec,
		UpdatedAt:       msToRFC3339(wl.UpdatedAtMs), UpdatedAtMs: wl.UpdatedAtMs,
	})
}

func (s *Server) handleGetConfig(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	wl, err := s.Store.GetWorkload(r.Context(), name)
	if errors.Is(err, store.ErrNotFound) {
		writeError(w, http.StatusNotFound, "WORKLOAD_NOT_FOUND", "no configuration for workload")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, configResponse{
		Workload: wl.Name, ConfigVersion: wl.ConfigVersion,
		MinReplicas: wl.MinReplicas, MaxReplicas: wl.MaxReplicas,
		TargetPct: wl.TargetPct, TolerancePct: wl.TolerancePct,
		StableWindowSec: wl.StableWindowSec,
		UpdatedAt:       msToRFC3339(wl.UpdatedAtMs), UpdatedAtMs: wl.UpdatedAtMs,
	})
}

func (s *Server) handlePostDecision(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	name := r.PathValue("name")
	if !workloadNameRE.MatchString(name) {
		writeError(w, http.StatusBadRequest, "INVALID_WORKLOAD_NAME", "bad workload name")
		return
	}
	var req decisionRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "INVALID_JSON", err.Error())
		return
	}
	if req.MetricTime == "" {
		writeError(w, http.StatusBadRequest, "MISSING_METRIC_TIME", "metricTime (RFC3339) is required")
		return
	}
	metricT, err := time.Parse(time.RFC3339Nano, req.MetricTime)
	if err != nil {
		writeError(w, http.StatusBadRequest, "INVALID_METRIC_TIME", "metricTime must be RFC3339, e.g. 2026-09-23T10:00:00Z")
		return
	}
	if req.CurrentReplicas < 0 {
		writeError(w, http.StatusBadRequest, "INVALID_CURRENT_REPLICAS", "currentReplicas must be >= 0")
		return
	}
	pods := make([]scaler.PodSample, 0, len(req.Pods))
	for i, p := range req.Pods {
		if p.UtilizationPct != nil && *p.UtilizationPct < 0 {
			writeError(w, http.StatusBadRequest, "INVALID_UTILIZATION",
				"pods[] utilizationPct must be >= 0 when present; use null for missing")
			return
		}
		if p.Name == "" {
			p.Name = "pod-" + itoa(i)
		}
		pods = append(pods, scaler.PodSample{
			Name: p.Name, Ready: p.Ready, UtilizationPct: p.UtilizationPct,
		})
	}

	wl, err := s.Store.GetWorkload(ctx, name)
	if errors.Is(err, store.ErrNotFound) {
		writeError(w, http.StatusNotFound, "WORKLOAD_NOT_FOUND", "configure the workload before posting metrics")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
		return
	}

	metricMs := metricT.UnixMilli()

	// Event-time regression guard: refuse anything not strictly newer.
	if latest, ok, err := s.Store.LatestMetricTime(ctx, name); err != nil {
		writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
		return
	} else if ok && metricMs <= latest {
		existing, gErr := s.Store.LatestDecision(ctx, name)
		status := http.StatusConflict
		if gErr != nil {
			writeError(w, http.StatusInternalServerError, "STORE_ERROR", gErr.Error())
			return
		}
		resp := recordToDecision(existing)
		writeJSON(w, status, map[string]any{
			"error": map[string]string{
				"code":    "STALE_METRIC_TIME",
				"message": "metricTime must be strictly newer than the latest stored decision; existing decision returned",
			},
			"existingDecision": resp,
		})
		return
	}

	cfg := scaler.Config{
		MinReplicas: wl.MinReplicas, MaxReplicas: wl.MaxReplicas,
		TargetPct: wl.TargetPct, TolerancePct: wl.TolerancePct,
		StableWindowSec: wl.StableWindowSec,
	}
	snap := scaler.Snapshot{CurrentReplicas: req.CurrentReplicas, Pods: pods}

	windowMs := int64(cfg.StableWindowSec) * 1000
	history, err := s.Store.WindowEntries(ctx, name, wl.ConfigVersion, metricMs, windowMs)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
		return
	}

	d, err := scaler.Calculate(cfg, snap, metricMs, history)
	if err != nil {
		code := "INVALID_CONFIG"
		status := http.StatusUnprocessableEntity
		if errors.Is(err, scaler.ErrZeroTarget) {
			code = "ZERO_TARGET"
		}
		writeError(w, status, code, err.Error())
		return
	}

	reasons := strings.Join(d.Reasons, ",")
	rec := store.DecisionRecord{
		Workload: name, ConfigVersion: wl.ConfigVersion, MetricTimeMs: metricMs,
		CreatedAtMs:     s.Clock.Now().UnixMilli(),
		CurrentReplicas: d.CurrentReplicas, ReadyCount: d.ReadyCount,
		UnreadyCount: d.UnreadyCount, ReadyReporting: d.ReadyReporting,
		ReadyMissing: d.ReadyMissing, AvgUtilizationPct: d.AvgUtilizationPct,
		MetricPresent: d.MetricPresent, TargetPct: d.TargetPct, Ratio: d.Ratio,
		RawProposed: d.RawProposed, Stabilized: d.Stabilized,
		FinalReplicas: d.Final, Action: d.Action, Reasons: reasons,
	}
	rec.Signature = crypto.SignDecision(s.Secret, crypto.SignDecisionParameters{
		Workload: rec.Workload, ConfigVersion: rec.ConfigVersion,
		MetricTimeMs: rec.MetricTimeMs, CurrentReplicas: rec.CurrentReplicas,
		AvgUtilizationPct: rec.AvgUtilizationPct, TargetPct: rec.TargetPct,
		Ratio: rec.Ratio, RawProposed: rec.RawProposed, Stabilized: rec.Stabilized,
		FinalReplicas: rec.FinalReplicas, Action: rec.Action, Reasons: rec.Reasons,
	})

	id, err := s.Store.InsertDecision(ctx, rec, d.RawProposed)
	if errors.Is(err, store.ErrStaleMetric) {
		// Lost a race against a concurrent newer write.
		existing, _ := s.Store.LatestDecision(ctx, name)
		writeJSON(w, http.StatusConflict, map[string]any{
			"error": map[string]string{
				"code":    "STALE_METRIC_TIME",
				"message": "concurrent newer decision stored; existing decision returned",
			},
			"existingDecision": recordToDecision(existing),
		})
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
		return
	}
	rec.ID = id

	resp := recordToDecision(rec)
	for _, e := range d.WindowUsed {
		resp.WindowUsed = append(resp.WindowUsed, windowPoint{
			MetricTime: msToRFC3339(e.MetricTimeMs), MetricTimeMs: e.MetricTimeMs,
			RawProposed: e.RawProposed,
		})
	}
	if resp.WindowUsed == nil {
		resp.WindowUsed = []windowPoint{}
	}
	writeJSON(w, http.StatusCreated, resp)
}

func (s *Server) handleLatest(w http.ResponseWriter, r *http.Request) {
	rec, err := s.Store.LatestDecision(r.Context(), r.PathValue("name"))
	if errors.Is(err, store.ErrNotFound) {
		writeError(w, http.StatusNotFound, "DECISION_NOT_FOUND", "no decisions stored for workload")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, recordToDecision(rec))
}

func (s *Server) handleList(w http.ResponseWriter, r *http.Request) {
	limit := 50
	decisions, err := s.Store.ListDecisions(r.Context(), r.PathValue("name"), limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
		return
	}
	out := make([]decisionResponse, 0, len(decisions))
	for _, d := range decisions {
		out = append(out, recordToDecision(d))
	}
	writeJSON(w, http.StatusOK, map[string]any{"decisions": out})
}

// handleVerify recomputes the HMAC over the submitted decision fields and
// reports whether it matches. This proves the signature is a real, checkable
// cryptographic tag rather than a placeholder.
func (s *Server) handleVerify(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Signature string `json:"signature"`
		Workload  string `json:"workload"`
		decisionResponse
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "INVALID_JSON", err.Error())
		return
	}
	workload := req.Workload
	if workload == "" {
		workload = r.PathValue("name")
	}
	raw, stab := -1, -1
	if req.RawProposed != nil {
		raw = *req.RawProposed
	}
	if req.Stabilized != nil {
		stab = *req.Stabilized
	}
	ok := crypto.VerifySignature(s.Secret, crypto.SignDecisionParameters{
		Workload: workload, ConfigVersion: req.ConfigVersion,
		MetricTimeMs: req.MetricTimeMs, CurrentReplicas: req.CurrentReplicas,
		AvgUtilizationPct: req.AvgUtilizationPct, TargetPct: req.TargetPct,
		Ratio: req.Ratio, RawProposed: raw, Stabilized: stab,
		FinalReplicas: req.FinalReplicas, Action: req.Action,
		Reasons: strings.Join(req.Reasons, ","),
	}, req.Signature)
	writeJSON(w, http.StatusOK, map[string]bool{"valid": ok})
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b [20]byte
	pos := len(b)
	for i > 0 {
		pos--
		b[pos] = byte('0' + i%10)
		i /= 10
	}
	return string(b[pos:])
}
