// HTTP API for the task store. Pure backend, JSON in/out.
//
//	POST   /tasks                  {"id","payload","max_attempts"}  -> 201 task
//	GET    /tasks                  -> 200 [task]
//	GET    /tasks/{id}             -> 200 task | 404
//	POST   /tasks/{id}/claim       {"worker"}                       -> 200 task | 409
//	POST   /tasks/{id}/heartbeat   {"worker","attempt"}             -> 200 task | 409
//	POST   /tasks/{id}/complete    {"worker","attempt","result"}    -> 200 task | 409
//	POST   /tasks/{id}/fail        {"worker","attempt"}             -> 200 task | 409
//	POST   /tasks/{id}/cancel      {}                               -> 200 task | 409
//	POST   /tasks/{id}/retry       {}                               -> 200 task | 409
//	POST   /admin/sweep            {"now"} (RFC3339, optional)      -> 200 {"swept":true}
//
// 409 means the race was lost or the precondition failed; the response body
// names the reason ("terminal", "stale_attempt", ...).
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"log"
	"net/http"
	"time"
)

type server struct {
	store *Store
}

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	wal := flag.String("wal", "taskrace.wal", "WAL file path")
	flag.Parse()

	store, err := OpenStore(*wal)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer store.Close()

	// Background timeout sweeper: abandons attempts whose lease expired.
	go func() {
		tick := time.NewTicker(time.Second)
		defer tick.Stop()
		for range tick.C {
			if err := store.Sweep(time.Now()); err != nil {
				log.Printf("sweep: %v", err)
			}
		}
	}()

	s := &server{store: store}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /tasks", s.createTask)
	mux.HandleFunc("GET /tasks", s.listTasks)
	mux.HandleFunc("GET /tasks/{id}", s.getTask)
	mux.HandleFunc("POST /tasks/{id}/claim", s.claim)
	mux.HandleFunc("POST /tasks/{id}/heartbeat", s.heartbeat)
	mux.HandleFunc("POST /tasks/{id}/complete", s.complete)
	mux.HandleFunc("POST /tasks/{id}/fail", s.fail)
	mux.HandleFunc("POST /tasks/{id}/cancel", s.cancel)
	mux.HandleFunc("POST /tasks/{id}/retry", s.retry)
	mux.HandleFunc("POST /admin/sweep", s.sweep)

	log.Printf("listening on %s, WAL at %s", *addr, *wal)
	log.Fatal(http.ListenAndServe(*addr, mux))
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, err error) {
	code := http.StatusInternalServerError
	reason := "internal"
	switch {
	case errors.Is(err, ErrNotFound):
		code, reason = http.StatusNotFound, "not_found"
	case errors.Is(err, ErrTerminal):
		code, reason = http.StatusConflict, "terminal"
	case errors.Is(err, ErrStaleAttempt):
		code, reason = http.StatusConflict, "stale_attempt"
	case errors.Is(err, ErrNotRunning):
		code, reason = http.StatusConflict, "not_running"
	case errors.Is(err, ErrNotClaimable):
		code, reason = http.StatusConflict, "not_claimable"
	case errors.Is(err, ErrNotRetryable):
		code, reason = http.StatusConflict, "not_retryable"
	case errors.Is(err, ErrWorkerMismatch):
		code, reason = http.StatusConflict, "worker_mismatch"
	}
	writeJSON(w, code, map[string]string{"error": reason, "detail": err.Error()})
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	if r.Body == nil || r.ContentLength == 0 {
		return true // bodyless POSTs are fine, fields keep zero values
	}
	if err := json.NewDecoder(r.Body).Decode(v); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bad_json", "detail": err.Error()})
		return false
	}
	return true
}

func (s *server) createTask(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ID          string `json:"id"`
		Payload     string `json:"payload"`
		MaxAttempts int    `json:"max_attempts"`
	}
	if !decode(w, r, &req) {
		return
	}
	if req.ID == "" {
		req.ID = time.Now().Format("20060102150405.000000000")
	}
	t, err := s.store.Submit(req.ID, req.Payload, req.MaxAttempts)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, t)
}

func (s *server) listTasks(w http.ResponseWriter, r *http.Request) {
	tasks, err := s.store.List()
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, tasks)
}

func (s *server) getTask(w http.ResponseWriter, r *http.Request) {
	t, err := s.store.Get(r.PathValue("id"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *server) claim(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Worker string `json:"worker"`
	}
	if !decode(w, r, &req) {
		return
	}
	t, err := s.store.Claim(r.PathValue("id"), req.Worker)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *server) heartbeat(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Worker  string `json:"worker"`
		Attempt int    `json:"attempt"`
	}
	if !decode(w, r, &req) {
		return
	}
	t, err := s.store.Heartbeat(r.PathValue("id"), req.Worker, req.Attempt)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *server) complete(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Worker  string `json:"worker"`
		Attempt int    `json:"attempt"`
		Result  string `json:"result"`
	}
	if !decode(w, r, &req) {
		return
	}
	t, err := s.store.Complete(r.PathValue("id"), req.Worker, req.Attempt, req.Result)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *server) fail(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Worker  string `json:"worker"`
		Attempt int    `json:"attempt"`
	}
	if !decode(w, r, &req) {
		return
	}
	t, err := s.store.Fail(r.PathValue("id"), req.Worker, req.Attempt)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *server) cancel(w http.ResponseWriter, r *http.Request) {
	t, err := s.store.Cancel(r.PathValue("id"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *server) retry(w http.ResponseWriter, r *http.Request) {
	t, err := s.store.Retry(r.PathValue("id"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *server) sweep(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Now time.Time `json:"now"`
	}
	if !decode(w, r, &req) {
		return
	}
	now := req.Now
	if now.IsZero() {
		now = time.Now()
	}
	if err := s.store.Sweep(now); err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"swept": true})
}
