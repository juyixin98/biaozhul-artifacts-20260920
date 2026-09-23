// Package httpapi exposes the DNS proxy over net/http with no third-party
// dependencies. JSON in and out.
package httpapi

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"time"

	"dnscomp-proxy/internal/dnsmsg"
	"dnscomp-proxy/internal/proxy"
)

// NewServer wires the routes to the given proxy.
func NewServer(p *proxy.Proxy, logger *log.Logger) http.Handler {
	if logger == nil {
		logger = log.New(log.Writer(), "[httpapi] ", log.LstdFlags)
	}
	mux := http.NewServeMux()
	s := &server{p: p, log: logger}
	mux.HandleFunc("GET /resolve", s.handleResolve)
	mux.HandleFunc("GET /cache", s.handleCacheList)
	mux.HandleFunc("DELETE /cache", s.handleCacheFlush)
	mux.HandleFunc("GET /healthz", s.handleHealth)
	return logging(logger, mux)
}

type server struct {
	p   *proxy.Proxy
	log *log.Logger
}

// resolveResponse is the JSON body of GET /resolve.
type resolveResponse struct {
	Name    string       `json:"name"`
	Type    string       `json:"type"`
	Source  string       `json:"source"`
	Answers []answerJSON `json:"answers"`
}

type answerJSON struct {
	Type string `json:"type"`
	IP   string `json:"ip"`
	TTL  uint32 `json:"ttl"`
}

func (s *server) handleResolve(w http.ResponseWriter, r *http.Request) {
	name := r.URL.Query().Get("name")
	typeStr := r.URL.Query().Get("type")
	if typeStr == "" {
		typeStr = "A"
	}
	var qtype uint16
	switch typeStr {
	case "A", "a", "1":
		qtype = dnsmsg.TypeA
	case "AAAA", "aaaa", "28":
		qtype = dnsmsg.TypeAAAA
	default:
		writeError(w, http.StatusBadRequest, "type must be A or AAAA")
		return
	}

	res, err := s.p.Resolve(name, qtype)
	if err != nil {
		status := http.StatusBadGateway
		switch {
		case errors.Is(err, proxy.ErrInvalidRequest):
			status = http.StatusBadRequest
		case errors.Is(err, proxy.ErrUpstream):
			status = http.StatusBadGateway
		case errors.Is(err, proxy.ErrBadResponse):
			status = http.StatusBadGateway
		}
		writeError(w, status, err.Error())
		return
	}

	answers := make([]answerJSON, 0, len(res.Answers))
	for _, a := range res.Answers {
		answers = append(answers, answerJSON{Type: a.Type, IP: a.IP, TTL: a.TTL})
	}
	writeJSON(w, http.StatusOK, resolveResponse{
		Name:    res.Name,
		Type:    res.Type,
		Source:  res.Source,
		Answers: answers,
	})
}

func (s *server) handleCacheList(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"entries": s.p.Cache().Snapshot(),
	})
}

func (s *server) handleCacheFlush(w http.ResponseWriter, r *http.Request) {
	n := s.p.Cache().Flush()
	writeJSON(w, http.StatusOK, map[string]any{"flushed": n})
}

func (s *server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "time": time.Now().UTC().Format(time.RFC3339)})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"error": msg, "status": status})
}

// statusRecorder lets the request logger see the final status code.
type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func logging(logger *log.Logger, h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		h.ServeHTTP(rec, r)
		logger.Printf("%s %s -> %d (%s)", r.Method, r.URL.RequestURI(), rec.status, time.Since(start))
	})
}
