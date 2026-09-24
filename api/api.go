// Package api wires the simulation engine to net/http handlers.
package api

import (
	"encoding/json"
	"net/http"

	"checkpoint-scheduler/sim"
)

// maxBodyBytes bounds the request body to keep the service un-memory-bomb-able.
const maxBodyBytes = 1 << 20 // 1 MiB

// Request is the JSON body for POST /simulate.
type Request struct {
	Capacity int            `json:"capacity"`
	Tasks    []sim.TaskSpec `json:"tasks"`
}

// errorJSON writes a 4xx/5xx response with a JSON error body.
func errorJSON(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": msg})
}

// NewHandler returns the root handler with all routes registered.
func NewHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /", indexHandler)
	mux.HandleFunc("GET /healthz", healthHandler)
	mux.HandleFunc("POST /simulate", simulateHandler)
	return mux
}

func indexHandler(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		errorJSON(w, http.StatusNotFound, "not found; available endpoints: GET /healthz, POST /simulate")
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"service": "preemptible-checkpoint-scheduler",
		"description": "Tick-based offline cluster simulation: higher-priority tasks may preempt " +
			"lower-priority tasks, which can only resume from fully committed checkpoints. " +
			"Runs both preemptive and non-preemptive policies and compares completion ticks.",
		"endpoints": map[string]string{
			"GET  /healthz":  "liveness probe",
			"POST /simulate": "body: {\"capacity\": int, \"tasks\": [...]} -> comparison of both policies",
		},
		"task_fields": map[string]string{
			"id":               "unique non-empty string",
			"priority":         "int, larger preempts smaller",
			"arrival_tick":     "int >= 0, when the task becomes available",
			"total_work":       "int >= 1, execution units (checkpoint saves are extra ticks)",
			"checkpoint_every": "int >= 1, save after this many units since last committed checkpoint",
			"checkpoint_cost":  "int >= 0, ticks a save takes while holding resources",
			"resource_demand":  "int >= 1, capacity occupied while running or saving",
		},
	})
}

func healthHandler(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

func simulateHandler(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
	var req Request
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		errorJSON(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if len(req.Tasks) == 0 {
		errorJSON(w, http.StatusBadRequest, "tasks must contain at least one task")
		return
	}
	result, err := sim.Simulate(req.Capacity, req.Tasks)
	if err != nil {
		errorJSON(w, http.StatusBadRequest, err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(result)
}
