package httpapi

import (
	"net/http"
	"strings"

	"domainengine/internal/apierror"
	"domainengine/internal/domains"
	"domainengine/internal/models"

	"github.com/labstack/echo/v4"
)

// authMiddleware validates "Authorization: Bearer <token>" and stores the
// principal on the request context.
func (s *Server) authMiddleware(next echo.HandlerFunc) echo.HandlerFunc {
	return func(c echo.Context) error {
		h := c.Request().Header.Get("Authorization")
		const prefix = "Bearer "
		if !strings.HasPrefix(h, prefix) {
			return writeError(c, apierror.ErrUnauthorized)
		}
		token := strings.TrimSpace(h[len(prefix):])
		p, err := s.accounts.Authenticate(c.Request().Context(), token)
		if err != nil {
			return writeError(c, err)
		}
		c.Set(string(principalKey), p)
		return next(c)
	}
}

// requireRole rejects requests whose principal has a different role.
func (s *Server) requireRole(role string) echo.MiddlewareFunc {
	return func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			p := principalFrom(c)
			if p == nil || p.Role != role {
				return writeError(c, apierror.ErrForbidden)
			}
			return next(c)
		}
	}
}

func writeError(c echo.Context, err error) error {
	if ae, ok := apierror.As(err); ok {
		return c.JSON(ae.Status, map[string]string{
			"error":   ae.Code,
			"message": ae.Message,
		})
	}
	return c.JSON(http.StatusInternalServerError, map[string]string{
		"error": "internal_error",
	})
}

// scopeFor computes the visibility scope enforced by list/get queries.
func scopeFor(p *models.Principal) domains.Scope {
	switch p.Role {
	case "admin":
		return domains.Scope{}
	case "reseller":
		if p.ResellerID == nil {
			return domains.Scope{ResellerID: -1}
		}
		return domains.Scope{ResellerID: *p.ResellerID}
	case "customer":
		if p.CustomerID == nil {
			return domains.Scope{CustomerID: -1}
		}
		return domains.Scope{CustomerID: *p.CustomerID}
	}
	return domains.Scope{CustomerID: -1}
}
