package httpx

import (
	"errors"
	"fmt"
	"net/http"

	"github.com/jmoiron/sqlx"
	"github.com/labstack/echo/v4"
	"github.com/labstack/echo/v4/middleware"

	"synapticgo/internal/dataset"
	"synapticgo/internal/experiments"
	"synapticgo/internal/modelx"
)

// Deps bundles services for the HTTP layer.
type Deps struct {
	DB           *sqlx.DB
	Datasets     *dataset.Service
	Models       *modelx.Service
	Experiments  *experiments.Service
	MaxBodyBytes int64
}

// NewRouter builds the Echo instance with all routes mounted.
func NewRouter(d Deps) *echo.Echo {
	e := echo.New()
	e.HideBanner = true
	if d.MaxBodyBytes > 0 {
		e.Use(middleware.BodyLimitWithConfig(middleware.BodyLimitConfig{
			Limit: fmt.Sprintf("%d", d.MaxBodyBytes),
		}))
	}
	e.HTTPErrorHandler = func(err error, c echo.Context) {
		if c.Response().Committed {
			return
		}
		var he *echo.HTTPError
		if errors.As(err, &he) {
			msg := http.StatusText(he.Code)
			if m, ok := he.Message.(string); ok && m != "" {
				msg = m
			}
			_ = c.JSON(he.Code, map[string]string{"error": msg})
			return
		}
		_ = fail(c, err)
	}

	// User bootstrap: open endpoint (typically firewalled); it is the only
	// unauthenticated route and hands back a key exactly once.
	users := &userHandler{db: d.DB}
	e.POST("/api/v1/users", users.create)

	api := e.Group("/api/v1")
	api.Use(AuthMiddleware(d.DB))

	ds := &datasetHandler{svc: d.Datasets}
	api.POST("/datasets", ds.create)
	api.GET("/datasets", ds.list)
	api.GET("/datasets/:id", ds.get)
	api.DELETE("/datasets/:id", ds.delete)
	api.PUT("/datasets/:id/chunks/:idx", ds.putChunk)
	api.GET("/datasets/:id/chunks", ds.chunks)
	api.POST("/datasets/:id/publish", ds.publish)
	api.GET("/datasets/:id/content", ds.content)

	mh := &modelHandler{svc: d.Models}
	api.POST("/models", mh.register)
	api.GET("/models", mh.list)
	api.GET("/models/:id", mh.get)
	api.DELETE("/models/:id", mh.delete)

	eh := &experimentHandler{svc: d.Experiments, models: d.Models}
	api.POST("/models/:id/predict", eh.predict)
	api.GET("/models/:id/experiments", eh.list)
	api.GET("/experiments/compare", eh.compare)

	e.GET("/healthz", func(c echo.Context) error {
		return c.JSON(200, map[string]string{"status": "ok"})
	})
	return e
}
