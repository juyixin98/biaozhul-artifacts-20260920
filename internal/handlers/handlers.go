package handlers

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"

	"github.com/go-chi/chi/v5"

	"communitygov/internal/middleware"
	"communitygov/internal/services"
)

type Handlers struct {
	Users       *services.UserService
	Posts       *services.PostService
	Memberships *services.MembershipService
	Reports     *services.ReportService
	Courses     *services.CourseService
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, err error) {
	status := services.HTTPStatus(err)
	var se *services.Error
	detail := err.Error()
	if errors.As(err, &se) {
		detail = se.Detail
		if detail == "" {
			detail = se.Kind.Error()
		}
	}
	writeJSON(w, status, map[string]string{"error": detail})
}

func decode(r *http.Request, dst any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	return dec.Decode(dst)
}

func urlInt64(r *http.Request, key string) int64 {
	v := chi.URLParam(r, key)
	n, _ := strconv.ParseInt(v, 10, 64)
	return n
}

func queryInt32(r *http.Request, key string, def int32) int32 {
	v := r.URL.Query().Get(key)
	if v == "" {
		return def
	}
	n, err := strconv.ParseInt(v, 10, 32)
	if err != nil || n <= 0 {
		return def
	}
	return int32(n)
}

func currentUser(r *http.Request) middleware.CurrentUser {
	u, _ := middleware.UserFromContext(r.Context())
	return u
}

func errBadRequest(detail string) error {
	return services.E(services.ErrValidation, detail)
}

// readContext builds the services.ReadContext for a community-scoped request.
// Reviewer status in this specific community is resolved here so that a global
// "reviewer" account is only privileged inside communities it was assigned to.
func (h *Handlers) readContext(r *http.Request, communityID int64) services.ReadContext {
	u := currentUser(r)
	return services.ReadContext{
		CommunityID: communityID,
		User:        u,
		IsReviewer:  u.Role != "admin" && h.Users.IsReviewer(r.Context(), communityID, u.ID),
	}
}
