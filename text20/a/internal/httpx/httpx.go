package httpx

import (
	"encoding/json"
	"log"
	"net/http"
)

// Error is the uniform error body for all 4xx/5xx responses.
type Error struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type errorBody struct {
	Error Error `json:"error"`
}

// JSON writes v as JSON with the given status code.
func JSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	if v == nil {
		return
	}
	enc := json.NewEncoder(w)
	if err := enc.Encode(v); err != nil {
		log.Printf("write response: %v", err)
	}
}

// ErrorJSON writes a uniform error response.
func ErrorJSON(w http.ResponseWriter, status int, code, message string) {
	JSON(w, status, errorBody{Error: Error{Code: code, Message: message}})
}

// DecodeJSON decodes a JSON request body into dst, rejecting unknown fields.
func DecodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		ErrorJSON(w, http.StatusBadRequest, "invalid_json", "request body is not valid JSON: "+err.Error())
		return false
	}
	return true
}
