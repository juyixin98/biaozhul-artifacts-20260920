package httpserver

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"communityvault/internal/apperr"
	"communityvault/internal/db"
)

type ctxKey string

const viewerKey ctxKey = "viewer"

// Viewer is the request principal. User is nil for anonymous requests.
type Viewer struct {
	User                *db.User
	ModeratorCategories []int64
}

func viewerFrom(ctx context.Context) Viewer {
	v, _ := ctx.Value(viewerKey).(Viewer)
	return v
}

// Auth loads the bearer token, if present. Anonymous requests are allowed
// through; handlers decide which endpoints require a user.
func Auth(pool *pgxpool.Pool) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			v := Viewer{}
			if h := r.Header.Get("Authorization"); strings.HasPrefix(h, "Bearer ") {
				token := strings.TrimPrefix(h, "Bearer ")
				u, err := db.New(pool).GetUserByToken(r.Context(), token)
				if err == nil {
					v.User = &u
					if u.Role == "moderator" {
						if cats, err := db.New(pool).ModeratorCategoryIDs(r.Context(), u.ID); err == nil {
							v.ModeratorCategories = cats
						}
					}
				}
				// An unknown token is treated as anonymous; protected
				// handlers reject anonymous viewers with 401.
			}
			next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), viewerKey, v)))
		})
	}
}

func requireUser(r *http.Request) (Viewer, error) {
	v := viewerFrom(r.Context())
	if v.User == nil {
		return v, apperr.ErrUnauthorized
	}
	return v, nil
}

func requireRole(r *http.Request, roles ...string) (Viewer, error) {
	v, err := requireUser(r)
	if err != nil {
		return v, err
	}
	for _, role := range roles {
		if v.User.Role == role {
			return v, nil
		}
	}
	return v, apperr.ErrForbidden
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, err error) {
	if ae, ok := apperr.As(err); ok {
		writeJSON(w, ae.Status, map[string]any{"error": ae.Code, "message": ae.Msg})
		return
	}
	if err == pgx.ErrNoRows {
		writeJSON(w, 404, map[string]any{"error": "not_found", "message": "resource not found"})
		return
	}
	writeJSON(w, 500, map[string]any{"error": "internal", "message": err.Error()})
}

func decode(r *http.Request, dst any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return apperr.New(400, "bad_json", err.Error())
	}
	return nil
}
