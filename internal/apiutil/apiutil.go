// Package apiutil 提供两个 HTTP 组件共用的 JSON 读写辅助。
package apiutil

import (
	"encoding/json"
	"net/http"
)

// DecodeJSON 严格解析请求体（拒绝未知字段）。
func DecodeJSON(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	return dec.Decode(v)
}

// WriteJSON 以 JSON 写出响应。
func WriteJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// WriteError 写出 {"error": msg}。
func WriteError(w http.ResponseWriter, status int, msg string) {
	WriteJSON(w, status, map[string]string{"error": msg})
}
