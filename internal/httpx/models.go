package httpx

import (
	"encoding/base64"
	"net/http"
	"strconv"
	"strings"

	"github.com/labstack/echo/v4"

	"synapticgo/internal/inference"
	"synapticgo/internal/modelx"
)

type modelHandler struct {
	svc *modelx.Service
}

type registerModelReq struct {
	ModelName string   `json:"model_name"`
	DatasetID int64    `json:"dataset_id"`
	InputDim  int      `json:"input_dim"`
	Classes   []string `json:"classes"`
	// WeightsBase64 is the encoded Weights blob (Decode-compatible).
	WeightsBase64 string `json:"weights_base64"`
}

func (h *modelHandler) register(c echo.Context) error {
	var req registerModelReq
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid JSON body")
	}
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(req.WeightsBase64))
	if err != nil {
		// Also tolerate URL-safe base64.
		raw, err = base64.RawURLEncoding.DecodeString(strings.TrimSpace(req.WeightsBase64))
		if err != nil {
			return echo.NewHTTPError(http.StatusBadRequest, "weights_base64 is not valid base64")
		}
	}
	w, err := inference.Decode(raw)
	if err != nil {
		return echo.NewHTTPError(http.StatusUnprocessableEntity, err.Error())
	}
	mv, err := h.svc.Register(c.Request().Context(), callerID(c), modelx.RegisterInput{
		ModelName: req.ModelName,
		DatasetID: req.DatasetID,
		InputDim:  req.InputDim,
		Classes:   req.Classes,
		Weights:   w,
	})
	if err != nil {
		return fail(c, err)
	}
	return c.JSON(http.StatusCreated, mv)
}

func (h *modelHandler) get(c echo.Context) error {
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "bad model id")
	}
	mv, err := h.svc.Get(c.Request().Context(), callerID(c), id)
	if err != nil {
		return fail(c, err)
	}
	return c.JSON(http.StatusOK, mv)
}

func (h *modelHandler) list(c echo.Context) error {
	limit, offset := paging(c)
	out, err := h.svc.List(c.Request().Context(), callerID(c), c.QueryParam("model_name"), limit, offset)
	if err != nil {
		return fail(c, err)
	}
	return c.JSON(http.StatusOK, map[string]any{"items": out})
}

func (h *modelHandler) delete(c echo.Context) error {
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "bad model id")
	}
	if err := h.svc.Delete(c.Request().Context(), callerID(c), id); err != nil {
		return fail(c, err)
	}
	return c.NoContent(http.StatusNoContent)
}

// predict is implemented on the experiment handler because recording is part
// of the operation; mounted here in the router.
