// Package httpapi exposes the SynapticGo JSON API on top of Echo.
package httpapi

import (
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/jmoiron/sqlx"
	"github.com/labstack/echo/v4"

	"synapticgo/internal/app"
)

const (
	maxChunkBytes = 64 << 20 // 64 MiB per chunk upload
	maxBatchRows  = 1024
)

type handler struct {
	datasets *app.DatasetService
	models   *app.ModelService
	exps     *app.ExperimentService
}

// New wires the API routes onto a fresh Echo instance.
func New(db *sqlx.DB, fs *app.FileStore) *echo.Echo {
	e := echo.New()
	e.HideBanner = true
	e.HidePort = true
	e.HTTPErrorHandler = errorHandler

	h := &handler{
		datasets: &app.DatasetService{DB: db, FS: fs},
		models:   &app.ModelService{DB: db},
		exps:     &app.ExperimentService{DB: db},
	}

	e.GET("/healthz", func(c echo.Context) error {
		return c.JSON(http.StatusOK, map[string]string{"status": "ok"})
	})

	g := e.Group("", authMiddleware)

	g.POST("/datasets", h.createDataset)
	g.GET("/datasets", h.listDatasets)
	g.GET("/datasets/:id", h.getDataset)
	g.PUT("/datasets/:id/chunks/:index", h.uploadChunk)
	g.POST("/datasets/:id/publish", h.publishDataset)
	g.DELETE("/datasets/:id", h.deleteDataset)

	g.POST("/models", h.createModel)
	g.GET("/models", h.listModels)
	g.GET("/models/:id", h.getModel)
	g.DELETE("/models/:id", h.deleteModel)
	g.POST("/models/:id/versions", h.createVersion)
	g.GET("/models/:id/versions/:version", h.getVersion)
	g.POST("/models/:id/versions/:version/predict", h.predict)
	g.GET("/models/:id/compare", h.compareVersions)

	g.POST("/experiments", h.createExperiment)
	g.GET("/experiments", h.listExperiments)
	g.DELETE("/experiments/:id", h.deleteExperiment)

	return e
}

func errorHandler(err error, c echo.Context) {
	if c.Response().Committed {
		return
	}
	var ae *app.Error
	if errors.As(err, &ae) {
		_ = c.JSON(ae.Status, map[string]string{"error": ae.Message})
		return
	}
	var he *echo.HTTPError
	if errors.As(err, &he) {
		_ = c.JSON(he.Code, map[string]any{"error": he.Message})
		return
	}
	_ = c.JSON(http.StatusInternalServerError, map[string]string{"error": err.Error()})
}

// authMiddleware derives the resource owner from X-User-ID. Every business
// resource is scoped to this identity.
func authMiddleware(next echo.HandlerFunc) echo.HandlerFunc {
	return func(c echo.Context) error {
		id := strings.TrimSpace(c.Request().Header.Get("X-User-ID"))
		if id == "" {
			return c.JSON(http.StatusUnauthorized, map[string]string{"error": "missing X-User-ID header"})
		}
		c.Set("owner", id)
		return next(c)
	}
}

func owner(c echo.Context) string { return c.Get("owner").(string) }

func idParam(c echo.Context, name string) (int64, error) {
	v, err := strconv.ParseInt(c.Param(name), 10, 64)
	if err != nil {
		return 0, app.BadRequestf("invalid %s parameter", name)
	}
	return v, nil
}

func intParam(c echo.Context, name string) (int, error) {
	v, err := strconv.Atoi(c.Param(name))
	if err != nil {
		return 0, app.BadRequestf("invalid %s parameter", name)
	}
	return v, nil
}

// --- datasets ---

func (h *handler) createDataset(c echo.Context) error {
	var in app.CreateDatasetInput
	if err := c.Bind(&in); err != nil {
		return app.BadRequestf("invalid request body: %v", err)
	}
	ds, err := h.datasets.Create(c.Request().Context(), owner(c), in)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, ds)
}

func (h *handler) listDatasets(c echo.Context) error {
	ds, err := h.datasets.List(c.Request().Context(), owner(c))
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"datasets": ds})
}

func (h *handler) getDataset(c echo.Context) error {
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	view, err := h.datasets.Get(c.Request().Context(), owner(c), id)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, view)
}

func (h *handler) uploadChunk(c echo.Context) error {
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	index, err := intParam(c, "index")
	if err != nil {
		return err
	}
	body, err := io.ReadAll(http.MaxBytesReader(c.Response(), c.Request().Body, maxChunkBytes))
	if err != nil {
		return app.BadRequestf("reading chunk body: %v", err)
	}
	chunk, created, err := h.datasets.UploadChunk(
		c.Request().Context(), owner(c), id, index, body,
		c.Request().Header.Get("X-Chunk-SHA256"))
	if err != nil {
		return err
	}
	status := http.StatusOK
	if created {
		status = http.StatusCreated
	}
	return c.JSON(status, chunk)
}

func (h *handler) publishDataset(c echo.Context) error {
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	ds, err := h.datasets.Publish(c.Request().Context(), owner(c), id)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, ds)
}

func (h *handler) deleteDataset(c echo.Context) error {
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	if err := h.datasets.Delete(c.Request().Context(), owner(c), id); err != nil {
		return err
	}
	return c.NoContent(http.StatusNoContent)
}

// --- models ---

func (h *handler) createModel(c echo.Context) error {
	var in app.CreateModelInput
	if err := c.Bind(&in); err != nil {
		return app.BadRequestf("invalid request body: %v", err)
	}
	m, err := h.models.Create(c.Request().Context(), owner(c), in)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, m)
}

func (h *handler) listModels(c echo.Context) error {
	models, err := h.models.List(c.Request().Context(), owner(c))
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"models": models})
}

func (h *handler) getModel(c echo.Context) error {
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	view, err := h.models.Get(c.Request().Context(), owner(c), id)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, view)
}

func (h *handler) deleteModel(c echo.Context) error {
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	if err := h.models.Delete(c.Request().Context(), owner(c), id); err != nil {
		return err
	}
	return c.NoContent(http.StatusNoContent)
}

func (h *handler) createVersion(c echo.Context) error {
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	var in app.CreateVersionInput
	if err := c.Bind(&in); err != nil {
		return app.BadRequestf("invalid request body: %v", err)
	}
	mv, err := h.models.CreateVersion(c.Request().Context(), owner(c), id, in)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, mv)
}

func (h *handler) getVersion(c echo.Context) error {
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	version, err := intParam(c, "version")
	if err != nil {
		return err
	}
	mv, err := h.models.GetVersion(c.Request().Context(), owner(c), id, version)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, mv)
}

func (h *handler) predict(c echo.Context) error {
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	version, err := intParam(c, "version")
	if err != nil {
		return err
	}
	var req struct {
		Inputs [][]float64 `json:"inputs"`
	}
	if err := c.Bind(&req); err != nil {
		return app.BadRequestf("invalid request body: %v", err)
	}
	if len(req.Inputs) > maxBatchRows {
		return app.BadRequestf("batch too large: %d rows (max %d)", len(req.Inputs), maxBatchRows)
	}
	preds, err := h.models.Predict(c.Request().Context(), owner(c), id, version, req.Inputs)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"predictions": preds})
}

func (h *handler) compareVersions(c echo.Context) error {
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	parts := strings.Split(c.QueryParam("versions"), ",")
	if len(parts) != 2 {
		return app.BadRequestf("versions query parameter must be two version numbers, e.g. ?versions=1,2")
	}
	v1, err1 := strconv.Atoi(strings.TrimSpace(parts[0]))
	v2, err2 := strconv.Atoi(strings.TrimSpace(parts[1]))
	if err1 != nil || err2 != nil {
		return app.BadRequestf("versions must be integers")
	}
	res, err := h.models.Compare(c.Request().Context(), owner(c), id, v1, v2)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, res)
}

// --- experiments ---

func (h *handler) createExperiment(c echo.Context) error {
	var in app.CreateExperimentInput
	if err := c.Bind(&in); err != nil {
		return app.BadRequestf("invalid request body: %v", err)
	}
	exp, err := h.exps.Create(c.Request().Context(), owner(c), in)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, exp)
}

func (h *handler) listExperiments(c echo.Context) error {
	var f app.ExperimentFilter
	for _, p := range []struct {
		name string
		dst  **int64
	}{
		{"model_id", &f.ModelID},
		{"model_version_id", &f.ModelVersionID},
		{"dataset_id", &f.DatasetID},
	} {
		if raw := c.QueryParam(p.name); raw != "" {
			v, err := strconv.ParseInt(raw, 10, 64)
			if err != nil {
				return app.BadRequestf("invalid %s query parameter", p.name)
			}
			*p.dst = &v
		}
	}
	exps, err := h.exps.List(c.Request().Context(), owner(c), f)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"experiments": exps})
}

func (h *handler) deleteExperiment(c echo.Context) error {
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	if err := h.exps.Delete(c.Request().Context(), owner(c), id); err != nil {
		return err
	}
	return c.NoContent(http.StatusNoContent)
}
