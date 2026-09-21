// Package httpx contains small HTTP helpers.
package httpx

import (
	"encoding/json"
	"net/http"
)

// Error is the uniform JSON error body.
type Error struct {
	Error string `json:"error"`
}

func JSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	if v != nil {
		_ = json.NewEncoder(w).Encode(v)
	}
}

func Err(w http.ResponseWriter, status int, msg string) {
	JSON(w, status, Error{Error: msg})
}

func Decode(r *http.Request, dst any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	return dec.Decode(dst)
}
