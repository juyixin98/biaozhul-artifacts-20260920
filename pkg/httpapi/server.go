// Package httpapi exposes the DRF scheduler over a small local HTTP API.
package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"drfscheduler/pkg/scheduler"
)

// Server wires the scheduler to HTTP handlers.
type Server struct {
	Sched *scheduler.Scheduler
	Fake  *scheduler.FakeClock // non-nil only in simulation mode
	Mux   *http.ServeMux
}

// NewServer builds the router. Go 1.22 method+pattern routing is used.
func NewServer(s *scheduler.Scheduler, fake *scheduler.FakeClock) *Server {
	srv := &Server{Sched: s, Fake: fake, Mux: http.NewServeMux()}
	m := srv.Mux

	m.HandleFunc("GET /healthz", srv.health)
	m.HandleFunc("GET /v1/state", srv.getState)
	m.HandleFunc("PUT /v1/cluster/capacity", srv.putCapacity)
	m.HandleFunc("PUT /v1/tenants/{id}", srv.putTenant)
	m.HandleFunc("GET /v1/tenants", srv.listTenants)
	m.HandleFunc("POST /v1/tasks", srv.postTask)
	m.HandleFunc("POST /v1/tasks/batch", srv.postTaskBatch)
	m.HandleFunc("GET /v1/tasks", srv.listTasks)
	m.HandleFunc("GET /v1/tasks/{id}", srv.getTask)
	m.HandleFunc("POST /v1/tasks/{id}/cancel", srv.cancelTask)
	m.HandleFunc("GET /v1/events", srv.getEvents)
	m.HandleFunc("POST /v1/schedule", srv.triggerSchedule)
	m.HandleFunc("POST /v1/clock/advance", srv.advanceClock)
	return srv
}

type errBody struct {
	Error string `json:"error"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, errBody{Error: err.Error()})
}

// statusFor maps sentinel scheduler errors to HTTP status codes.
func statusFor(err error) int {
	switch {
	case errors.Is(err, scheduler.ErrCapacity):
		return http.StatusUnprocessableEntity // 422: never runnable / can't shrink
	case errors.Is(err, scheduler.ErrUnknownTenant), errors.Is(err, scheduler.ErrUnknownTask):
		return http.StatusNotFound
	case errors.Is(err, scheduler.ErrFakeClockOnly):
		return http.StatusBadRequest
	case errors.Is(err, scheduler.ErrClosed):
		return http.StatusServiceUnavailable
	default:
		return http.StatusBadRequest
	}
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(v); err != nil {
		writeJSON(w, http.StatusBadRequest, errBody{Error: "invalid JSON body: " + err.Error()})
		return false
	}
	return true
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "mode": s.mode()})
}

func (s *Server) mode() string {
	if s.Fake != nil {
		return "simulation"
	}
	return "realtime"
}

type capacityReq struct {
	CPUMillicpu int64 `json:"cpu_millicpu"`
	MemoryMiB   int64 `json:"memory_mib"`
}

func (s *Server) putCapacity(w http.ResponseWriter, r *http.Request) {
	var req capacityReq
	if !decode(w, r, &req) {
		return
	}
	err := s.Sched.SetCapacity(scheduler.Resources{CPU: req.CPUMillicpu, Memory: req.MemoryMiB})
	if err != nil {
		writeErr(w, statusFor(err), err)
		return
	}
	writeJSON(w, http.StatusOK, s.Sched.Snapshot())
}

type tenantReq struct {
	Weight int64 `json:"weight"`
}

func (s *Server) putTenant(w http.ResponseWriter, r *http.Request) {
	var req tenantReq
	if !decode(w, r, &req) {
		return
	}
	id := r.PathValue("id")
	if err := s.Sched.AddTenant(id, req.Weight); err != nil {
		writeErr(w, statusFor(err), err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"id": id, "weight": req.Weight})
}

func (s *Server) listTenants(w http.ResponseWriter, r *http.Request) {
	snap := s.Sched.Snapshot()
	writeJSON(w, http.StatusOK, map[string]any{"tenants": snap.Tenants})
}

type taskReq struct {
	TenantID    string `json:"tenant_id"`
	CPUMillicpu int64  `json:"cpu_millicpu"`
	MemoryMiB   int64  `json:"memory_mib"`
	DurationMS  int64  `json:"duration_ms"` // simulation mode
	Spec        string `json:"spec"`        // process mode: shell command
	ID          string `json:"id"`
}

func (s *Server) postTask(w http.ResponseWriter, r *http.Request) {
	var req taskReq
	if !decode(w, r, &req) {
		return
	}
	t, err := s.Sched.Submit(scheduler.SubmitRequest{
		TenantID: req.TenantID,
		CPU:      req.CPUMillicpu,
		Memory:   req.MemoryMiB,
		Duration: time.Duration(req.DurationMS) * time.Millisecond,
		Spec:     req.Spec,
		ID:       req.ID,
	})
	if err != nil {
		writeErr(w, statusFor(err), err)
		return
	}
	// Let the asynchronous scheduler react before responding, so the returned
	// state reflects whether the task started immediately or had to wait.
	_ = s.Sched.WaitIdleOrQuiet(500 * time.Millisecond)
	view, _ := s.Sched.GetTask(t.ID)
	writeJSON(w, http.StatusAccepted, view)
}

func (s *Server) postTaskBatch(w http.ResponseWriter, r *http.Request) {
	var reqs []taskReq
	if !decode(w, r, &reqs) {
		return
	}
	sreqs := make([]scheduler.SubmitRequest, len(reqs))
	for i, q := range reqs {
		sreqs[i] = scheduler.SubmitRequest{
			TenantID: q.TenantID,
			CPU:      q.CPUMillicpu,
			Memory:   q.MemoryMiB,
			Duration: time.Duration(q.DurationMS) * time.Millisecond,
			Spec:     q.Spec,
			ID:       q.ID,
		}
	}
	tasks, err := s.Sched.SubmitBatch(sreqs)
	if err != nil {
		writeErr(w, statusFor(err), err)
		return
	}
	_ = s.Sched.WaitIdleOrQuiet(500 * time.Millisecond)
	views := make([]scheduler.TaskView, len(tasks))
	for i, t := range tasks {
		views[i], _ = s.Sched.GetTask(t.ID)
	}
	writeJSON(w, http.StatusAccepted, map[string]any{"tasks": views, "count": len(views)})
}

func (s *Server) listTasks(w http.ResponseWriter, r *http.Request) {
	snap := s.Sched.Snapshot()
	tenant := r.URL.Query().Get("tenant_id")
	state := r.URL.Query().Get("state")
	out := snap.Tasks
	if tenant != "" || state != "" {
		filtered := make([]scheduler.TaskView, 0, len(out))
		for _, t := range out {
			if tenant != "" && t.TenantID != tenant {
				continue
			}
			if state != "" && string(t.State) != state {
				continue
			}
			filtered = append(filtered, t)
		}
		out = filtered
	}
	writeJSON(w, http.StatusOK, map[string]any{"tasks": out})
}

func (s *Server) getTask(w http.ResponseWriter, r *http.Request) {
	view, err := s.Sched.GetTask(r.PathValue("id"))
	if err != nil {
		writeErr(w, statusFor(err), err)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

type cancelReq struct {
	Reason string `json:"reason"`
}

func (s *Server) cancelTask(w http.ResponseWriter, r *http.Request) {
	var req cancelReq
	if r.ContentLength > 0 {
		if !decode(w, r, &req) {
			return
		}
	}
	if err := s.Sched.CancelTask(r.PathValue("id"), req.Reason); err != nil {
		writeErr(w, statusFor(err), err)
		return
	}
	view, _ := s.Sched.GetTask(r.PathValue("id"))
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) getEvents(w http.ResponseWriter, r *http.Request) {
	limit := 200
	writeJSON(w, http.StatusOK, map[string]any{"events": s.Sched.Events(limit)})
}

func (s *Server) triggerSchedule(w http.ResponseWriter, r *http.Request) {
	s.Sched.Kick()
	writeJSON(w, http.StatusOK, s.Sched.Snapshot())
}

type advanceReq struct {
	AdvanceMS int64 `json:"advance_ms"`
}

func (s *Server) advanceClock(w http.ResponseWriter, r *http.Request) {
	if s.Fake == nil {
		writeErr(w, http.StatusBadRequest, scheduler.ErrFakeClockOnly)
		return
	}
	var req advanceReq
	if !decode(w, r, &req) {
		return
	}
	if req.AdvanceMS <= 0 {
		writeErr(w, http.StatusBadRequest, errors.New("advance_ms must be positive"))
		return
	}
	s.Fake.Advance(time.Duration(req.AdvanceMS) * time.Millisecond)
	// Give the scheduler goroutine a chance to process all completions and
	// reach quiescence before the response shows state.
	_ = s.Sched.WaitIdleOrQuiet(500 * time.Millisecond)
	writeJSON(w, http.StatusOK, s.Sched.Snapshot())
}

func (s *Server) getState(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.Sched.Snapshot())
}
