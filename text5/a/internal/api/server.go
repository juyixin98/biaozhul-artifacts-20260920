package api

import (
	"net/http"

	"github.com/jmoiron/sqlx"
	"github.com/labstack/echo/v4"
	"github.com/labstack/echo/v4/middleware"
)

// NewServer builds the HTTP server with all routes wired.
func NewServer(db *sqlx.DB) *echo.Echo {
	e := echo.New()
	e.HideBanner = true
	e.Use(middleware.Recover())
	e.Use(middleware.Logger())

	e.GET("/health", func(c echo.Context) error {
		if err := db.PingContext(c.Request().Context()); err != nil {
			return c.JSON(http.StatusServiceUnavailable, map[string]string{"status": "db unreachable"})
		}
		return c.JSON(http.StatusOK, map[string]string{"status": "ok"})
	})

	h := &handler{db: db}
	v1 := e.Group("/v1", authMiddleware(db))

	v1.POST("/snapshots", h.postSnapshots)

	v1.GET("/employees/:id/daily", h.getEmployeeDaily)
	v1.GET("/departments/:id/weekly", h.getDepartmentWeekly)
	v1.GET("/departments/:id/export.csv", h.exportDepartmentCSV)
	v1.GET("/snapshots", h.getSnapshots)

	admin := v1.Group("/admin", func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			if err := requireAdmin(c); err != nil {
				return err
			}
			return next(c)
		}
	})
	admin.POST("/rebuild", h.postRebuild)
	admin.POST("/cleanup", h.postCleanup)
	admin.POST("/policies", h.postPolicy)
	admin.POST("/rules", h.postRules)

	return e
}
