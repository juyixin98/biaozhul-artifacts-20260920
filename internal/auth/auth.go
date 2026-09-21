package auth

import (
	"context"
	"net/http"
	"strings"

	"github.com/labstack/echo/v4"

	"desklens/internal/repo"
)

const principalKey = "desklens.principal"

// Principal is the authenticated caller, stored on the echo context.
type Principal struct {
	Role         string
	DepartmentID *int64
}

// Middleware authenticates a bearer token against api_tokens. Agents POST
// snapshots anonymously (the snapshot payload carries workstation/employee),
// but all read and admin routes require a token.
func Middleware(r *repo.Repo) echo.MiddlewareFunc {
	return func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			h := c.Request().Header.Get(echo.HeaderAuthorization)
			token := strings.TrimSpace(strings.TrimPrefix(h, "Bearer"))
			token = strings.TrimSpace(token)
			if token == "" {
				token = c.QueryParam("token")
			}
			if token == "" {
				return echo.NewHTTPError(http.StatusUnauthorized, "missing bearer token")
			}
			t, err := r.Token(context.Background(), token)
			if err != nil {
				if err == repo.ErrNotFound {
					return echo.NewHTTPError(http.StatusUnauthorized, "invalid token")
				}
				return err
			}
			c.Set(principalKey, Principal{Role: t.Role, DepartmentID: t.DepartmentID})
			return next(c)
		}
	}
}

// From extracts the authenticated principal.
func From(c echo.Context) Principal {
	if p, ok := c.Get(principalKey).(Principal); ok {
		return p
	}
	return Principal{}
}

// RequireAdmin rejects manager callers.
func RequireAdmin(next echo.HandlerFunc) echo.HandlerFunc {
	return func(c echo.Context) error {
		if From(c).Role != "admin" {
			return echo.NewHTTPError(http.StatusForbidden, "admin role required")
		}
		return next(c)
	}
}
