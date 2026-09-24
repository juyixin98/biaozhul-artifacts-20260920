// Package httpx contains small helpers shared by the lock and resource HTTP servers.
package httpx

import (
	"encoding/json"
	"fmt"
	"net/http"
)

// JSON writes v as JSON with the given status code.
func JSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(v)
}

// Errorf responds with a JSON error body: {"error":"..."}.
func Errorf(w http.ResponseWriter, status int, format string, args ...any) {
	JSON(w, status, map[string]string{"error": fmt.Sprintf(format, args...)})
}

// DecodeJSON strictly decodes r's body into dst. It rejects unknown fields
// and requires a non-empty body.
func DecodeJSON(r *http.Request, dst any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return err
	}
	return nil
}
