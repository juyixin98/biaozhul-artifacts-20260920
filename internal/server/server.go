// Package server exposes HTTP endpoints for histogram ingestion and queries.
package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strings"
	"time"

	"histmerge/internal/histogram"
	"histmerge/internal/store"
)

// Server wires the store to HTTP handlers.
type Server struct {
	Store *store.Store
	Mux   *http.ServeMux
}

// New builds the router.
func New(st *store.Store) *Server {
	s := &Server{Store: st, Mux: http.NewServeMux()}
	s.Mux.HandleFunc("/healthz", s.health)
	s.Mux.HandleFunc("/api/v1/ingest", s.ingest)
	s.Mux.HandleFunc("/api/v1/series", s.series)
	s.Mux.HandleFunc("/api/v1/query", s.query)
	s.Mux.HandleFunc("/api/v1/query_range", s.queryRange)
	return s
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// ---- ingest ----

type ingestItem struct {
	Timestamp *time.Time           `json:"timestamp,omitempty"`
	Labels    []store.Label        `json:"labels"`
	Histogram *histogram.Histogram `json:"histogram"`
}

type ingestResultItem struct {
	Index  int    `json:"index"`
	OK     bool   `json:"ok"`
	Error  string `json:"error,omitempty"`
	Series string `json:"series,omitempty"`
}

type ingestResponse struct {
	Accepted int                `json:"accepted"`
	Rejected int                `json:"rejected"`
	Results  []ingestResultItem `json:"results"`
}

func (s *Server) ingest(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "use POST")
		return
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeError(w, http.StatusBadRequest, "read body: "+err.Error())
		return
	}
	var items []ingestItem
	// Batch form: {"samples":[...]}. Single form: one sample object.
	var probe struct {
		Samples *json.RawMessage `json:"samples"`
	}
	if err := json.Unmarshal(body, &probe); err == nil && probe.Samples != nil {
		if err := json.Unmarshal(*probe.Samples, &items); err != nil {
			writeError(w, http.StatusBadRequest, "invalid samples array: "+err.Error())
			return
		}
	} else {
		var one ingestItem
		if err := json.Unmarshal(body, &one); err != nil {
			writeError(w, http.StatusBadRequest, "invalid sample: "+err.Error())
			return
		}
		items = []ingestItem{one}
	}

	resp := ingestResponse{Results: make([]ingestResultItem, 0, len(items))}
	var conflict bool
	for i, it := range items {
		smp, err := toSample(it)
		if err == nil {
			err = s.Store.Ingest(smp)
		}
		res := ingestResultItem{Index: i}
		if err != nil {
			res.Error = err.Error()
			resp.Rejected++
			if errors.Is(err, store.ErrCounterReset) || errors.Is(err, store.ErrOutOfOrder) {
				conflict = true
			}
		} else {
			res.OK = true
			res.Series = store.SeriesKey(smp.Labels)
			resp.Accepted++
		}
		resp.Results = append(resp.Results, res)
	}
	status := http.StatusOK
	switch {
	case resp.Accepted == 0 && conflict:
		status = http.StatusConflict
	case resp.Accepted == 0:
		status = http.StatusUnprocessableEntity
	case resp.Rejected > 0:
		status = http.StatusConflict
	}
	writeJSON(w, status, resp)
}

func toSample(it ingestItem) (store.Sample, error) {
	if it.Histogram == nil {
		return store.Sample{}, errors.New("sample requires a histogram")
	}
	if len(it.Labels) == 0 {
		return store.Sample{}, errors.New("sample requires at least one label")
	}
	smp := store.Sample{Labels: it.Labels, Histogram: it.Histogram}
	if it.Timestamp != nil {
		smp.Timestamp = it.Timestamp.UTC()
	} else {
		smp.Timestamp = time.Now().UTC()
	}
	return smp, nil
}

// ---- series listing ----

func (s *Server) series(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "use GET")
		return
	}
	sel, err := selectorFromURLValues(r.URL.Query())
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	latest := s.Store.Latest(sel)
	type item struct {
		Key     string        `json:"series"`
		Labels  []store.Label `json:"labels"`
		Samples int           `json:"samples"`
	}
	out := make([]item, 0, len(latest))
	for _, smp := range latest {
		out = append(out, item{
			Key:    store.SeriesKey(smp.Labels),
			Labels: smp.Labels,
			// Latest() only returns the tip; sample count needs the store.
			Samples: s.Store.CountSamples(smp.Labels),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"series": out})
}

// ---- query ----

type queryRequestBody struct {
	Match     []string  `json:"match"`
	Quantiles []float64 `json:"quantiles"`
	Coarsen   *bool     `json:"coarsen"`
	From      time.Time `json:"from"`
	To        time.Time `json:"to"`
}

type queryResponse struct {
	SeriesCount    int                          `json:"series_count"`
	Strategy       string                       `json:"strategy"`
	CommonBounds   []string                     `json:"common_bounds,omitempty"`
	Merge          *histogram.Histogram         `json:"merge"`
	InputTotal     uint64                       `json:"input_total_count"`
	Conserved      bool                         `json:"total_count_conserved"`
	BucketConserve bool                         `json:"bucket_count_conserved"`
	Quantiles      []histogram.QuantileEstimate `json:"quantiles,omitempty"`
	Empty          bool                         `json:"empty"`
	Note           string                       `json:"note,omitempty"`
}

func (s *Server) query(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost && r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "use GET or POST")
		return
	}
	body, err := parseQueryBody(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	sel, err := selectorFromPairs(body.Match)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	latest := s.Store.Latest(sel)
	if len(latest) == 0 {
		writeError(w, http.StatusNotFound, "no_series: no series match the selector")
		return
	}
	hs := make([]*histogram.Histogram, len(latest))
	for i, smp := range latest {
		hs[i] = smp.Histogram
	}
	resp, err := buildQueryResponse(hs, body.Quantiles, coarsenOrDefault(body.Coarsen))
	if err != nil {
		writeError(w, mergeErrStatus(err), err.Error())
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) queryRange(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost && r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "use GET or POST")
		return
	}
	body, err := parseQueryBody(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	sel, err := selectorFromPairs(body.Match)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if body.From.IsZero() || body.To.IsZero() {
		writeError(w, http.StatusBadRequest, "query_range requires from and to (RFC3339)")
		return
	}
	windows := s.Store.Range(sel, body.From, body.To)
	var hs []*histogram.Histogram
	var skipped int
	for _, samples := range windows {
		h := store.Increase(samples)
		if h == nil {
			skipped++
			continue
		}
		hs = append(hs, h)
	}
	if len(hs) == 0 {
		writeError(w, http.StatusNotFound,
			"no_series: no series had >= 2 samples in the window (or a counter reset was seen)")
		return
	}
	resp, err := buildQueryResponse(hs, body.Quantiles, coarsenOrDefault(body.Coarsen))
	if err != nil {
		writeError(w, mergeErrStatus(err), err.Error())
		return
	}
	if skipped > 0 {
		resp.Note = fmt.Sprintf("%d series skipped (counter reset or <2 samples in window)", skipped)
	}
	writeJSON(w, http.StatusOK, resp)
}

func mergeErrStatus(err error) int {
	switch {
	case errors.Is(err, histogram.ErrIncompatibleLayout):
		return http.StatusConflict
	case errors.Is(err, histogram.ErrNoCommonBounds),
		errors.Is(err, histogram.ErrInvalidHistogram),
		errors.Is(err, histogram.ErrInvalidQuantile):
		return http.StatusUnprocessableEntity
	default:
		return http.StatusUnprocessableEntity
	}
}

func buildQueryResponse(hs []*histogram.Histogram, qs []float64, coarsen bool) (*queryResponse, error) {
	var res *histogram.MergeResult
	var err error
	if coarsen {
		res, err = histogram.Merge(hs...)
	} else {
		res, err = histogram.MergeStrict(hs...)
	}
	if err != nil {
		return nil, err
	}
	out := &queryResponse{
		SeriesCount:    len(hs),
		CommonBounds:   boundsToStrings(res.CommonBounds),
		Merge:          res.Merged,
		InputTotal:     res.InputTotalCounts,
		Conserved:      res.Conserved,
		BucketConserve: res.BucketConserved,
		Empty:          res.Merged.TotalCount == 0,
	}
	if res.Coarsened {
		out.Strategy = "coarsened"
	} else {
		out.Strategy = "direct"
	}
	if out.Empty {
		out.Note = "aggregate histogram is empty: quantiles undefined"
		return out, nil
	}
	if len(qs) > 0 {
		est, err := histogram.EstimateQuantiles(res.Merged, qs)
		if err != nil {
			return nil, err
		}
		out.Quantiles = est
	}
	return out, nil
}

// ---- request parsing helpers ----

func coarsenOrDefault(c *bool) bool {
	if c == nil {
		return true // safest default: allow contraction
	}
	return *c
}

func parseQueryBody(r *http.Request) (queryRequestBody, error) {
	var b queryRequestBody
	q := r.URL.Query()
	for _, m := range q["match[]"] {
		b.Match = append(b.Match, m)
	}
	for _, qv := range q["quantile"] {
		var f float64
		if _, err := fmt.Sscanf(qv, "%g", &f); err != nil {
			return b, fmt.Errorf("invalid quantile %q", qv)
		}
		b.Quantiles = append(b.Quantiles, f)
	}
	if v := q.Get("coarsen"); v == "false" {
		no := false
		b.Coarsen = &no
	} else if v == "true" {
		yes := true
		b.Coarsen = &yes
	}
	if v := q.Get("from"); v != "" {
		t, err := time.Parse(time.RFC3339, v)
		if err != nil {
			return b, fmt.Errorf("invalid from: %w", err)
		}
		b.From = t
	}
	if v := q.Get("to"); v != "" {
		t, err := time.Parse(time.RFC3339, v)
		if err != nil {
			return b, fmt.Errorf("invalid to: %w", err)
		}
		b.To = t
	}
	if r.Method == http.MethodPost {
		raw, err := io.ReadAll(r.Body)
		if err != nil {
			return b, err
		}
		if len(strings.TrimSpace(string(raw))) > 0 {
			if err := json.Unmarshal(raw, &b); err != nil {
				return b, fmt.Errorf("invalid JSON body: %w", err)
			}
		}
	}
	return b, nil
}

func selectorFromPairs(pairs []string) (store.Selector, error) {
	var sel store.Selector
	for _, m := range pairs {
		name, val, ok := strings.Cut(m, "=")
		if !ok || name == "" {
			return nil, fmt.Errorf("%w: selector must be name=value, got %q", store.ErrBadSelector, m)
		}
		sel = append(sel, store.Label{Name: name, Value: val})
	}
	return sel, nil
}

func selectorFromURLValues(q url.Values) (store.Selector, error) {
	var sel store.Selector
	for _, m := range q["match[]"] {
		name, val, ok := strings.Cut(m, "=")
		if !ok || name == "" {
			return nil, fmt.Errorf("%w: selector must be name=value, got %q", store.ErrBadSelector, m)
		}
		sel = append(sel, store.Label{Name: name, Value: val})
	}
	return sel, nil
}

func boundsToStrings(bs []histogram.Bound) []string {
	if bs == nil {
		return nil
	}
	out := make([]string, len(bs))
	for i, b := range bs {
		out[i] = b.String()
	}
	return out
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("write response: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
