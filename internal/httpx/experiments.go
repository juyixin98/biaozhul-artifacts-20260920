package httpx

import (
	"crypto/sha256"
	"encoding/hex"
	"math"
	"net/http"
	"strconv"
	"time"

	"github.com/labstack/echo/v4"

	"synapticgo/internal/experiments"
	"synapticgo/internal/models"
	"synapticgo/internal/modelx"
)

type experimentHandler struct {
	svc    *experiments.Service
	models *modelx.Service
}

type predictReq struct {
	Features []float64 `json:"features"`
}

type predictResp struct {
	ModelVersionID int64     `json:"model_version_id"`
	PredictedClass string    `json:"predicted_class"`
	PredictedIndex int       `json:"predicted_index"`
	Confidence     float64   `json:"confidence"`
	Probabilities  []float64 `json:"probabilities"`
	LatencyMs      float64   `json:"latency_ms"`
}

func (h *experimentHandler) predict(c echo.Context) error {
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "bad model id")
	}
	var req predictReq
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid JSON body")
	}

	mv, w, err := h.models.LoadForInference(c.Request().Context(), id)
	if err != nil {
		return fail(c, err)
	}

	x := make([]float32, len(req.Features))
	for i, v := range req.Features {
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return echo.NewHTTPError(http.StatusUnprocessableEntity,
				"features must all be finite")
		}
		x[i] = float32(v)
	}

	start := time.Now()
	_, probs, idx, perr := w.Predict(x)
	latency := float64(time.Since(start).Microseconds()) / 1000.0
	if perr != nil {
		return echo.NewHTTPError(http.StatusUnprocessableEntity, perr.Error())
	}

	inputSum := sha256.Sum256(featuresCanonical(x))

	probs64 := make([]float64, len(probs))
	for i, p := range probs {
		probs64[i] = float64(p)
	}

	rec := &models.ExperimentRecord{
		ModelVersionID: mv.ID,
		CallerID:       callerID(c),
		InputDigest:    hex.EncodeToString(inputSum[:]),
		InputDim:       int32(w.InputDim),
		PredictedClass: mv.Classes[idx],
		PredictedIndex: int32(idx),
		Confidence:     probs64[idx],
		LatencyMs:      latency,
	}
	if err := h.svc.Record(c.Request().Context(), rec); err != nil {
		return fail(c, err)
	}

	return c.JSON(http.StatusOK, predictResp{
		ModelVersionID: mv.ID,
		PredictedClass: mv.Classes[idx],
		PredictedIndex: idx,
		Confidence:     probs64[idx],
		Probabilities:  probs64,
		LatencyMs:      latency,
	})
}

// featuresCanonical produces a stable digest input for the feature vector
// (little-endian float32, exactly what the model consumed).
func featuresCanonical(x []float32) []byte {
	buf := make([]byte, 4*len(x))
	for i, v := range x {
		bits := math.Float32bits(v)
		buf[4*i] = byte(bits)
		buf[4*i+1] = byte(bits >> 8)
		buf[4*i+2] = byte(bits >> 16)
		buf[4*i+3] = byte(bits >> 24)
	}
	return buf
}

func (h *experimentHandler) list(c echo.Context) error {
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "bad model id")
	}
	limit, offset := paging(c)
	out, err := h.svc.List(c.Request().Context(), callerID(c), id, limit, offset)
	if err != nil {
		return fail(c, err)
	}
	return c.JSON(http.StatusOK, map[string]any{"items": out})
}

func (h *experimentHandler) compare(c echo.Context) error {
	a, err1 := strconv.ParseInt(c.QueryParam("a"), 10, 64)
	b, err2 := strconv.ParseInt(c.QueryParam("b"), 10, 64)
	if err1 != nil || err2 != nil || a == 0 || b == 0 {
		return echo.NewHTTPError(http.StatusBadRequest, "query params a and b must be model version ids")
	}
	cmp, err := h.svc.Compare(c.Request().Context(), callerID(c), a, b)
	if err != nil {
		return fail(c, err)
	}
	return c.JSON(http.StatusOK, cmp)
}
