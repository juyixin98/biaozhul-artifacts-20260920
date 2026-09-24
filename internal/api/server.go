// Package api exposes the allocation service over HTTP with chi.
package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"

	"github.com/go-chi/chi/v5"

	"deadlockcheck/internal/core"
)

// Server holds HTTP dependencies.
type Server struct {
	svc *core.Service
}

func NewServer(svc *core.Service) *Server {
	return &Server{svc: svc}
}

// Router builds the application router.
func (s *Server) Router() http.Handler {
	r := chi.NewRouter()
	r.Use(recoverer)

	r.Get("/healthz", s.health)

	r.Route("/api/v1", func(r chi.Router) {
		r.Post("/resources", s.registerResources)
		r.Get("/resources", s.listResources)

		r.Post("/tasks", s.createTask)
		r.Get("/tasks", s.listTasks)
		r.Get("/tasks/{id}", s.getTask)
		r.Post("/tasks/{id}/requests", s.submitExtra)
		r.Post("/tasks/{id}/complete", s.completeTask)
		r.Post("/tasks/{id}/fail", s.failTask)
		r.Post("/tasks/{id}/revoke", s.revoke)
		r.Post("/tasks/{id}/heartbeat", s.heartbeat)
		r.Get("/tasks/{id}/events", s.listEvents)

		r.Get("/holds", s.listHolds)
		r.Get("/evidence", s.listEvidence)

		r.Post("/admin/sweep", s.sweep)
		r.Post("/admin/grant-wave", s.grantWave)
		r.Get("/debug/waits", s.waitGraph)
	})
	return r
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// ---------------------------------------------------------------------------
// handlers
// ---------------------------------------------------------------------------

type registerReq struct {
	Resources []struct {
		Kind        string `json:"kind"`
		Name        string `json:"name"`
		Description string `json:"description"`
	} `json:"resources"`
}

func (s *Server) registerResources(w http.ResponseWriter, r *http.Request) {
	var in registerReq
	if err := decodeBody(r, &in); err != nil {
		writeError(w, err)
		return
	}
	if len(in.Resources) == 0 {
		writeError(w, core.Errorf("validation_error", "resources must contain at least one entry"))
		return
	}
	refs := make([]core.Ref, 0, len(in.Resources))
	descs := map[core.Ref]string{}
	for _, x := range in.Resources {
		ref := core.Ref{Kind: x.Kind, Name: x.Name}
		refs = append(refs, ref)
		descs[ref] = x.Description
	}
	if err := s.svc.RegisterResources(r.Context(), refs, descs); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"registered": refs})
}

func (s *Server) listResources(w http.ResponseWriter, r *http.Request) {
	v, err := s.svc.ListResources(r.Context())
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

type createTaskReq struct {
	Label       string     `json:"label"`
	Priority    int        `json:"priority"`
	AgingPerSec *float64   `json:"aging_per_sec"`
	TimeoutMS   int64      `json:"timeout_ms"`
	Resources   []core.Ref `json:"resources"`
}

func (s *Server) createTask(w http.ResponseWriter, r *http.Request) {
	var in createTaskReq
	if err := decodeBody(r, &in); err != nil {
		writeError(w, err)
		return
	}
	cin := core.CreateTaskInput{
		Label:       in.Label,
		Priority:    in.Priority,
		AgingPerSec: -1,
		TimeoutMS:   in.TimeoutMS,
		Resources:   in.Resources,
	}
	if in.AgingPerSec != nil {
		cin.AgingPerSec = *in.AgingPerSec
	}
	res, err := s.svc.CreateTask(r.Context(), cin)
	if err != nil {
		writeError(w, err)
		return
	}
	status := http.StatusCreated
	if !res.Granted {
		status = http.StatusAccepted
	}
	writeJSON(w, status, res)
}

func (s *Server) submitExtra(w http.ResponseWriter, r *http.Request) {
	id, err := idParam(r)
	if err != nil {
		writeError(w, err)
		return
	}
	var in core.ExtraRequestInput
	if err := decodeBody(r, &in); err != nil {
		writeError(w, err)
		return
	}
	res, err := s.svc.SubmitExtra(r.Context(), id, in)
	if err != nil {
		writeError(w, err)
		return
	}
	status := http.StatusOK
	if !res.Granted {
		status = http.StatusAccepted
	}
	writeJSON(w, status, res)
}

func (s *Server) completeTask(w http.ResponseWriter, r *http.Request) {
	id, err := idParam(r)
	if err != nil {
		writeError(w, err)
		return
	}
	v, err := s.svc.Complete(r.Context(), id)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

type failReq struct {
	Reason string `json:"reason"`
}

func (s *Server) failTask(w http.ResponseWriter, r *http.Request) {
	id, err := idParam(r)
	if err != nil {
		writeError(w, err)
		return
	}
	in := failReq{}
	_ = decodeBody(r, &in) // body optional
	v, err := s.svc.Fail(r.Context(), id, in.Reason)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

func (s *Server) revoke(w http.ResponseWriter, r *http.Request) {
	id, err := idParam(r)
	if err != nil {
		writeError(w, err)
		return
	}
	var in core.RevokeInput
	if err := decodeBody(r, &in); err != nil {
		writeError(w, err)
		return
	}
	v, err := s.svc.Revoke(r.Context(), id, in)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

func (s *Server) heartbeat(w http.ResponseWriter, r *http.Request) {
	id, err := idParam(r)
	if err != nil {
		writeError(w, err)
		return
	}
	var in core.HeartbeatInput
	_ = decodeBody(r, &in) // body optional
	v, err := s.svc.Heartbeat(r.Context(), id, in)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

func (s *Server) getTask(w http.ResponseWriter, r *http.Request) {
	id, err := idParam(r)
	if err != nil {
		writeError(w, err)
		return
	}
	v, err := s.svc.GetTask(r.Context(), id)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

func (s *Server) listTasks(w http.ResponseWriter, r *http.Request) {
	v, err := s.svc.ListTasks(r.Context(), queryLimit(r, 100))
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

func (s *Server) listEvents(w http.ResponseWriter, r *http.Request) {
	id, err := idParam(r)
	if err != nil {
		writeError(w, err)
		return
	}
	v, err := s.svc.ListEvents(r.Context(), id)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

func (s *Server) listHolds(w http.ResponseWriter, r *http.Request) {
	v, err := s.svc.ListHolds(r.Context())
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

func (s *Server) listEvidence(w http.ResponseWriter, r *http.Request) {
	var taskID *int64
	if raw := r.URL.Query().Get("task_id"); raw != "" {
		id, err := strconv.ParseInt(raw, 10, 64)
		if err != nil {
			writeError(w, core.Errorf("validation_error", "task_id must be an integer"))
			return
		}
		taskID = &id
	}
	v, err := s.svc.ListEvidence(r.Context(), taskID, queryLimit(r, 200))
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

func (s *Server) sweep(w http.ResponseWriter, r *http.Request) {
	ids, err := s.svc.SweepOnce(r.Context())
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"transitioned_to_uncertain": ids})
}

func (s *Server) grantWave(w http.ResponseWriter, r *http.Request) {
	if err := s.svc.GrantWave(r.Context()); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "grant wave executed"})
}

func (s *Server) waitGraph(w http.ResponseWriter, r *http.Request) {
	g, err := s.svc.WaitGraph(r.Context())
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, g)
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func idParam(r *http.Request) (int64, error) {
	raw := chi.URLParam(r, "id")
	id, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || id <= 0 {
		return 0, core.Errorf("validation_error", "id must be a positive integer")
	}
	return id, nil
}

func queryLimit(r *http.Request, def int) int {
	raw := r.URL.Query().Get("limit")
	if raw == "" {
		return def
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n <= 0 {
		return def
	}
	return n
}

type bodyError struct{ kind, msg string }

func (e *bodyError) Error() string { return e.kind + ": " + e.msg }

func decodeBody(r *http.Request, dst any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return &bodyError{kind: "validation_error", msg: "invalid JSON body: " + err.Error()}
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, err error) {
	var ce *core.Error
	if errors.As(err, &ce) {
		writeJSON(w, statusFor(ce.Kind), map[string]any{
			"error":   ce.Kind,
			"message": ce.Message,
			"details": ce.Details,
		})
		return
	}
	var be *bodyError
	if errors.As(err, &be) {
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"error":   be.kind,
			"message": be.msg,
		})
		return
	}
	writeJSON(w, http.StatusInternalServerError, map[string]any{
		"error":   "internal_error",
		"message": err.Error(),
	})
}

func statusFor(k core.ErrKind) int {
	switch k {
	case core.ErrValidation:
		return http.StatusBadRequest
	case core.ErrNotFound:
		return http.StatusNotFound
	case core.ErrConflict, core.ErrCycle:
		return http.StatusConflict
	case core.ErrState, core.ErrNotHeld:
		return http.StatusUnprocessableEntity
	default:
		return http.StatusInternalServerError
	}
}

func recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				writeJSON(w, http.StatusInternalServerError, map[string]any{
					"error":   "internal_error",
					"message": "panic recovered",
				})
			}
		}()
		next.ServeHTTP(w, r)
	})
}
