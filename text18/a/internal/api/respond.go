// Package api holds HTTP transport helpers and handlers.
package api

import (
	"encoding/json"
	"errors"
	"net/http"

	"sircc/internal/domain"
)

type errorBody struct {
	Error   string `json:"error"`
	Message string `json:"message"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	if v != nil {
		_ = json.NewEncoder(w).Encode(v)
	}
}

func writeError(w http.ResponseWriter, err error) {
	status, code := mapError(err)
	writeJSON(w, status, errorBody{Error: code, Message: err.Error()})
}

func mapError(err error) (int, string) {
	switch {
	case errors.Is(err, domain.ErrNotFound):
		return http.StatusNotFound, "not_found"
	case errors.Is(err, domain.ErrForbidden):
		return http.StatusForbidden, "forbidden"
	case errors.Is(err, domain.ErrStaleVersion):
		return http.StatusConflict, "stale_version"
	case errors.Is(err, domain.ErrKeyReuse):
		return http.StatusConflict, "idempotency_key_reuse"
	case errors.Is(err, domain.ErrClosureGate),
		errors.Is(err, domain.ErrTriageGate),
		errors.Is(err, domain.ErrEvidenceCap):
		return http.StatusUnprocessableEntity, "precondition_failed"
	case errors.Is(err, domain.ErrInvalidTransition):
		return http.StatusConflict, "invalid_transition"
	case errors.Is(err, domain.ErrValidation):
		return http.StatusBadRequest, "validation_error"
	default:
		return http.StatusInternalServerError, "internal_error"
	}
}

func decode(w http.ResponseWriter, r *http.Request, dst any) bool {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		writeJSON(w, http.StatusBadRequest, errorBody{
			Error:   "validation_error",
			Message: "invalid JSON body: " + err.Error(),
		})
		return false
	}
	return true
}
