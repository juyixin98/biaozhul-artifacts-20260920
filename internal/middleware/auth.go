package middleware

import (
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"

	"communitygov/internal/database/sqlcgen"
)

type ctxKey int

const userCtxKey ctxKey = iota

type Claims struct {
	UserID int64  `json:"uid"`
	Role   string `json:"role"`
	jwt.RegisteredClaims
}

func IssueToken(secret string, ttl time.Duration, userID int64, role string) (string, error) {
	claims := Claims{
		UserID: userID,
		Role:   role,
		RegisteredClaims: jwt.RegisteredClaims{
			ExpiresAt: jwt.NewNumericDate(time.Now().Add(ttl)),
			IssuedAt:  jwt.NewNumericDate(time.Now()),
		},
	}
	return jwt.NewWithClaims(jwt.SigningMethodHS256, claims).SignedString([]byte(secret))
}

// CurrentUser is the authenticated principal attached to the request context.
type CurrentUser struct {
	ID    int64
	Email string
	Name  string
	Role  string // admin | reviewer | member (global role)
}

func UserFromContext(ctx context.Context) (CurrentUser, bool) {
	u, ok := ctx.Value(userCtxKey).(CurrentUser)
	return u, ok
}

// Authenticator validates the Bearer token and loads the live user row, so a
// token issued before an account change still reflects current role.
func Authenticator(secret string, getUser func(context.Context, int64) (sqlcgen.User, error)) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			h := r.Header.Get("Authorization")
			if h == "" || !strings.HasPrefix(h, "Bearer ") {
				http.Error(w, `{"error":"missing bearer token"}`, http.StatusUnauthorized)
				return
			}
			tokenStr := strings.TrimPrefix(h, "Bearer ")
			claims := &Claims{}
			token, err := jwt.ParseWithClaims(tokenStr, claims, func(t *jwt.Token) (interface{}, error) {
				if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
					return nil, jwt.ErrSignatureInvalid
				}
				return []byte(secret), nil
			})
			if err != nil || !token.Valid {
				http.Error(w, `{"error":"invalid or expired token"}`, http.StatusUnauthorized)
				return
			}
			user, err := getUser(r.Context(), claims.UserID)
			if err != nil {
				http.Error(w, `{"error":"user no longer exists"}`, http.StatusUnauthorized)
				return
			}
			cu := CurrentUser{ID: user.ID, Email: user.Email, Name: user.DisplayName, Role: user.Role}
			ctx := context.WithValue(r.Context(), userCtxKey, cu)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// RequireRole returns 403 unless the current user has one of the global roles.
func RequireRole(roles ...string) func(http.Handler) http.Handler {
	allowed := make(map[string]bool, len(roles))
	for _, r := range roles {
		allowed[r] = true
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			u, ok := UserFromContext(r.Context())
			if !ok || !allowed[u.Role] {
				http.Error(w, `{"error":"forbidden"}`, http.StatusForbidden)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}
