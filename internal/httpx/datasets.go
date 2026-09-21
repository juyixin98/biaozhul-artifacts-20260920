package httpx

import (
	"net/http"
	"strconv"

	"github.com/labstack/echo/v4"

	"synapticgo/internal/dataset"
)

type datasetHandler struct {
	svc *dataset.Service
}

type createDatasetReq struct {
	Name        string `json:"name"`
	TotalSize   int64  `json:"total_size"`
	ChunkSize   int32  `json:"chunk_size"`
	WholeDigest string `json:"whole_digest"`
}

func (h *datasetHandler) create(c echo.Context) error {
	var req createDatasetReq
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid JSON body")
	}
	var wd *string
	if req.WholeDigest != "" {
		wd = &req.WholeDigest
	}
	d, err := h.svc.Create(c.Request().Context(), callerID(c), req.Name, req.TotalSize, req.ChunkSize, wd)
	if err != nil {
		return fail(c, err)
	}
	return c.JSON(http.StatusCreated, d)
}

func (h *datasetHandler) get(c echo.Context) error {
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "bad dataset id")
	}
	d, err := h.svc.Get(c.Request().Context(), callerID(c), id)
	if err != nil {
		return fail(c, err)
	}
	return c.JSON(http.StatusOK, d)
}

func (h *datasetHandler) list(c echo.Context) error {
	limit, offset := paging(c)
	out, err := h.svc.List(c.Request().Context(), callerID(c), limit, offset)
	if err != nil {
		return fail(c, err)
	}
	return c.JSON(http.StatusOK, map[string]any{"items": out})
}

func (h *datasetHandler) delete(c echo.Context) error {
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "bad dataset id")
	}
	if err := h.svc.Delete(c.Request().Context(), callerID(c), id); err != nil {
		return fail(c, err)
	}
	return c.NoContent(http.StatusNoContent)
}

func (h *datasetHandler) chunks(c echo.Context) error {
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "bad dataset id")
	}
	status, err := h.svc.ChunkStatus(c.Request().Context(), callerID(c), id)
	if err != nil {
		return fail(c, err)
	}
	return c.JSON(http.StatusOK, map[string]any{"chunks": status})
}

// putChunk accepts a raw chunk body. Headers:
//
//	Content-Type: application/octet-stream
//	Digest: sha-256=<hex>
//	Content-Length must equal the slot length
func (h *datasetHandler) putChunk(c echo.Context) error {
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "bad dataset id")
	}
	idx64, err := strconv.ParseInt(c.Param("idx"), 10, 32)
	if err != nil || idx64 < 0 {
		return echo.NewHTTPError(http.StatusBadRequest, "bad chunk index")
	}
	idx := int32(idx64)
	digest := digestFromHeader(c.Request().Header.Get("Digest"))
	if digest == "" {
		return echo.NewHTTPError(http.StatusBadRequest, `Digest header required, e.g. "Digest: sha-256=<hex>"`)
	}
	ch, idempotent, err := h.svc.UploadChunk(c.Request().Context(), callerID(c), id, idx,
		c.Request().Body, digest)
	if err != nil {
		return fail(c, err)
	}
	code := http.StatusCreated
	if idempotent {
		code = http.StatusOK
	}
	return c.JSON(code, map[string]any{"chunk": ch, "idempotent": idempotent})
}

func (h *datasetHandler) publish(c echo.Context) error {
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "bad dataset id")
	}
	d, digest, err := h.svc.Publish(c.Request().Context(), callerID(c), id)
	if err != nil {
		return fail(c, err)
	}
	return c.JSON(http.StatusOK, map[string]any{"dataset": d, "whole_digest": digest})
}

func (h *datasetHandler) content(c echo.Context) error {
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "bad dataset id")
	}
	_, digest, r, err := h.svc.OpenContent(c.Request().Context(), callerID(c), id)
	if err != nil {
		return fail(c, err)
	}
	defer r.Close()
	c.Response().Header().Set(echo.HeaderContentType, "application/octet-stream")
	c.Response().Header().Set("Digest", "sha-256="+digest)
	return c.Stream(http.StatusOK, "application/octet-stream", r)
}

func paging(c echo.Context) (limit, offset int) {
	limit = 50
	offset = 0
	if v := c.QueryParam("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 500 {
			limit = n
		}
	}
	if v := c.QueryParam("offset"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			offset = n
		}
	}
	return limit, offset
}
