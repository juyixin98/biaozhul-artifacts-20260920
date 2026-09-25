// Package server exposes ingestion, correction and query over HTTP.
package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"metricrollup/internal/model"
	"metricrollup/internal/store"
)

// Server wires a store and (optional) snapshot path to HTTP handlers.
type Server struct {
	store        *store.Store
	snapshotPath string
	mux          *http.ServeMux
	logger       *log.Logger
}

// New builds the HTTP server. If snapshotPath is non-empty, /v1/admin/snapshot
// persists the store there.
func New(st *store.Store, snapshotPath string, logger *log.Logger) *Server {
	if logger == nil {
		logger = log.Default()
	}
	s := &Server{store: st, snapshotPath: snapshotPath, logger: logger}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /v1/series", s.handleSeries)
	mux.HandleFunc("POST /v1/ingest", s.handleIngest)
	mux.HandleFunc("POST /v1/correct", s.handleCorrect)
	mux.HandleFunc("GET /v1/query", s.handleQuery)
	mux.HandleFunc("POST /v1/admin/prune", s.handlePrune)
	mux.HandleFunc("POST /v1/admin/snapshot", s.handleSnapshot)
	s.mux = mux
	return s
}

// Handler exposes the routed handler (used by tests and main).
func (s *Server) Handler() http.Handler { return requestLog(s.logger, s.mux) }

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// ingestRequest is the POST /v1/ingest body.
type ingestRequest struct {
	Samples []model.Sample `json:"samples"`
}

// ingestResponse reports how many samples were accepted.
type ingestResponse struct {
	Ingested int `json:"ingested"`
}

func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	var req ingestRequest
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if len(req.Samples) == 0 {
		writeError(w, http.StatusBadRequest, errors.New("samples must not be empty"))
		return
	}
	for i, smp := range req.Samples {
		if err := validateSample(smp); err != nil {
			writeError(w, http.StatusBadRequest, fmt.Errorf("samples[%d]: %w", i, err))
			return
		}
	}
	s.store.Ingest(req.Samples)
	writeJSON(w, http.StatusOK, ingestResponse{Ingested: len(req.Samples)})
}

// correctRequest is the POST /v1/correct body.
type correctRequest struct {
	Metric string            `json:"metric"`
	Labels map[string]string `json:"labels"`
	Ts     int64             `json:"ts"`
	Value  float64           `json:"value"`
}

func (s *Server) handleCorrect(w http.ResponseWriter, r *http.Request) {
	var req correctRequest
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if strings.TrimSpace(req.Metric) == "" {
		writeError(w, http.StatusBadRequest, errors.New("metric is required"))
		return
	}
	if math.IsNaN(req.Value) || math.IsInf(req.Value, 0) {
		writeError(w, http.StatusBadRequest, errors.New("value must be finite"))
		return
	}
	err := s.store.Correct(req.Metric, req.Labels, req.Ts, req.Value)
	switch {
	case errors.Is(err, store.ErrNotFound):
		writeError(w, http.StatusNotFound, err)
	case errors.Is(err, store.ErrRawUnavailable):
		writeError(w, http.StatusConflict, err)
	case err != nil:
		writeError(w, http.StatusInternalServerError, err)
	default:
		writeJSON(w, http.StatusOK, map[string]string{"status": "corrected"})
	}
}

// queryResponse is the GET /v1/query result envelope.
type queryResponse struct {
	Metric  string            `json:"metric"`
	Labels  map[string]string `json:"labels"`
	Layer   store.Layer       `json:"layer"`
	Start   int64             `json:"start"`
	End     int64             `json:"end"`
	Width   int64             `json:"width_sec"`
	Buckets []store.Bucket    `json:"buckets"`
}

func (s *Server) handleQuery(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	metric := strings.TrimSpace(q.Get("metric"))
	if metric == "" {
		writeError(w, http.StatusBadRequest, errors.New("metric is required"))
		return
	}
	layer := store.Layer(q.Get("layer"))
	width, err := layer.Width()
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	start, err := parseUnixTime(q.Get("start"))
	if err != nil {
		writeError(w, http.StatusBadRequest, fmt.Errorf("start: %w", err))
		return
	}
	end, err := parseUnixTime(q.Get("end"))
	if err != nil {
		writeError(w, http.StatusBadRequest, fmt.Errorf("end: %w", err))
		return
	}
	labels, err := parseLabels(q["label"])
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}

	buckets, err := s.store.Query(metric, labels, start, end, layer)
	if errors.Is(err, store.ErrNotFound) {
		writeError(w, http.StatusNotFound, err)
		return
	}
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, queryResponse{
		Metric: metric, Labels: labels, Layer: layer,
		Start: start, End: end, Width: width, Buckets: buckets,
	})
}

func (s *Server) handleSeries(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"series": s.store.ListSeries()})
}

// pruneRequest drops raw samples older than Cutoff (Unix seconds).
type pruneRequest struct {
	Cutoff int64 `json:"cutoff"`
}

func (s *Server) handlePrune(w http.ResponseWriter, r *http.Request) {
	var req pruneRequest
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if req.Cutoff <= 0 {
		writeError(w, http.StatusBadRequest, errors.New("cutoff must be a positive Unix timestamp"))
		return
	}
	removed := s.store.PruneRaw(req.Cutoff)
	writeJSON(w, http.StatusOK, map[string]int{"removed_raw_samples": removed})
}

func (s *Server) handleSnapshot(w http.ResponseWriter, _ *http.Request) {
	if s.snapshotPath == "" {
		writeError(w, http.StatusConflict, errors.New("snapshot persistence disabled (no data file configured)"))
		return
	}
	if err := s.store.Save(s.snapshotPath); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "saved", "path": s.snapshotPath})
}

func validateSample(smp model.Sample) error {
	if strings.TrimSpace(smp.Metric) == "" {
		return errors.New("metric is required")
	}
	if smp.Ts < 0 {
		return errors.New("ts must be a non-negative Unix timestamp")
	}
	if math.IsNaN(smp.Value) || math.IsInf(smp.Value, 0) {
		return errors.New("value must be finite")
	}
	for k, v := range smp.Labels {
		if strings.TrimSpace(k) == "" {
			return errors.New("label keys must be non-empty")
		}
		_ = v
	}
	return nil
}

// parseUnixTime accepts an integer Unix-second timestamp or an RFC3339 string.
func parseUnixTime(raw string) (int64, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return 0, errors.New("required unix seconds or RFC3339 time")
	}
	if n, err := strconv.ParseInt(raw, 10, 64); err == nil {
		return n, nil
	}
	t, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		return 0, errors.New("not unix seconds or RFC3339 time")
	}
	return t.Unix(), nil
}

// parseLabels parses repeated k=v label query parameters.
func parseLabels(vals []string) (map[string]string, error) {
	labels := map[string]string{}
	for _, v := range vals {
		k, val, ok := strings.Cut(v, "=")
		if !ok || strings.TrimSpace(k) == "" {
			return nil, fmt.Errorf("label %q must be k=v", v)
		}
		labels[k] = val
	}
	return labels, nil
}

func decodeBody(r *http.Request, dst any) error {
	dec := json.NewDecoder(http.MaxBytesReader(nil, r.Body, 4<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return fmt.Errorf("invalid JSON body: %w", err)
	}
	if dec.More() {
		return errors.New("invalid JSON body: multiple JSON values")
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, map[string]string{"error": err.Error()})
}

// requestLog logs one line per request.
func requestLog(logger *log.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rw, r)
		logger.Printf("%s %s -> %d (%s)", r.Method, r.URL.RequestURI(), rw.status, time.Since(start).Round(time.Microsecond))
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}
