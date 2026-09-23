// Package httpapi exposes the management endpoints of the SOCKS5 proxy over
// net/http. It is a control plane only; proxy traffic speaks SOCKS5.
package httpapi

import (
	"encoding/json"
	"net/http"

	"socks5loop/internal/socks5"
)

// Handler returns an http.Handler serving:
//
//	GET /healthz     liveness probe
//	GET /stats        JSON snapshot of proxy counters
//	GET /allowlist    the effective destination rules
func Handler(srv *socks5.Server, cidrs, hosts []string) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	mux.HandleFunc("GET /stats", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, srv.Stats())
	})
	mux.HandleFunc("GET /allowlist", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"cidrs": cidrs,
			"hosts": hosts,
		})
	})
	return mux
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
