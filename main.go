// Command pip-sim serves a deterministic real-time scheduling simulator with
// the Priority Inheritance Protocol over a small JSON HTTP API. The service
// is backend only: every response is JSON, there is no UI.
package main

import (
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"os"
	"strings"

	"pip-sim/simulator"
)

func main() {
	addr := os.Getenv("ADDR")
	if addr == "" {
		port := os.Getenv("PORT")
		if port == "" {
			port = "8080"
		}
		addr = ":" + port
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/", handleIndex)
	mux.HandleFunc("/healthz", handleHealth)
	mux.HandleFunc("/api/scenarios", handleScenarios)
	mux.HandleFunc("/api/simulate", handleSimulate)
	mux.HandleFunc("/api/compare", handleCompare)
	mux.HandleFunc("/api/demos/", handleDemo) // /api/demos/{id}[/compare|/simulate]

	log.Printf("priority-inheritance simulator listening on %s", addr)
	if err := http.ListenAndServe(addr, withLogging(mux)); err != nil {
		log.Fatal(err)
	}
}

// statusRecorder captures the response status for request logging.
type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

// withLogging is a minimal request logger.
func withLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		log.Printf("%s %s -> %d", r.Method, r.URL.RequestURI(), rec.status)
	})
}

type errBody struct {
	Error string `json:"error"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeErr(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, errBody{Error: err.Error()})
}

// decodeJSON limits body size and rejects unknown fields to surface typos.
func decodeJSON(r *http.Request, dst any) error {
	if r.Body == nil {
		return errors.New("request body is required")
	}
	defer r.Body.Close()
	dec := json.NewDecoder(io.LimitReader(r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return err
	}
	if dec.More() {
		return errors.New("unexpected trailing data in request body")
	}
	return nil
}

func handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeErr(w, http.StatusNotFound, errors.New("not found; see GET / for endpoints"))
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"service": "priority-inheritance scheduling simulator",
		"endpoints": []map[string]string{
			{"method": "GET", "path": "/healthz", "description": "liveness probe"},
			{"method": "GET", "path": "/api/scenarios", "description": "list built-in scenarios"},
			{"method": "POST", "path": "/api/simulate", "description": "run one simulation; body: {enableInheritance, tasks[], maxTime?}"},
			{"method": "POST", "path": "/api/compare", "description": "run workload twice (PIP on/off) and diff blocking times; body: {tasks[], maxTime?}"},
			{"method": "GET", "path": "/api/demos/{id}", "description": "show a built-in scenario (classic|nested-chain|deadlock)"},
			{"method": "GET", "path": "/api/demos/{id}/simulate?inherit=true|false", "description": "run a built-in scenario once"},
			{"method": "GET", "path": "/api/demos/{id}/compare", "description": "run a built-in scenario with and without PIP"},
		},
	})
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeErr(w, http.StatusMethodNotAllowed, errors.New("use GET"))
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func handleScenarios(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeErr(w, http.StatusMethodNotAllowed, errors.New("use GET"))
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"scenarios": simulator.Scenarios()})
}

func handleSimulate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeErr(w, http.StatusMethodNotAllowed, errors.New("use POST"))
		return
	}
	var cfg simulator.Config
	if err := decodeJSON(r, &cfg); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	res, err := simulator.Run(cfg)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func handleCompare(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeErr(w, http.StatusMethodNotAllowed, errors.New("use POST"))
		return
	}
	var req simulator.CompareRequest
	if err := decodeJSON(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	out, err := simulator.Compare(req)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, out)
}

// handleDemo routes /api/demos/{id}, /api/demos/{id}/simulate and
// /api/demos/{id}/compare.
func handleDemo(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeErr(w, http.StatusMethodNotAllowed, errors.New("use GET"))
		return
	}
	rest := strings.TrimPrefix(r.URL.Path, "/api/demos/")
	parts := strings.Split(rest, "/")
	if len(parts) == 0 || parts[0] == "" {
		writeErr(w, http.StatusNotFound, errors.New("scenario id required: classic|nested-chain|deadlock"))
		return
	}
	sc, ok := simulator.FindScenario(parts[0])
	if !ok {
		writeErr(w, http.StatusNotFound, errors.New("unknown scenario: "+parts[0]))
		return
	}
	switch {
	case len(parts) == 1:
		writeJSON(w, http.StatusOK, sc)
	case len(parts) == 2 && parts[1] == "compare":
		out, err := simulator.Compare(simulator.CompareRequest{Tasks: sc.Tasks})
		if err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
		writeJSON(w, http.StatusOK, mapDemo(sc, out))
	case len(parts) == 2 && parts[1] == "simulate":
		inherit := r.URL.Query().Get("inherit") != "false" // default true
		res, err := simulator.Run(simulator.Config{EnableInheritance: inherit, Tasks: sc.Tasks})
		if err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"scenario": sc.ID, "enableInheritance": inherit, "result": res})
	default:
		writeErr(w, http.StatusNotFound, errors.New("unknown demo subpath; use /simulate or /compare"))
	}
}

func mapDemo(sc simulator.Scenario, out simulator.CompareResult) map[string]any {
	return map[string]any{
		"scenario":    sc.ID,
		"name":        sc.Name,
		"description": sc.Description,
		"narrative":   sc.Narrative,
		"comparison":  out,
	}
}
