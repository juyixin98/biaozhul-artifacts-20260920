package api

import (
	"net/http"
	"strings"

	"github.com/jmoiron/sqlx"
	"github.com/labstack/echo/v4"
)

// authUser is the authenticated caller resolved from an API token.
type authUser struct {
	EmployeeID   int64  `db:"employee_id"`
	DepartmentID int64  `db:"department_id"`
	Role         string `db:"role"` // employee | manager | admin
}

const ctxUserKey = "desklens.user"

func authMiddleware(db *sqlx.DB) echo.MiddlewareFunc {
	return func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			h := c.Request().Header.Get("Authorization")
			if !strings.HasPrefix(h, "Bearer ") {
				return errJSON(http.StatusUnauthorized, "missing bearer token")
			}
			token := strings.TrimSpace(strings.TrimPrefix(h, "Bearer "))
			if token == "" {
				return errJSON(http.StatusUnauthorized, "missing bearer token")
			}
			var u authUser
			err := db.GetContext(c.Request().Context(), &u, `
				SELECT e.id AS employee_id, e.department_id, e.role
				FROM api_tokens t JOIN employees e ON e.id = t.employee_id
				WHERE t.token = $1`, token)
			if err != nil {
				return errJSON(http.StatusUnauthorized, "invalid token")
			}
			c.Set(ctxUserKey, &u)
			return next(c)
		}
	}
}

func currentUser(c echo.Context) *authUser {
	u, _ := c.Get(ctxUserKey).(*authUser)
	return u
}

func requireAdmin(c echo.Context) error {
	if u := currentUser(c); u == nil || u.Role != "admin" {
		return errJSON(http.StatusForbidden, "admin role required")
	}
	return nil
}

// canViewEmployee reports whether u may read data belonging to target.
func canViewEmployee(u *authUser, targetEmployeeID, targetDepartmentID int64) bool {
	switch u.Role {
	case "admin":
		return true
	case "manager":
		return u.DepartmentID == targetDepartmentID
	default: // employee
		return u.EmployeeID == targetEmployeeID
	}
}

func canViewDepartment(u *authUser, departmentID int64) bool {
	return u.Role == "admin" || (u.Role == "manager" && u.DepartmentID == departmentID)
}

func errJSON(status int, msg string) *echo.HTTPError {
	return echo.NewHTTPError(status, map[string]string{"error": msg})
}
