package main

import (
	"encoding/json"
	"flag"
	"log"
	"net/http"
)

// compare runs the same scenario twice: with and without preemption.
func compare(req SimRequest) (CompareResult, error) {
	with, err := simulate(req, true)
	if err != nil {
		return CompareResult{}, err
	}
	without, err := simulate(req, false)
	if err != nil {
		return CompareResult{}, err
	}
	delta := map[string]float64{}
	withByID := map[string]float64{}
	for _, t := range with.Tasks {
		withByID[t.ID] = t.Completion
	}
	for _, t := range without.Tasks {
		delta[t.ID] = round(t.Completion - withByID[t.ID])
	}
	return CompareResult{WithPreemption: with, WithoutPreemption: without, CompletionDelta: delta}, nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func decodeSimRequest(w http.ResponseWriter, r *http.Request) (SimRequest, bool) {
	var req SimRequest
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return SimRequest{}, false
	}
	if err := validate(&req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return SimRequest{}, false
	}
	return req, true
}

func handleSimulate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeErr(w, http.StatusMethodNotAllowed, "use POST")
		return
	}
	req, ok := decodeSimRequest(w, r)
	if !ok {
		return
	}
	preempt := true
	if req.Preempt != nil {
		preempt = *req.Preempt
	}
	res, err := simulate(req, preempt)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func handleCompare(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeErr(w, http.StatusMethodNotAllowed, "use POST")
		return
	}
	req, ok := decodeSimRequest(w, r)
	if !ok {
		return
	}
	res, err := compare(req)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func newMux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("/api/simulate", handleSimulate)
	mux.HandleFunc("/api/compare", handleCompare)
	return mux
}

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	flag.Parse()
	log.Printf("checkpoint scheduler listening on %s", *addr)
	log.Fatal(http.ListenAndServe(*addr, newMux()))
}
