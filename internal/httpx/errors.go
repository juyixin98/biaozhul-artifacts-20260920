// Package httpx holds HTTP transport: Echo handlers, middleware and routing.
package httpx

import (
	"errors"
	"net/http"

	"github.com/labstack/echo/v4"

	"synapticgo/internal/dataset"
	"synapticgo/internal/experiments"
	"synapticgo/internal/modelx"
)

type errResponse struct {
	Error string `json:"error"`
}

func fail(c echo.Context, err error) error {
	status := http.StatusInternalServerError
	switch {
	case isUniqueViolation(err):
		status = http.StatusConflict
	case errors.Is(err, dataset.ErrNotFound), errors.Is(err, modelx.ErrNotFound),
		errors.Is(err, experiments.ErrNotFound):
		status = http.StatusNotFound
	case errors.Is(err, dataset.ErrForbidden), errors.Is(err, modelx.ErrForbidden):
		status = http.StatusForbidden
	case errors.Is(err, dataset.ErrNotUploading), errors.Is(err, dataset.ErrInUse),
		errors.Is(err, modelx.ErrInUse), errors.Is(err, modelx.ErrDatasetNotReady):
		status = http.StatusConflict
	case errors.Is(err, experiments.ErrClassTablesDiffer):
		status = http.StatusUnprocessableEntity
	case errors.Is(err, dataset.ErrChunkIndex), errors.Is(err, dataset.ErrChunkOffset),
		errors.Is(err, dataset.ErrChunkLength), errors.Is(err, dataset.ErrDigestMismatch),
		errors.Is(err, dataset.ErrSizeMismatch), errors.Is(err, dataset.ErrMissingChunks),
		errors.Is(err, dataset.ErrWholeMismatch),
		errors.Is(err, modelx.ErrClassMismatch), errors.Is(err, modelx.ErrClassesInvalid):
		status = http.StatusUnprocessableEntity
	}
	return c.JSON(status, errResponse{Error: err.Error()})
}
