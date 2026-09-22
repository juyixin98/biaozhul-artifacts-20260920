package handlers

import (
	"net/http"
	"time"

	"communitygov/internal/middleware"
)

type registerReq struct {
	Email     string `json:"email"`
	Name      string `json:"name"`
	Password  string `json:"password"`
	Role      string `json:"role"`
	Community string `json:"community"`
}

func (h *Handlers) Register(w http.ResponseWriter, r *http.Request) {
	var req registerReq
	if err := decode(r, &req); err != nil {
		writeErr(w, errBadRequest(err.Error()))
		return
	}
	u, err := h.Users.Register(r.Context(), req.Email, req.Name, req.Password, req.Role)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"id": u.ID, "email": u.Email, "display_name": u.DisplayName, "role": u.Role,
	})
}

type loginReq struct {
	Email    string `json:"email"`
	Password string `json:"password"`
}

type AuthDeps struct {
	Secret string
	TTL    time.Duration
}

func (h *Handlers) Login(deps AuthDeps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req loginReq
		if err := decode(r, &req); err != nil {
			writeErr(w, errBadRequest(err.Error()))
			return
		}
		u, err := h.Users.Login(r.Context(), req.Email, req.Password)
		if err != nil {
			writeErr(w, err)
			return
		}
		tok, err := middleware.IssueToken(deps.Secret, deps.TTL, u.ID, u.Role)
		if err != nil {
			writeErr(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"token": tok,
			"user":  map[string]any{"id": u.ID, "email": u.Email, "role": u.Role, "name": u.DisplayName},
		})
	}
}

func (h *Handlers) Me(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	writeJSON(w, http.StatusOK, map[string]any{
		"id": u.ID, "email": u.Email, "name": u.Name, "role": u.Role,
	})
}

type communityReq struct {
	Name string `json:"name"`
}

func (h *Handlers) CreateCommunity(w http.ResponseWriter, r *http.Request) {
	var req communityReq
	if err := decode(r, &req); err != nil {
		writeErr(w, errBadRequest(err.Error()))
		return
	}
	u := currentUser(r)
	c, err := h.Users.CreateCommunity(r.Context(), req.Name, u.ID)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, c)
}

func (h *Handlers) ListCommunities(w http.ResponseWriter, r *http.Request) {
	cs, err := h.Users.ListCommunities(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, cs)
}

type reviewerReq struct {
	UserID int64 `json:"user_id"`
}

func (h *Handlers) AddReviewer(w http.ResponseWriter, r *http.Request) {
	var req reviewerReq
	if err := decode(r, &req); err != nil {
		writeErr(w, errBadRequest(err.Error()))
		return
	}
	cid := urlInt64(r, "communityID")
	if err := h.Users.AddReviewer(r.Context(), cid, req.UserID); err != nil {
		writeErr(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
