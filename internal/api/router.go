package api

import (
	"strconv"
	"time"

	"github.com/labstack/echo/v4"
	"github.com/labstack/echo/v4/middleware"

	"github.com/synapticgo/synapticgo/internal/auth"
	"github.com/synapticgo/synapticgo/internal/config"
	"github.com/synapticgo/synapticgo/internal/dataset"
	"github.com/synapticgo/synapticgo/internal/httpx"
	model "github.com/synapticgo/synapticgo/internal/model"
	"github.com/synapticgo/synapticgo/internal/user"
)

// Deps bundles everything the HTTP layer needs.
type Deps struct {
	Cfg      config.Config
	Users    *user.Service
	Datasets *dataset.Service
	Models   *model.Service
}

// NewServer builds the echo router.
func NewServer(d Deps) *echo.Echo {
	e := echo.New()
	e.HideBanner = true
	// Route every handler-returned error through the single APIError-aware
	// mapper so sentinel statuses (400/404/409/422/...) survive.
	e.HTTPErrorHandler = func(err error, c echo.Context) {
		_ = httpx.HandleError(c, err)
	}
	e.Use(middleware.Recover())
	e.Use(middleware.Logger())
	e.Use(middleware.TimeoutWithConfig(middleware.TimeoutConfig{Timeout: 60 * time.Second}))

	e.GET("/healthz", func(c echo.Context) error {
		return c.JSON(200, map[string]string{"status": "ok"})
	})

	// Unauthenticated: account creation returns the one-time token.
	e.POST("/v1/users", d.Users.Register)

	v1 := e.Group("/v1", auth.Middleware(d.Users.DB))
	v1.GET("/me", d.Users.Me)

	dh := &DatasetHandler{Svc: d.Datasets, MaxChunkMB: d.Cfg.MaxChunkBytes}
	v1.GET("/datasets", dh.List)
	v1.POST("/datasets", dh.Create)
	v1.GET("/datasets/:id", dh.Get)
	v1.DELETE("/datasets/:id", dh.Delete)
	v1.POST("/datasets/:id/publish", dh.Publish)
	v1.PUT("/datasets/:id/chunks/:idx", dh.UploadChunk)
	v1.GET("/datasets/:id/content", dh.Download)

	mh := &ModelHandler{Svc: d.Models}
	v1.GET("/models", mh.List)
	v1.POST("/models", mh.Register)
	v1.GET("/models/:id", mh.Get)
	v1.DELETE("/models/:id", mh.Delete)
	v1.POST("/models/:id/predict", mh.Predict)
	// Comparison and experiment logs live at top-level paths so they do not
	// collide with the "/models/:id" parameter route.
	v1.GET("/model-comparisons", mh.Compare)
	v1.GET("/experiments", mh.Experiments)

	return e
}

func parseInt64(s string) (int64, error) {
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil || n <= 0 {
		return 0, strconv.ErrSyntax
	}
	return n, nil
}
