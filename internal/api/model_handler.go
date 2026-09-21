package api

import (
	"encoding/json"
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/synapticgo/synapticgo/internal/httpx"
	model "github.com/synapticgo/synapticgo/internal/model"
)

// ModelHandler wires model registry methods to HTTP routes.
type ModelHandler struct {
	Svc *model.Service
}

// Register creates an immutable model version.
func (h *ModelHandler) Register(c echo.Context) error {
	u := httpx.CurrentUser(c)
	var req model.RegisterRequest
	body := http.MaxBytesReader(c.Response(), c.Request().Body, 32<<20)
	dec := json.NewDecoder(body)
	if err := dec.Decode(&req); err != nil {
		return httpx.ErrBadRequest("invalid JSON body (or >32MiB weights payload)")
	}
	v, err := h.Svc.Register(c.Request().Context(), u.ID, &req)
	if err != nil {
		return httpx.HandleError(c, err)
	}
	return c.JSON(201, v)
}

// Get returns one model version.
func (h *ModelHandler) Get(c echo.Context) error {
	u := httpx.CurrentUser(c)
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	v, err := h.Svc.Get(c.Request().Context(), u.ID, id)
	if err != nil {
		return httpx.HandleError(c, err)
	}
	return c.JSON(200, v)
}

// List returns the caller's model versions (?name= filters by model name).
func (h *ModelHandler) List(c echo.Context) error {
	u := httpx.CurrentUser(c)
	vs, err := h.Svc.List(c.Request().Context(), u.ID, c.QueryParam("name"), queryInt(c, "limit", 50))
	if err != nil {
		return httpx.HandleError(c, err)
	}
	return c.JSON(200, vs)
}

// Delete removes a model version.
func (h *ModelHandler) Delete(c echo.Context) error {
	u := httpx.CurrentUser(c)
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	if err := h.Svc.Delete(c.Request().Context(), u.ID, id); err != nil {
		return httpx.HandleError(c, err)
	}
	return c.NoContent(204)
}

// Predict runs the real forward pass and logs an experiment.
func (h *ModelHandler) Predict(c echo.Context) error {
	u := httpx.CurrentUser(c)
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	var req model.PredictRequest
	if err := c.Bind(&req); err != nil {
		return httpx.ErrBadRequest("invalid JSON body")
	}
	res, err := h.Svc.Predict(c.Request().Context(), u.ID, id, &req)
	if err != nil {
		return httpx.HandleError(c, err)
	}
	return c.JSON(200, res)
}

// Compare returns a metric comparison of two versions, refusing mismatched
// class tables/evaluation datasets. IDs are supplied as ?a=&b= query params.
func (h *ModelHandler) Compare(c echo.Context) error {
	u := httpx.CurrentUser(c)
	a, err := parsePositive(c.QueryParam("a"))
	if err != nil {
		return httpx.ErrBadRequest("invalid a query parameter")
	}
	b, err := parsePositive(c.QueryParam("b"))
	if err != nil {
		return httpx.ErrBadRequest("invalid b query parameter")
	}
	res, err := h.Svc.Compare(c.Request().Context(), u.ID, a, b)
	if err != nil {
		return httpx.HandleError(c, err)
	}
	return c.JSON(200, res)
}

// Experiments lists inference records (?model_version_id=, ?limit=, ?before=).
func (h *ModelHandler) Experiments(c echo.Context) error {
	u := httpx.CurrentUser(c)
	var mvID, beforeID *int64
	if v := c.QueryParam("model_version_id"); v != "" {
		id, err := parsePositive(v)
		if err != nil {
			return httpx.ErrBadRequest("invalid model_version_id")
		}
		mvID = &id
	}
	if v := c.QueryParam("before"); v != "" {
		id, err := parsePositive(v)
		if err != nil {
			return httpx.ErrBadRequest("invalid before")
		}
		beforeID = &id
	}
	res, err := h.Svc.ListExperiments(c.Request().Context(), u.ID, mvID,
		queryInt(c, "limit", 50), beforeID)
	if err != nil {
		return httpx.HandleError(c, err)
	}
	return c.JSON(200, res)
}

func parsePositive(s string) (int64, error) {
	id, err := parseInt64(s)
	return id, err
}
