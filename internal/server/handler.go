// Package server exposes the cancellation tree over HTTP and embeds the
// controllable fake upstream in the same process under /upstream, so the
// whole system runs locally with no production dependencies.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"canceltree/internal/client"
	"canceltree/internal/tree"
)

// API envelope for process requests.
type processRequest struct {
	RequestID string    `json:"request_id"`
	Tasks     []taskDTO `json:"tasks"`
}

type taskDTO struct {
	ID        string      `json:"id"`
	Fatal     bool        `json:"fatal"`
	TimeoutMS int64       `json:"timeout_ms"`
	Call      callDTO     `json:"call"`
	Cleanup   *cleanupDTO `json:"cleanup,omitempty"`
}

type callDTO struct {
	Method   string    `json:"method"`
	URL      string    `json:"url"`
	Attempts int       `json:"attempts"`
	Fault    *faultDTO `json:"fault,omitempty"`
}

type faultDTO struct {
	Kind    string `json:"kind"`
	DelayMS int64  `json:"delay_ms"`
}

type cleanupDTO struct {
	URL       string `json:"url"`
	TimeoutMS int64  `json:"timeout_ms"`
}

func (s *Server) handleProcess(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "use POST")
		return
	}
	var req processRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if len(req.Tasks) == 0 {
		writeError(w, http.StatusBadRequest, "tasks must not be empty")
		return
	}

	// Relative task/cleanup URLs resolve against the request's own host,
	// so the same binary works on any port (and under httptest).
	requestBase := "http://" + r.Host
	tasks, err := s.buildTasks(requestBase, req.Tasks)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	reqID := req.RequestID
	if reqID == "" {
		reqID = "req-" + time.Now().UTC().Format("20060102T150405.000000000")
	}

	// r.Context() is canceled by net/http when the TCP client goes away,
	// which is exactly the "client disconnect terminates remaining work"
	// edge of the cancellation tree.
	report := s.engine.Run(r.Context(), reqID, tasks)

	status := http.StatusOK
	if report.Status == tree.TreeFailed {
		status = http.StatusServiceUnavailable
	}
	s.obs.record(report.Status)
	writeJSON(w, status, report)
	if report.Status == tree.TreeCanceled && r.Context().Err() != nil {
		// The response write above is expected to fail against a dead
		// connection; log for operators, never retry into the void.
		s.obs.ClientCanceled(report.RequestID)
	}
}

// buildTasks converts wire DTOs (millisecond durations) into engine specs.
func (s *Server) buildTasks(baseURL string, dtos []taskDTO) ([]tree.TaskSpec, error) {
	tasks := make([]tree.TaskSpec, 0, len(dtos))
	seen := make(map[string]struct{}, len(dtos))
	for _, d := range dtos {
		if d.ID == "" {
			return nil, errors.New("task id is required")
		}
		if _, dup := seen[d.ID]; dup {
			return nil, errors.New("duplicate task id: " + d.ID)
		}
		seen[d.ID] = struct{}{}

		call := client.Call{
			TaskID:   d.ID,
			Method:   d.Call.Method,
			Attempts: d.Call.Attempts,
		}
		target := d.Call.URL
		if target == "" {
			return nil, errors.New("task " + d.ID + ": call.url is required")
		}
		if d.Call.Fault != nil && d.Call.Fault.Kind == client.FaultReset {
			// A reset is realized by a real HTTP call to the fake's
			// connection-resetting endpoint.
			target = s.resetURL
		}
		abs, err := client.ResolveURL(baseURL, target)
		if err != nil {
			return nil, errors.New("task " + d.ID + ": " + err.Error())
		}
		call.URL = abs
		if d.Call.Fault != nil {
			call.Fault = client.Fault{
				Kind:  d.Call.Fault.Kind,
				Delay: ms(d.Call.Fault.DelayMS),
			}
		}

		spec := tree.TaskSpec{
			ID:      d.ID,
			Fatal:   d.Fatal,
			Call:    call,
			Timeout: ms(d.TimeoutMS),
		}
		if d.Cleanup != nil {
			cuAbs, err := client.ResolveURL(baseURL, d.Cleanup.URL)
			if err != nil {
				return nil, errors.New("task " + d.ID + " cleanup: " + err.Error())
			}
			spec.Cleanup = &tree.CleanupSpec{
				URL:     cuAbs,
				Timeout: ms(d.Cleanup.TimeoutMS),
			}
		}
		tasks = append(tasks, spec)
	}
	return tasks, nil
}

func (s *Server) handleShutdown(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "use POST")
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]string{"status": "shutting down"})
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = s.Shutdown(ctx)
	}()
}

func ms(v int64) time.Duration {
	if v <= 0 {
		return 0
	}
	return time.Duration(v) * time.Millisecond
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
