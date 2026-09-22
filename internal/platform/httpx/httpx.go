// Package httpx contains small HTTP response helpers.
package httpx

import (
	"encoding/json"
	"net/http"
)

type ErrorBody struct {
	Error struct {
		Code    string         `json:"code"`
		Message string         `json:"message"`
		Details map[string]any `json:"details,omitempty"`
	} `json:"error"`
}

// Error writes a JSON error. code defaults to the HTTP status phrase.
func Error(w http.ResponseWriter, status int, message string, details map[string]any) {
	var b ErrorBody
	b.Error.Message = message
	b.Error.Code = http.StatusText(status)
	b.Error.Details = details
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(b)
}

// JSON writes a JSON success body.
func JSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if v != nil {
		_ = json.NewEncoder(w).Encode(v)
	}
}

// DecodeJSON parses a JSON request body with a hard size cap.
func DecodeJSON(w http.ResponseWriter, r *http.Request, dst any, maxBytes int64) bool {
	r.Body = http.MaxBytesReader(w, r.Body, maxBytes)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		Error(w, http.StatusBadRequest, "invalid JSON body: "+err.Error(), nil)
		return false
	}
	return true
}
