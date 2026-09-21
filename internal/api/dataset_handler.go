package api

import (
	"net/http"
	"strconv"

	"github.com/labstack/echo/v4"

	"github.com/synapticgo/synapticgo/internal/dataset"
	"github.com/synapticgo/synapticgo/internal/httpx"
)

// DatasetHandler wires dataset service methods to HTTP routes.
type DatasetHandler struct {
	Svc        *dataset.Service
	MaxChunkMB int64
}

func maxChunkBytes(limitMB int64) int64 { return limitMB }

// Create starts an upload session.
func (h *DatasetHandler) Create(c echo.Context) error {
	u := httpx.CurrentUser(c)
	var req dataset.CreateRequest
	if err := c.Bind(&req); err != nil {
		return httpx.ErrBadRequest("invalid JSON body")
	}
	v, err := h.Svc.Create(c.Request().Context(), u.ID, &req)
	if err != nil {
		return httpx.HandleError(c, err)
	}
	return c.JSON(http.StatusCreated, v)
}

// Get returns a dataset with per-chunk receipt status.
func (h *DatasetHandler) Get(c echo.Context) error {
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

// List returns the caller's datasets.
func (h *DatasetHandler) List(c echo.Context) error {
	u := httpx.CurrentUser(c)
	limit := queryInt(c, "limit", 50)
	vs, err := h.Svc.List(c.Request().Context(), u.ID, limit)
	if err != nil {
		return httpx.HandleError(c, err)
	}
	return c.JSON(200, vs)
}

// UploadChunk handles PUT /datasets/:id/chunks/:idx. The body is streamed
// straight into the storage layer; at most MaxChunkMB bytes are accepted.
func (h *DatasetHandler) UploadChunk(c echo.Context) error {
	u := httpx.CurrentUser(c)
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	idx, err := idxParam(c, "idx")
	if err != nil {
		return err
	}
	c.Response().Header().Set("X-Synaptic-Dataset-ID", strconv.FormatInt(id, 10))
	err = h.Svc.UploadChunk(c.Request().Context(), u.ID, id, int(idx),
		c.Request().Body, maxChunkBytes(h.MaxChunkMB))
	if err != nil {
		return httpx.HandleError(c, err)
	}
	return c.JSON(200, map[string]any{
		"dataset_id": id,
		"idx":        idx,
		"received":   true,
	})
}

// Publish merges chunks and flips the dataset to ready.
func (h *DatasetHandler) Publish(c echo.Context) error {
	u := httpx.CurrentUser(c)
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	v, err := h.Svc.Publish(c.Request().Context(), u.ID, id)
	if err != nil {
		return httpx.HandleError(c, err)
	}
	return c.JSON(200, v)
}

// Delete removes a dataset if nothing references it.
func (h *DatasetHandler) Delete(c echo.Context) error {
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

// Download streams the verified content of a ready dataset.
func (h *DatasetHandler) Download(c echo.Context) error {
	u := httpx.CurrentUser(c)
	id, err := idParam(c, "id")
	if err != nil {
		return err
	}
	raw, sha, err := h.Svc.OpenContent(c.Request().Context(), u.ID, id)
	if err != nil {
		return httpx.HandleError(c, err)
	}
	c.Response().Header().Set("ETag", `"`+sha+`"`)
	return c.Blob(200, "application/octet-stream", raw)
}

func idParam(c echo.Context, name string) (int64, error) {
	v := c.Param(name)
	id, err := strconv.ParseInt(v, 10, 64)
	if err != nil || id <= 0 {
		return 0, httpx.ErrBadRequest("invalid " + name + " path parameter")
	}
	return id, nil
}

// idxParam parses a zero-based chunk index (0 is a valid chunk position).
func idxParam(c echo.Context, name string) (int64, error) {
	v := c.Param(name)
	id, err := strconv.ParseInt(v, 10, 64)
	if err != nil || id < 0 {
		return 0, httpx.ErrBadRequest("invalid " + name + " path parameter")
	}
	return id, nil
}

func queryInt(c echo.Context, name string, def int) int {
	v := c.QueryParam(name)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}
