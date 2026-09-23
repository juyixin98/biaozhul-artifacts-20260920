package service

import (
	"encoding/json"
	"net/http"
)

const maxBodyBytes = 4 << 20 // 4 MiB

// Handler 返回装配好路由的 http.Handler。
func Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", healthz)
	mux.HandleFunc("POST /api/v1/scan", handleScan)
	mux.HandleFunc("POST /api/v1/affected", handleAffected)
	mux.HandleFunc("POST /api/v1/parse", handleParse)
	return mux
}

func healthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func handleScan(w http.ResponseWriter, r *http.Request) {
	var req ScanRequest
	if !decode(w, r, &req) {
		return
	}
	resp, err := Engine{}.Scan(req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func handleAffected(w http.ResponseWriter, r *http.Request) {
	var req AffectedRequest
	if !decode(w, r, &req) {
		return
	}
	resp, err := Engine{}.Affected(req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func handleParse(w http.ResponseWriter, r *http.Request) {
	var req ParseRequest
	if !decode(w, r, &req) {
		return
	}
	resp, err := Engine{}.ParseFile(req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func decode(w http.ResponseWriter, r *http.Request, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "invalid JSON request: " + err.Error(),
		})
		return false
	}
	return true
}

func writeError(w http.ResponseWriter, err error) {
	code := http.StatusInternalServerError
	msg := err.Error()
	if re, ok := err.(*RequestError); ok {
		code = http.StatusBadRequest
		_ = re
	}
	writeJSON(w, code, map[string]string{"error": msg})
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	_ = enc.Encode(body)
}
