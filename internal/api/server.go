// Package api implements the JSON HTTP interface of the merging service.
package api

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/local/testmerge/internal/domain"
	"github.com/local/testmerge/internal/executor"
	"github.com/local/testmerge/internal/merge"
	"github.com/local/testmerge/internal/store"
)

// Server wires the store, reducer and executor to HTTP handlers.
type Server struct {
	Store    *store.Store
	Executor *executor.Executor
}

// NewRouter builds the mux (Go 1.22 method+pattern routing).
func (s *Server) NewRouter() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.health)
	mux.HandleFunc("POST /runs", s.createRun)
	mux.HandleFunc("GET /runs", s.listRuns)
	mux.HandleFunc("GET /runs/{runID}", s.getRun)
	mux.HandleFunc("GET /runs/{runID}/events", s.getEvents)
	mux.HandleFunc("POST /runs/{runID}/events", s.appendEvent)
	mux.HandleFunc("POST /runs/{runID}/replay", s.replay)
	mux.HandleFunc("POST /runs/{runID}/execute", s.execute)
	mux.HandleFunc("GET /runs/{runID}/executions", s.listExecutions)
	return mux
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
}

type createRunReq struct {
	RunID string   `json:"run_id,omitempty"`
	Tests []string `json:"tests,omitempty"`
}

func (s *Server) createRun(w http.ResponseWriter, r *http.Request) {
	var req createRunReq
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	runID := req.RunID
	if runID == "" {
		runID = "run-" + randHex(6)
	}
	if !store.ValidRunID(runID) {
		writeError(w, http.StatusBadRequest, fmt.Errorf("invalid run_id %q: use 1-128 chars of [A-Za-z0-9_-], starting alphanumeric", runID))
		return
	}
	if err := s.Store.CreateRun(runID); err != nil {
		if errors.Is(err, store.ErrExists) {
			writeError(w, http.StatusConflict, err)
			return
		}
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	ev := domain.Event{
		EventID: "evt-" + randHex(8),
		Type:    domain.EventRunStarted,
		RunID:   runID,
		At:      time.Now().UTC().Format(time.RFC3339Nano),
		Tests:   req.Tests,
	}
	if err := s.Store.AppendEvent(ev); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	summary, err := s.summarize(runID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"run_id": runID, "summary": summary})
}

func (s *Server) listRuns(w http.ResponseWriter, _ *http.Request) {
	ids, err := s.Store.ListRuns()
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	type item struct {
		RunID  string           `json:"run_id"`
		Status domain.RunStatus `json:"status"`
	}
	items := make([]item, 0, len(ids))
	for _, id := range ids {
		sum, err := s.summarize(id)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err)
			return
		}
		items = append(items, item{RunID: id, Status: sum.Status})
	}
	writeJSON(w, http.StatusOK, map[string]any{"runs": items})
}

func (s *Server) getRun(w http.ResponseWriter, r *http.Request) {
	runID := r.PathValue("runID")
	summary, err := s.summarize(runID)
	if err != nil {
		s.writeLoadErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, summary)
}

func (s *Server) getEvents(w http.ResponseWriter, r *http.Request) {
	runID := r.PathValue("runID")
	events, err := s.Store.LoadEvents(runID)
	if err != nil {
		s.writeLoadErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"run_id": runID, "events": events, "count": len(events)})
}

func (s *Server) appendEvent(w http.ResponseWriter, r *http.Request) {
	runID := r.PathValue("runID")
	var ev domain.Event
	if err := decodeBody(r, &ev); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if ev.Type == domain.EventRunStarted {
		writeError(w, http.StatusBadRequest, errors.New("run_started is emitted automatically at run creation"))
		return
	}
	ev.RunID = runID // server-authoritative
	status, result, err := s.ingest(ev)
	if err != nil {
		writeError(w, status, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

type replayReq struct {
	Events []domain.Event `json:"events"`
}

type replayResult struct {
	Received   int              `json:"received"`
	Accepted   int              `json:"accepted"`
	Duplicates int              `json:"duplicates"`
	Rejected   []rejectedEvent  `json:"rejected"`
	Summary    merge.RunSummary `json:"summary"`
}

type rejectedEvent struct {
	EventID string `json:"event_id,omitempty"`
	Line    int    `json:"line,omitempty"`
	Error   string `json:"error"`
}

// replay ingests a batch of events (e.g. re-streamed after an executor
// restart). Already-seen events are counted as duplicates, not errors, so
// the whole replay is safe to repeat.
func (s *Server) replay(w http.ResponseWriter, r *http.Request) {
	runID := r.PathValue("runID")
	var req replayReq
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	res := replayResult{Received: len(req.Events)}
	for i, ev := range req.Events {
		if ev.Type == domain.EventRunStarted {
			res.Rejected = append(res.Rejected, rejectedEvent{EventID: ev.EventID, Line: i + 1, Error: "run_started is emitted automatically"})
			continue
		}
		ev.RunID = runID
		_, _, err := s.ingest(ev)
		if err == nil {
			res.Accepted++
			continue
		}
		var dup *store.DuplicateEventError
		if errors.As(err, &dup) {
			res.Duplicates++
			continue
		}
		res.Rejected = append(res.Rejected, rejectedEvent{EventID: ev.EventID, Line: i + 1, Error: err.Error()})
	}
	summary, err := s.summarize(runID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	res.Summary = summary
	writeJSON(w, http.StatusOK, res)
}

type executeReq struct {
	// Command is argv; argv[0] is a path relative to the fixtures root.
	Command []string `json:"command"`
	Env     []string `json:"env,omitempty"` // KEY=VALUE overrides
	Timeout string   `json:"timeout,omitempty"`
	// ExecutionID lets callers make "run this fixture" idempotent across
	// retries of the HTTP call. Auto-generated when empty.
	ExecutionID string `json:"execution_id,omitempty"`
	// AutoFinish, when true, appends run_finished after a successful exit.
	AutoFinish bool `json:"auto_finish,omitempty"`
}

type executeResult struct {
	ExecutionID string           `json:"execution_id"`
	Record      *executor.Record `json:"record"`
	Ingested    int              `json:"ingested"`
	Duplicates  int              `json:"duplicates"`
	Rejected    []rejectedEvent  `json:"rejected"`
	Summary     merge.RunSummary `json:"summary"`
}

func (s *Server) execute(w http.ResponseWriter, r *http.Request) {
	runID := r.PathValue("runID")
	var req executeReq
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if len(req.Command) == 0 {
		writeError(w, http.StatusBadRequest, errors.New("command argv is required"))
		return
	}
	var timeout time.Duration
	if req.Timeout != "" {
		d, err := time.ParseDuration(req.Timeout)
		if err != nil {
			writeError(w, http.StatusBadRequest, fmt.Errorf("invalid timeout: %w", err))
			return
		}
		timeout = d
	}
	execID := req.ExecutionID
	if execID == "" {
		execID = "exec-" + randHex(6)
	}

	rec, err := s.Executor.Run(r.Context(), runID, execID, req.Command, req.Env, timeout)
	if err != nil {
		// Fixture resolution / launch errors are client errors.
		writeError(w, http.StatusBadRequest, err)
		return
	}
	result := executeResult{ExecutionID: execID, Record: rec}
	for _, ie := range rec.Events {
		ev := ie.Event
		if ev.RunID == "" {
			ev.RunID = runID
		}
		if ev.RunID != runID {
			result.Rejected = append(result.Rejected, rejectedEvent{EventID: ev.EventID, Line: ie.LineNo, Error: "event run_id does not match URL run id"})
			continue
		}
		if ev.Type == domain.EventRunStarted {
			result.Rejected = append(result.Rejected, rejectedEvent{EventID: ev.EventID, Line: ie.LineNo, Error: "run_started is emitted automatically"})
			continue
		}
		_, _, err := s.ingest(ev)
		if err == nil {
			result.Ingested++
			continue
		}
		var dup *store.DuplicateEventError
		if errors.As(err, &dup) {
			result.Duplicates++
			continue
		}
		result.Rejected = append(result.Rejected, rejectedEvent{EventID: ev.EventID, Line: ie.LineNo, Error: err.Error()})
	}

	if req.AutoFinish && rec.ExitCode == 0 && !rec.TimedOut {
		sum, _ := s.summarize(runID)
		cancelled := sum.Status == domain.RunCancelled
		// seq_no is chosen server-side (max+1) so it can never collide
		// with fixture-emitted sequence numbers.
		if _, err := s.Store.AppendRunFinished(
			runID, "evt-finish-"+execID, cancelled, sum.Status,
			"auto_finish after fixture exit 0",
		); err != nil {
			var dup *store.DuplicateEventError
			if !errors.As(err, &dup) {
				writeError(w, http.StatusInternalServerError, err)
				return
			}
		}
	}

	if err := s.Store.AppendExecution(runID, rec); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	summary, err := s.summarize(runID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	result.Summary = summary
	code := http.StatusOK
	writeJSON(w, code, result)
}

func (s *Server) listExecutions(w http.ResponseWriter, r *http.Request) {
	runID := r.PathValue("runID")
	recs, err := s.Store.LoadExecutions(runID)
	if err != nil {
		s.writeLoadErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"run_id": runID, "executions": recs, "count": len(recs)})
}

// ingest validates and appends one event, returning the HTTP status on error.
func (s *Server) ingest(ev domain.Event) (int, map[string]any, error) {
	if ev.EventID == "" {
		ev.EventID = "evt-" + randHex(8)
	}
	if err := merge.Validate(ev); err != nil {
		return http.StatusBadRequest, nil, err
	}
	if err := s.Store.AppendEvent(ev); err != nil {
		var dup *store.DuplicateEventError
		if errors.As(err, &dup) {
			return http.StatusConflict, nil, err
		}
		var col *store.SeqCollisionError
		if errors.As(err, &col) {
			return http.StatusConflict, nil, err
		}
		if errors.Is(err, store.ErrNotFound) {
			return http.StatusNotFound, nil, err
		}
		return http.StatusInternalServerError, nil, err
	}
	return http.StatusOK, map[string]any{"status": "accepted", "event_id": ev.EventID}, nil
}

func (s *Server) summarize(runID string) (merge.RunSummary, error) {
	var zero merge.RunSummary
	events, err := s.Store.LoadEvents(runID)
	if err != nil {
		return zero, err
	}
	run, _, err := merge.Reduce(runID, events)
	if err != nil {
		return zero, err
	}
	return run.Summarize(), nil
}

func (s *Server) writeLoadErr(w http.ResponseWriter, err error) {
	if errors.Is(err, store.ErrNotFound) {
		writeError(w, http.StatusNotFound, err)
		return
	}
	writeError(w, http.StatusInternalServerError, err)
}

// --- small HTTP helpers ---

func decodeBody(r *http.Request, v any) error {
	if r.Body == nil {
		return errors.New("empty request body")
	}
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return fmt.Errorf("invalid JSON: %w", err)
	}
	return nil
}

type errBody struct {
	Error string `json:"error"`
}

func writeError(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, errBody{Error: err.Error()})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func randHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
