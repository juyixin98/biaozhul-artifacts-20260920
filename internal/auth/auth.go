// Package auth implements API-key middleware for the three caller classes:
// workstation agents, department managers, and the deployment admin.
package auth

import (
	"context"
	"crypto/subtle"
	"net/http"

	"github.com/jmoiron/sqlx"
	"github.com/labstack/echo/v4"

	"desklens/internal/ingest"
	"desklens/internal/manager"
)

type ctxKey string

const (
	keyIngest  ctxKey = "ingestPrincipal"
	keyManager ctxKey = "managerPrincipal"
)

type result struct {
	workstationID string
	employeeID    int64
	departmentID  int64
	tz            string
	active        bool
}

// lookupIngestKey resolves an ingest key to its workstation principal.
func lookupIngestKey(ctx context.Context, db *sqlx.DB, key string) (result, error) {
	var r result
	err := db.QueryRowxContext(ctx, `
		select k.workstation_id, w.employee_id, e.department_id, e.timezone, k.active
		from ingest_api_keys k
		join workstations w on w.id = k.workstation_id
		join employees e on e.id = w.employee_id
		where k.key = $1`, key).Scan(&r.workstationID, &r.employeeID, &r.departmentID, &r.tz, &r.active)
	return r, err
}

func lookupManagerKey(ctx context.Context, db *sqlx.DB, key string) (manager.Principal, bool, error) {
	var p manager.Principal
	var active bool
	err := db.QueryRowxContext(ctx, `
		select username, department_id, active from manager_api_keys where key = $1`,
		key).Scan(&p.Username, &p.DepartmentID, &active)
	return p, active, err
}

func apiKey(c echo.Context) string {
	if v := c.Request().Header.Get("X-API-Key"); v != "" {
		return v
	}
	if user, pass, ok := c.Request().BasicAuth(); ok && user == "apikey" {
		return pass
	}
	return ""
}

func reject(c echo.Context, code string) error {
	return c.JSON(http.StatusUnauthorized, echo.Map{"error": echo.Map{"code": code, "message": "invalid or missing API key"}})
}

// IngestAuth authenticates a workstation agent.
func IngestAuth(db *sqlx.DB) echo.MiddlewareFunc {
	return func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			key := apiKey(c)
			if key == "" {
				return reject(c, "unauthorized")
			}
			r, err := lookupIngestKey(c.Request().Context(), db, key)
			if err != nil || !r.active {
				return reject(c, "unauthorized")
			}
			c.Set(string(keyIngest), ingest.Principal{
				WorkstationID: r.workstationID,
				EmployeeID:    r.employeeID,
				DepartmentID:  r.departmentID,
				Timezone:      r.tz,
			})
			return next(c)
		}
	}
}

// ManagerAuth authenticates a department manager.
func ManagerAuth(db *sqlx.DB) echo.MiddlewareFunc {
	return func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			key := apiKey(c)
			if key == "" {
				return reject(c, "unauthorized")
			}
			p, active, err := lookupManagerKey(c.Request().Context(), db, key)
			if err != nil || !active {
				return reject(c, "unauthorized")
			}
			c.Set(string(keyManager), p)
			return next(c)
		}
	}
}

// AdminAuth checks the single deployment admin secret with a constant-time
// comparison.
func AdminAuth(adminKey string) echo.MiddlewareFunc {
	return func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			if adminKey == "" {
				return c.JSON(http.StatusServiceUnavailable,
					echo.Map{"error": echo.Map{"code": "admin_disabled", "message": "ADMIN_API_KEY is not configured"}})
			}
			key := apiKey(c)
			if key == "" || subtle.ConstantTimeCompare([]byte(key), []byte(adminKey)) != 1 {
				return reject(c, "unauthorized")
			}
			return next(c)
		}
	}
}

func IngestPrincipal(c echo.Context) ingest.Principal {
	return c.Get(string(keyIngest)).(ingest.Principal)
}

func ManagerPrincipal(c echo.Context) manager.Principal {
	return c.Get(string(keyManager)).(manager.Principal)
}
