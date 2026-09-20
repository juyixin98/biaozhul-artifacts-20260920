package httpx

import (
	"encoding/json"
	"log"
	"net/http"
)

// Error is the uniform JSON error body: {"error": {"code": ..., "message": ...}}.
type Error struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if v == nil {
		return
	}
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("write response: %v", err)
	}
}

func JSON(w http.ResponseWriter, status int, v any) {
	writeJSON(w, status, v)
}

func ErrorJSON(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]any{"error": Error{Code: code, Message: msg}})
}

func Decode(r *http.Request, dst any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	return dec.Decode(dst)
}

// Domain error codes (stable strings documented in docs/api.md).
const (
	CodeBadRequest      = "bad_request"
	CodeUnauthorized    = "unauthorized"
	CodeForbidden       = "forbidden"
	CodeNotFound        = "not_found"
	CodeVersionConflict = "version_conflict"
	CodeStageConflict   = "invalid_transition"
	CodeGateFailed      = "gate_failed"
	CodeCapacity        = "evidence_capacity"
	CodeDuplicate       = "duplicate_request"
	CodeInternal        = "internal_error"
)
