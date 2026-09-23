// Package api implements the HTTP ingestion and query surface.
package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"

	"counterreset/counter"
	"counterreset/store"
)

// Server wires the store to HTTP handlers.
type Server struct {
	Store *store.Store
}

// New creates a Server with routes registered on a fresh mux.
func New(st *store.Store) http.Handler {
	s := &Server{Store: st}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.health)
	mux.HandleFunc("POST /api/v1/series", s.ingest)
	mux.HandleFunc("GET /api/v1/series", s.listSeries)
	mux.HandleFunc("GET /api/v1/query", s.query)
	mux.HandleFunc("POST /api/v1/admin/reset", s.adminReset)
	return mux
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// sampleJSON accepts t/value as numbers or t as an RFC3339 string.
type sampleJSON struct {
	T     flexTime `json:"t"`
	Value *float64 `json:"value"`
}

type ingestRequest struct {
	Metric  string            `json:"metric"`
	Labels  map[string]string `json:"labels"`
	Samples []sampleJSON      `json:"samples"`
}

type ingestResponse struct {
	Status           string            `json:"status"`
	Metric           string            `json:"metric"`
	Labels           map[string]string `json:"labels"`
	NewSamples       int               `json:"new_samples"`
	DuplicateIgnored int               `json:"duplicate_ignored"`
}

func (s *Server) ingest(w http.ResponseWriter, r *http.Request) {
	var req ingestRequest
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if len(req.Samples) == 0 {
		writeError(w, http.StatusBadRequest, "samples must not be empty")
		return
	}
	samples := make([]counter.Sample, 0, len(req.Samples))
	for _, sj := range req.Samples {
		if sj.Value == nil {
			writeError(w, http.StatusBadRequest, "each sample requires a value")
			return
		}
		samples = append(samples, counter.Sample{T: float64(sj.T), Value: *sj.Value})
	}
	n, dup, err := s.Store.Ingest(req.Metric, req.Labels, samples)
	if err != nil {
		code := http.StatusBadRequest
		if errors.Is(err, store.ErrConflict) {
			code = http.StatusConflict
		}
		writeError(w, code, err.Error())
		return
	}
	labels := req.Labels
	if labels == nil {
		labels = map[string]string{}
	}
	writeJSON(w, http.StatusOK, ingestResponse{
		Status: "accepted", Metric: req.Metric, Labels: labels,
		NewSamples: n, DuplicateIgnored: dup,
	})
}

type seriesOut struct {
	Metric  string            `json:"metric"`
	Labels  map[string]string `json:"labels"`
	Samples []counter.Sample  `json:"samples"`
	Count   int               `json:"sample_count"`
}

func (s *Server) listSeries(w http.ResponseWriter, r *http.Request) {
	metric := r.URL.Query().Get("metric")
	var all []store.Series
	if metric != "" {
		all = s.Store.Match(metric, map[string]string{})
	} else {
		all = s.Store.List()
	}
	out := make([]seriesOut, 0, len(all))
	for _, ser := range all {
		labels := ser.Labels
		if labels == nil {
			labels = map[string]string{}
		}
		out = append(out, seriesOut{
			Metric: ser.Metric, Labels: labels, Samples: ser.Samples, Count: len(ser.Samples),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "series": out})
}

type queryResponse struct {
	Status string                 `json:"status"`
	Result []counter.WindowResult `json:"result"`
}

func (s *Server) query(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	metric := q.Get("metric")
	if metric == "" {
		writeError(w, http.StatusBadRequest, "query parameter metric is required")
		return
	}
	from, err := parseTime(q.Get("from"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid from: "+err.Error())
		return
	}
	to, err := parseTime(q.Get("to"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid to: "+err.Error())
		return
	}
	if to < from {
		writeError(w, http.StatusBadRequest, "to must be >= from")
		return
	}
	selector := map[string]string{}
	for key, vals := range q {
		if strings.HasPrefix(key, "label.") && len(vals) > 0 {
			selector[strings.TrimPrefix(key, "label.")] = vals[0]
		}
	}
	maxInterval := 0.0
	if v := q.Get("max_interval"); v != "" {
		maxInterval, err = strconv.ParseFloat(v, 64)
		if err != nil || maxInterval < 0 {
			writeError(w, http.StatusBadRequest, "invalid max_interval")
			return
		}
	}

	matched := s.Store.Match(metric, selector)
	results := make([]counter.WindowResult, 0, len(matched))
	for _, ser := range matched {
		results = append(results, counter.Analyze(ser.Metric, ser.Labels, ser.Samples,
			counter.AnalysisOptions{From: from, To: to, MaxInterval: maxInterval}))
	}
	writeJSON(w, http.StatusOK, queryResponse{Status: "ok", Result: results})
}

func (s *Server) adminReset(w http.ResponseWriter, r *http.Request) {
	if err := s.Store.Reset(); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "reset"})
}

// ---------- helpers ----------

func decodeBody(r *http.Request, dst any) error {
	dec := json.NewDecoder(io.LimitReader(r.Body, 4<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return fmt.Errorf("invalid JSON: %w", err)
	}
	if dec.More() {
		return errors.New("unexpected trailing content in request body")
	}
	return nil
}

type errorBody struct {
	Status string `json:"status"`
	Error  string `json:"error"`
}

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, errorBody{Status: "error", Error: msg})
}

func writeJSON(w http.ResponseWriter, code int, body any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(body)
}
