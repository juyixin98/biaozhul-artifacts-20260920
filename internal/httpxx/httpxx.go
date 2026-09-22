// Package httpxx holds small HTTP helpers shared by handlers.
package httpxx

import (
	"encoding/json"
	"net/http"
)

type ErrorBody struct {
	Error      string      `json:"error"`
	StatusCode int         `json:"status_code"`
	Details    interface{} `json:"details,omitempty"`
}

// JSON writes v as JSON with the given status.
func JSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if v != nil {
		_ = json.NewEncoder(w).Encode(v)
	}
}

// Error writes a uniform error envelope; details may be nil.
func Error(w http.ResponseWriter, status int, msg string, details interface{}) {
	JSON(w, status, ErrorBody{Error: msg, StatusCode: status, Details: details})
}
