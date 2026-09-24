// Package api wires the solver to an HTTP API built on net/http.
package api

import (
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"time"

	"robotdispatch/model"
	"robotdispatch/solver"
)

// maxBodyBytes caps request bodies (1 MiB is far above a size-capped instance).
const maxBodyBytes = 1 << 20

// NewServer builds the HTTP handler with all routes registered.
func NewServer() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", healthz)
	mux.HandleFunc("GET /{$}", root)
	mux.HandleFunc("/api/v1/plans", createPlan)
	return logging(mux)
}

func healthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func root(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"service": "robot-task-allocation",
		"endpoints": map[string]string{
			"GET  /healthz":      "liveness probe",
			"POST /api/v1/plans": "solve an offline allocation instance",
		},
		"example": "curl -s localhost:8080/api/v1/plans -d @examples/must_charge.json",
	})
}

func createPlan(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		writeError(w, http.StatusMethodNotAllowed, "method not allowed; use POST")
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
	dec := json.NewDecoder(r.Body)
	var req model.Request
	if err := dec.Decode(&req); err != nil {
		var serr *json.SyntaxError
		if errors.As(err, &serr) {
			writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		if errors.Is(err, io.EOF) {
			writeError(w, http.StatusBadRequest, "request body is empty")
			return
		}
		writeError(w, http.StatusBadRequest, "invalid request: "+err.Error())
		return
	}
	if dec.More() {
		writeError(w, http.StatusBadRequest, "request body must contain exactly one JSON object")
		return
	}

	epd, err := req.Validate()
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	resp := solver.Solve(&req, epd)
	status := http.StatusOK
	writeJSON(w, status, resp)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		log.Printf("write response: %v", err)
	}
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

// logging is a minimal request logger using only the standard library.
func logging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		log.Printf("%s %s -> %d (%s)", r.Method, r.URL.Path, rec.status, time.Since(start))
	})
}
