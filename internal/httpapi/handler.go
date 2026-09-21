package httpapi

import (
	"errors"
	"net/http"
	"strconv"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"communityvault/internal/auth"
	"communityvault/internal/httpx"
	"communityvault/internal/service"
)

type Handler struct {
	svc *service.Service
}

func New(pool *pgxpool.Pool, claimTTLSeconds float64) http.Handler {
	h := &Handler{svc: service.New(pool, durFromSeconds(claimTTLSeconds))}

	r := chi.NewRouter()
	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		httpx.JSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	r.Route("/v1", func(r chi.Router) {
		r.Use(auth.Middleware(pool))

		// content
		r.Post("/contents", h.createContent)
		r.Get("/contents", h.feed)
		r.Get("/contents/{id}", h.getContent)
		r.Post("/contents/{id}/edits", h.editContent)
		r.Post("/contents/{id}/submit", h.submitContent)
		r.Post("/contents/{id}/rollback", h.rollback)
		r.Post("/contents/{id}/withdraw", h.withdraw)
		r.Get("/contents/{id}/revisions", h.listRevisions)
		r.Get("/contents/{id}/events", h.listEvents)
		r.Get("/contents/{id}/decisions", h.listDecisions)

		// reports
		r.Post("/contents/{id}/reports", h.createReport)
		r.Get("/contents/{id}/reports", h.listReports)
		r.Post("/reports/{id}/resolve", h.resolveReport)

		// moderation queue
		r.Get("/mod/queue", h.listQueue)
		r.Post("/mod/claims", h.claim)
		r.Post("/mod/tasks/{id}/approve", h.approve)
		r.Post("/mod/tasks/{id}/reject", h.reject)

		// rule versions
		r.Get("/rules", h.listRules)
		r.Post("/rules", h.createRule)
		r.Get("/rules/active", h.activeRule)
	})

	return r
}

// ---------- helpers ----------

func actor(r *http.Request) *auth.Principal { return auth.FromContext(r.Context()) }

func idParam(r *http.Request, key string) (int64, bool) {
	v := chi.URLParam(r, key)
	id, err := strconv.ParseInt(v, 10, 64)
	if err != nil || id <= 0 {
		return 0, false
	}
	return id, true
}

// writeError maps domain errors to HTTP status codes.
func writeError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, service.ErrNotFound):
		httpx.Err(w, http.StatusNotFound, err.Error())
	case errors.Is(err, service.ErrForbidden):
		httpx.Err(w, http.StatusForbidden, err.Error())
	case errors.Is(err, service.ErrValidation):
		httpx.Err(w, http.StatusBadRequest, err.Error())
	case errors.Is(err, service.ErrConflict):
		httpx.Err(w, http.StatusConflict, err.Error())
	case errors.Is(err, service.ErrNoTask):
		httpx.Err(w, http.StatusNoContent, err.Error())
	case errors.Is(err, service.ErrStaleClaim):
		httpx.Err(w, http.StatusConflict, err.Error())
	case errors.Is(err, service.ErrRuleViolation):
		httpx.Err(w, http.StatusUnprocessableEntity, err.Error())
	case errors.Is(err, service.ErrAlreadyReported):
		httpx.Err(w, http.StatusConflict, err.Error())
	default:
		httpx.Err(w, http.StatusInternalServerError, "internal error")
	}
}

func requireMod(w http.ResponseWriter, p *auth.Principal) bool {
	if p.IsAdmin() || p.IsModerator() {
		return true
	}
	httpx.Err(w, http.StatusForbidden, "moderator role required")
	return false
}

func requireAdmin(w http.ResponseWriter, p *auth.Principal) bool {
	if p.IsAdmin() {
		return true
	}
	httpx.Err(w, http.StatusForbidden, "admin role required")
	return false
}

// ---------- content handlers ----------

func (h *Handler) createContent(w http.ResponseWriter, r *http.Request) {
	var in service.CreateContentInput
	if err := httpx.Decode(r, &in); err != nil {
		httpx.Err(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	out, err := h.svc.Create(r.Context(), actor(r), in)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusCreated, out)
}

func (h *Handler) feed(w http.ResponseWriter, r *http.Request) {
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	cursor := r.URL.Query().Get("cursor")
	page, err := h.svc.Feed(r.Context(), actor(r), int32(limit), cursor)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, page)
}

func (h *Handler) getContent(w http.ResponseWriter, r *http.Request) {
	id, ok := idParam(r, "id")
	if !ok {
		httpx.Err(w, http.StatusBadRequest, "invalid id")
		return
	}
	out, err := h.svc.GetContent(r.Context(), actor(r), id)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}

func (h *Handler) editContent(w http.ResponseWriter, r *http.Request) {
	id, ok := idParam(r, "id")
	if !ok {
		httpx.Err(w, http.StatusBadRequest, "invalid id")
		return
	}
	var in service.EditContentInput
	if err := httpx.Decode(r, &in); err != nil {
		httpx.Err(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	out, err := h.svc.Edit(r.Context(), actor(r), id, in)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}

func (h *Handler) submitContent(w http.ResponseWriter, r *http.Request) {
	id, ok := idParam(r, "id")
	if !ok {
		httpx.Err(w, http.StatusBadRequest, "invalid id")
		return
	}
	out, err := h.svc.Submit(r.Context(), actor(r), id)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}

func (h *Handler) rollback(w http.ResponseWriter, r *http.Request) {
	id, ok := idParam(r, "id")
	if !ok {
		httpx.Err(w, http.StatusBadRequest, "invalid id")
		return
	}
	var body struct {
		RevisionID int64  `json:"revision_id"`
		Reason     string `json:"reason"`
	}
	if err := httpx.Decode(r, &body); err != nil || body.RevisionID <= 0 {
		httpx.Err(w, http.StatusBadRequest, "revision_id and reason are required")
		return
	}
	out, err := h.svc.Rollback(r.Context(), actor(r), id, body.RevisionID, body.Reason)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}

func (h *Handler) withdraw(w http.ResponseWriter, r *http.Request) {
	id, ok := idParam(r, "id")
	if !ok {
		httpx.Err(w, http.StatusBadRequest, "invalid id")
		return
	}
	var body service.ReasonInput
	if err := httpx.Decode(r, &body); err != nil {
		httpx.Err(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	out, err := h.svc.Withdraw(r.Context(), actor(r), id, body.Reason)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}

func (h *Handler) listRevisions(w http.ResponseWriter, r *http.Request) {
	id, ok := idParam(r, "id")
	if !ok {
		httpx.Err(w, http.StatusBadRequest, "invalid id")
		return
	}
	out, err := h.svc.ListRevisions(r.Context(), actor(r), id)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}

func (h *Handler) listEvents(w http.ResponseWriter, r *http.Request) {
	id, ok := idParam(r, "id")
	if !ok {
		httpx.Err(w, http.StatusBadRequest, "invalid id")
		return
	}
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	out, err := h.svc.Events(r.Context(), actor(r), id, int32(limit))
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}

func (h *Handler) listDecisions(w http.ResponseWriter, r *http.Request) {
	id, ok := idParam(r, "id")
	if !ok {
		httpx.Err(w, http.StatusBadRequest, "invalid id")
		return
	}
	out, err := h.svc.Decisions(r.Context(), actor(r), id)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}

// ---------- report handlers ----------

func (h *Handler) createReport(w http.ResponseWriter, r *http.Request) {
	id, ok := idParam(r, "id")
	if !ok {
		httpx.Err(w, http.StatusBadRequest, "invalid id")
		return
	}
	var body service.ReasonInput
	if err := httpx.Decode(r, &body); err != nil {
		httpx.Err(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	out, err := h.svc.Report(r.Context(), actor(r), id, body.Reason)
	if err != nil {
		writeError(w, err)
		return
	}
	status := http.StatusCreated
	if !out.Created {
		status = http.StatusOK
	}
	httpx.JSON(w, status, out)
}

func (h *Handler) listReports(w http.ResponseWriter, r *http.Request) {
	id, ok := idParam(r, "id")
	if !ok {
		httpx.Err(w, http.StatusBadRequest, "invalid id")
		return
	}
	out, err := h.svc.ListReports(r.Context(), actor(r), id)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}

func (h *Handler) resolveReport(w http.ResponseWriter, r *http.Request) {
	id, ok := idParam(r, "id")
	if !ok {
		httpx.Err(w, http.StatusBadRequest, "invalid id")
		return
	}
	if err := h.svc.ResolveReport(r.Context(), actor(r), id); err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]string{"status": "resolved"})
}

// ---------- moderation handlers ----------

func (h *Handler) listQueue(w http.ResponseWriter, r *http.Request) {
	p := actor(r)
	if !requireMod(w, p) {
		return
	}
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	out, err := h.svc.ListQueue(r.Context(), p, int32(limit))
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}

func (h *Handler) claim(w http.ResponseWriter, r *http.Request) {
	p := actor(r)
	if !requireMod(w, p) {
		return
	}
	out, err := h.svc.Claim(r.Context(), p)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}

func (h *Handler) approve(w http.ResponseWriter, r *http.Request) {
	p := actor(r)
	if !requireMod(w, p) {
		return
	}
	taskID, ok := idParam(r, "id")
	if !ok {
		httpx.Err(w, http.StatusBadRequest, "invalid task id")
		return
	}
	var body service.ReasonInput
	_ = httpx.Decode(r, &body) // reason optional for approval
	out, err := h.svc.Approve(r.Context(), p, taskID, body.Reason)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}

func (h *Handler) reject(w http.ResponseWriter, r *http.Request) {
	p := actor(r)
	if !requireMod(w, p) {
		return
	}
	taskID, ok := idParam(r, "id")
	if !ok {
		httpx.Err(w, http.StatusBadRequest, "invalid task id")
		return
	}
	var body service.ReasonInput
	if err := httpx.Decode(r, &body); err != nil {
		httpx.Err(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	out, err := h.svc.Reject(r.Context(), p, taskID, body.Reason)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}

// ---------- rule handlers ----------

func (h *Handler) listRules(w http.ResponseWriter, r *http.Request) {
	p := actor(r)
	if !requireAdmin(w, p) {
		return
	}
	out, err := h.svc.ListRules(r.Context(), p)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}

func (h *Handler) createRule(w http.ResponseWriter, r *http.Request) {
	p := actor(r)
	if !requireAdmin(w, p) {
		return
	}
	var in service.CreateRuleVersionInput
	if err := httpx.Decode(r, &in); err != nil {
		httpx.Err(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	out, err := h.svc.CreateRuleVersion(r.Context(), p, in)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusCreated, out)
}

func (h *Handler) activeRule(w http.ResponseWriter, r *http.Request) {
	p := actor(r)
	if !requireMod(w, p) {
		return
	}
	out, err := h.svc.ActiveRule(r.Context(), p)
	if err != nil {
		writeError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}
