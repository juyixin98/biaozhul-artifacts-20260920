// Command pi-sim serves the deterministic priority-inheritance scheduler
// simulator over HTTP using only the Go standard library (net/http).
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"

	"pi-sim/sim"
)

type simRequest struct {
	Inheritance bool         `json:"inheritance"`
	Workload    sim.Workload `json:"workload"`
}

type errorResponse struct {
	Error string `json:"error"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		log.Printf("encode response: %v", err)
	}
}

func writeErr(w http.ResponseWriter, status int, format string, args ...any) {
	writeJSON(w, status, errorResponse{Error: fmt.Sprintf(format, args...)})
}

// decodeStrict reads a JSON body while rejecting unknown fields and
// trailing data.
func decodeStrict(r *http.Request, dst any) error {
	if r.Body == nil {
		return errors.New("missing request body")
	}
	dec := json.NewDecoder(io.LimitReader(r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return fmt.Errorf("invalid JSON: %w", err)
	}
	if dec.More() {
		return errors.New("invalid JSON: unexpected trailing values")
	}
	return nil
}

func handleSimulate(w http.ResponseWriter, r *http.Request) {
	var req simRequest
	if err := decodeStrict(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, "%s", err.Error())
		return
	}
	res, err := sim.Execute(req.Workload, req.Inheritance)
	if err != nil {
		writeErr(w, http.StatusUnprocessableEntity, "%s", err.Error())
		return
	}
	// A deadlock is a valid simulation outcome, not a request error.
	writeJSON(w, http.StatusOK, res)
}

func handleCompare(w http.ResponseWriter, r *http.Request) {
	var wl sim.Workload
	if err := decodeStrict(r, &wl); err != nil {
		writeErr(w, http.StatusBadRequest, "%s", err.Error())
		return
	}
	rep, err := sim.Compare(wl)
	if err != nil {
		writeErr(w, http.StatusUnprocessableEntity, "%s", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

func handlePresets(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeErr(w, http.StatusMethodNotAllowed, "use GET")
		return
	}
	presets := map[string]sim.Workload{}
	for _, n := range sim.PresetNames() {
		wl, _ := sim.Preset(n)
		presets[n] = wl
	}
	writeJSON(w, http.StatusOK, presets)
}

// handlePreset handles /api/presets/{name} (workload) and
// /api/presets/{name}/run?inheritance=0|1 (simulation result).
func handlePreset(w http.ResponseWriter, r *http.Request, run bool, name string) {
	wl, ok := sim.Preset(name)
	if !ok {
		writeErr(w, http.StatusNotFound, "unknown preset %q (try: %s)", name, strings.Join(sim.PresetNames(), ", "))
		return
	}
	if !run {
		if r.Method != http.MethodGet {
			writeErr(w, http.StatusMethodNotAllowed, "use GET")
			return
		}
		writeJSON(w, http.StatusOK, wl)
		return
	}
	if r.Method != http.MethodPost {
		writeErr(w, http.StatusMethodNotAllowed, "use POST")
		return
	}
	inheritance := r.URL.Query().Get("inheritance") != "0"
	res, err := sim.Execute(wl, inheritance)
	if err != nil {
		writeErr(w, http.StatusUnprocessableEntity, "%s", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}

type router struct{}

func (router) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/")
	parts := strings.Split(path, "/")
	switch {
	case r.URL.Path == "/api/health" && r.Method == http.MethodGet:
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	case r.URL.Path == "/api/simulate":
		if r.Method != http.MethodPost {
			writeErr(w, http.StatusMethodNotAllowed, "use POST")
			return
		}
		handleSimulate(w, r)
	case r.URL.Path == "/api/compare":
		if r.Method != http.MethodPost {
			writeErr(w, http.StatusMethodNotAllowed, "use POST")
			return
		}
		handleCompare(w, r)
	case r.URL.Path == "/api/presets":
		handlePresets(w, r)
	case len(parts) == 3 && parts[0] == "api" && parts[1] == "presets" && parts[2] != "":
		handlePreset(w, r, false, parts[2])
	case len(parts) == 4 && parts[0] == "api" && parts[1] == "presets" && parts[2] != "" && parts[3] == "run":
		handlePreset(w, r, true, parts[2])
	default:
		writeErr(w, http.StatusNotFound, "not found; see /api/health and the README")
	}
}

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	flag.Parse()
	log.Printf("priority-inheritance simulator listening on %s", *addr)
	if err := http.ListenAndServe(*addr, router{}); err != nil {
		log.Fatal(err)
	}
}
