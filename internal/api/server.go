// Package api exposes the simulation over a small JSON/HTTP interface.
package api

import (
	"encoding/json"
	"io"
	"net/http"

	"chsim/internal/sim"
)

const maxBody = 16 << 20 // 16 MiB

// Handler returns the HTTP routes:
//
//	POST /run      body: scenario JSON -> 200 result JSON | 400 {"error": ...}
//	GET  /healthz  -> 200 "ok"
func Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/run", runHandler)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	return mux
}

func runHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "use POST")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBody))
	if err != nil {
		writeError(w, http.StatusRequestEntityTooLarge, "body too large")
		return
	}
	var sc sim.Scenario
	if err := json.Unmarshal(body, &sc); err != nil {
		writeError(w, http.StatusBadRequest, "invalid scenario JSON: "+err.Error())
		return
	}
	res, err := sim.Run(sc)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/json")
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(res)
}

func writeError(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": msg})
}
