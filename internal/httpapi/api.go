// Package httpapi exposes the allocation service over a small JSON HTTP API
// using chi. All effects happen in the service layer's DB transactions.
package httpapi

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"

	"github.com/go-chi/chi/v5"

	"deadlockcheck/internal/service"
)

type API struct {
	svc *service.Service
}

func New(svc *service.Service) http.Handler {
	a := &API{svc: svc}
	r := chi.NewRouter()
	r.Use(recoverer)

	r.Get("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]string{"status": "ok"})
	})
	r.Get("/v1/verify-key", a.verifyKey)

	r.Route("/v1/resources", func(r chi.Router) {
		r.Get("/", a.listResources)
		r.Post("/", a.registerResource)
	})

	r.Route("/v1/tasks", func(r chi.Router) {
		r.Get("/", a.listTasks)
		r.Post("/", a.createTask)
		r.Get("/{taskID}", a.getTask)
		r.Get("/{taskID}/waits", a.getWaits)
		r.Get("/{taskID}/events", a.getEvents)
		r.Post("/{taskID}/resources", a.addResources)
		r.Post("/{taskID}/heartbeat", a.heartbeat)
		r.Post("/{taskID}/complete", a.complete)
		r.Post("/{taskID}/revoke", a.revoke)
		r.Post("/{taskID}/confirm-stop", a.confirmStop)
	})

	r.Get("/v1/graph", a.graph)
	r.Post("/v1/sweep-timeouts", a.sweep)
	r.Post("/v1/verify-token", a.verifyToken)

	return r
}

func recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				log.Printf("panic: %v", rec)
				writeJSON(w, 500, map[string]string{"error": "internal error"})
			}
		}()
		next.ServeHTTP(w, r)
	})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, err error) {
	var rej *service.ErrRejected
	if errors.As(err, &rej) {
		writeJSON(w, http.StatusConflict, map[string]string{"error": rej.Error()})
		return
	}
	log.Printf("error: %v", err)
	writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	if r.Body == nil {
		writeJSON(w, 400, map[string]string{"error": "empty body"})
		return false
	}
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		writeJSON(w, 400, map[string]string{"error": "invalid JSON: " + err.Error()})
		return false
	}
	return true
}

func (a *API) verifyKey(w http.ResponseWriter, r *http.Request) {
	pub := a.svc.PublicKey()
	writeJSON(w, 200, map[string]any{
		"alg":       "EdDSA",
		"curve":     "Ed25519",
		"publicKey": encodeB64(pub),
	})
}

func (a *API) listResources(w http.ResponseWriter, r *http.Request) {
	rs, err := a.svc.ListResources(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"resources": rs})
}

func (a *API) registerResource(w http.ResponseWriter, r *http.Request) {
	var req service.RegisterResourceReq
	if !decode(w, r, &req) {
		return
	}
	res, err := a.svc.RegisterResource(r.Context(), req)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 201, res)
}

func (a *API) listTasks(w http.ResponseWriter, r *http.Request) {
	ts, err := a.svc.ListTasks(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"tasks": ts})
}

func (a *API) createTask(w http.ResponseWriter, r *http.Request) {
	var req service.CreateTaskReq
	if !decode(w, r, &req) {
		return
	}
	res, err := a.svc.CreateTask(r.Context(), req)
	if err != nil {
		writeErr(w, err)
		return
	}
	code := 201
	if res.Status == "rejected" {
		code = 409
	}
	writeJSON(w, code, res)
}

func (a *API) getTask(w http.ResponseWriter, r *http.Request) {
	d, err := a.svc.TaskDetailFor(r.Context(), chi.URLParam(r, "taskID"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, d)
}

func (a *API) getWaits(w http.ResponseWriter, r *http.Request) {
	wr, err := a.svc.WaitReasons(r.Context(), chi.URLParam(r, "taskID"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"taskId": chi.URLParam(r, "taskID"), "waitReasons": wr})
}

func (a *API) getEvents(w http.ResponseWriter, r *http.Request) {
	ev, err := a.svc.Events(r.Context(), chi.URLParam(r, "taskID"), 100)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"events": ev})
}

func (a *API) addResources(w http.ResponseWriter, r *http.Request) {
	var req service.AddResourcesReq
	if !decode(w, r, &req) {
		return
	}
	req.TaskID = chi.URLParam(r, "taskID")
	res, err := a.svc.AddResources(r.Context(), req)
	if err != nil {
		writeErr(w, err)
		return
	}
	code := 200
	if res.Status == "rejected" {
		code = 409
	}
	writeJSON(w, code, res)
}

func (a *API) heartbeat(w http.ResponseWriter, r *http.Request) {
	var req service.FenceReq
	_ = json.NewDecoder(r.Body).Decode(&req)
	t, err := a.svc.Heartbeat(r.Context(), chi.URLParam(r, "taskID"), req.FenceEpoch)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, t)
}

func (a *API) complete(w http.ResponseWriter, r *http.Request) {
	var req service.FenceReq
	_ = json.NewDecoder(r.Body).Decode(&req)
	d, err := a.svc.Complete(r.Context(), chi.URLParam(r, "taskID"), req.FenceEpoch)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, d)
}

func (a *API) revoke(w http.ResponseWriter, r *http.Request) {
	d, err := a.svc.Revoke(r.Context(), chi.URLParam(r, "taskID"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, d)
}

func (a *API) confirmStop(w http.ResponseWriter, r *http.Request) {
	d, err := a.svc.ConfirmStop(r.Context(), chi.URLParam(r, "taskID"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, d)
}

func (a *API) graph(w http.ResponseWriter, r *http.Request) {
	g, err := a.svc.Graph(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, g)
}

func (a *API) sweep(w http.ResponseWriter, r *http.Request) {
	ids, err := a.svc.SweepTimeouts(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"timedOut": ids, "count": len(ids)})
}

type tokenReq struct {
	Token string `json:"token"`
}

func (a *API) verifyToken(w http.ResponseWriter, r *http.Request) {
	var req tokenReq
	if !decode(w, r, &req) {
		return
	}
	p, err := a.svc.VerifyToken(req.Token)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, p)
}
