package handlers

import (
	"net/http"

	"communitygov/internal/middleware"
	"communitygov/internal/services"
)

type tierReq struct {
	Level        int32  `json:"level"`
	Name         string `json:"name"`
	PriceCents   int64  `json:"price_cents"`
	DurationDays int32  `json:"duration_days"`
}

func (h *Handlers) CreateTier(w http.ResponseWriter, r *http.Request) {
	var req tierReq
	if err := decode(r, &req); err != nil {
		writeErr(w, errBadRequest(err.Error()))
		return
	}
	cid := urlInt64(r, "communityID")
	t, err := h.Memberships.CreateTier(r.Context(), cid, req.Level, req.Name,
		req.PriceCents, req.DurationDays)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, t)
}

func (h *Handlers) ListTiers(w http.ResponseWriter, r *http.Request) {
	cid := urlInt64(r, "communityID")
	ts, err := h.Memberships.ListTiers(r.Context(), cid)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, ts)
}

type paymentReq struct {
	RequestID   string `json:"request_id"`
	UserID      int64  `json:"user_id"`
	TierID      int64  `json:"tier_id"`
	AmountCents int64  `json:"amount_cents"`
	ExtendDays  int32  `json:"extend_days"`
}

// RecordPayment: admin-only offline payment registration. Reviewers are
// explicitly rejected even though they can moderate. Idempotency key is the
// X-Request-Id header (falling back to body request_id): same id + same body
// replays, same id + different body -> 409.
func (h *Handlers) RecordPayment(w http.ResponseWriter, r *http.Request) {
	var req paymentReq
	if err := decode(r, &req); err != nil {
		writeErr(w, errBadRequest(err.Error()))
		return
	}
	reqID := middleware.GetRequestID(r.Context())
	if req.RequestID != "" {
		reqID = req.RequestID
	}
	if reqID == "" {
		writeErr(w, errBadRequest("request id is required (X-Request-Id header or request_id)"))
		return
	}
	u := currentUser(r)
	cid := urlInt64(r, "communityID")
	// Hard separation of duties: a reviewer account never handles payments.
	// Global admins (the route is admin-only) may record payments even if they
	// were also listed as a reviewer; the admin role is the governing one.
	if u.Role != "admin" {
		writeErr(w, services.E(services.ErrReviewerRenewal, ""))
		return
	}
	pay, m, err := h.Memberships.RecordPayment(r.Context(), services.RecordPaymentParams{
		CommunityID: cid, RequestID: reqID, UserID: req.UserID, TierID: req.TierID,
		AmountCents: req.AmountCents, ExtendDays: req.ExtendDays, RecordedBy: u.ID,
	})
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"payment": pay, "membership": m})
}

func (h *Handlers) CancelMembership(w http.ResponseWriter, r *http.Request) {
	cid := urlInt64(r, "communityID")
	uid := urlInt64(r, "userID")
	m, err := h.Memberships.Cancel(r.Context(), cid, uid)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, m)
}

func (h *Handlers) GetMembership(w http.ResponseWriter, r *http.Request) {
	cid := urlInt64(r, "communityID")
	uid := urlInt64(r, "userID")
	m, err := h.Memberships.Get(r.Context(), cid, uid)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, m)
}
