// Command server runs the local retry-budget demonstration HTTP service. It
// exposes a structured acceptance-suite endpoint (virtual clock, instant) and
// a live three-tier mesh endpoint driven by named fault presets (real
// loopback HTTP hops, small real backoff). No external systems are contacted.
package main

import (
	"encoding/json"
	"flag"
	"log"
	"net/http"
	"strconv"
	"time"

	"retrybudget/internal/clock"
	"retrybudget/internal/demo"
	"retrybudget/internal/fault"
	"retrybudget/internal/mesh"
	"retrybudget/internal/retry"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	flag.Parse()

	srv := &http.Server{
		Addr:              *addr,
		Handler:           logRequests(newMux()),
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("retry-budget demo listening on http://%s", *addr)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}

func newMux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	mux.HandleFunc("/", index)
	mux.HandleFunc("/suite", suiteHandler)
	mux.HandleFunc("/mesh/", meshHandler)
	return mux
}

func index(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"service": "retrybudget",
		"endpoints": []map[string]string{
			{"method": "GET", "path": "/healthz", "description": "liveness"},
			{"method": "GET", "path": "/suite", "description": "run the built-in acceptance suite (virtual clock), returns structured JSON"},
			{"method": "GET", "path": "/mesh/{preset}?max=&deadline_ms=", "description": "one live call through client->edge->mid->leaf; presets: " + presetNames()},
		},
	})
}

func suiteHandler(w http.ResponseWriter, r *http.Request) {
	results := demo.RunSuite()
	passed, total := 0, len(results)
	for _, c := range results {
		if c.Passed {
			passed++
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"summary": map[string]any{"passed": passed, "total": total, "all_passed": passed == total},
		"cases":   results,
	})
}

// presetDef builds a live three-tier mesh for one named fault scenario.
type presetDef struct {
	desc     string
	script   []fault.Outcome
	max      int
	deadline time.Duration
	edge     int
	mid      int
	client   int
}

func presets() map[string]presetDef {
	return map[string]presetDef{
		"budget-cap": {
			desc:   "leaf always 503; all layers retry; total attempts capped by root budget",
			script: []fault.Outcome{fault.Unavailable(0)}, max: 8,
			edge: 3, mid: 3, client: 3,
		},
		"retry-after": {
			desc:   "leaf 429 Retry-After=1s once, then 200",
			script: []fault.Outcome{fault.TooManyRequests(time.Second), fault.OK()},
			max:    10, edge: 3, mid: 3, client: 3,
		},
		"deadline": {
			desc:   "leaf 429 with 10s Retry-After against a 400ms root deadline",
			script: []fault.Outcome{fault.TooManyRequests(10 * time.Second)},
			max:    20, deadline: 400 * time.Millisecond, edge: 5, mid: 5, client: 5,
		},
		"non-retryable": {
			desc:   "leaf 400 — never replayed",
			script: []fault.Outcome{fault.BadRequest()},
			max:    10, edge: 3, mid: 3, client: 3,
		},
		"success": {
			desc:   "leaf healthy — one chain",
			script: []fault.Outcome{fault.OK()},
			max:    10, edge: 3, mid: 3, client: 3,
		},
	}
}

var presetOrder = []string{"budget-cap", "retry-after", "deadline", "non-retryable", "success"}

func presetNames() string {
	out := ""
	for i, n := range presetOrder {
		if i > 0 {
			out += ", "
		}
		out += n
	}
	return out
}

func meshHandler(w http.ResponseWriter, r *http.Request) {
	name := r.URL.Path[len("/mesh/"):]
	p, ok := presets()[name]
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{
			"error": "unknown preset", "available": presetNames(),
		})
		return
	}

	max := p.max
	deadline := p.deadline
	q := r.URL.Query()
	if v := q.Get("max"); v != "" {
		if parsed, err := strconv.Atoi(v); err == nil && parsed > 0 {
			max = parsed
		}
	}
	if v := q.Get("deadline_ms"); v != "" {
		if parsed, err := strconv.Atoi(v); err == nil && parsed >= 0 {
			if parsed == 0 {
				deadline = 0
			} else {
				deadline = time.Duration(parsed) * time.Millisecond
			}
		}
	}

	// Live mesh: real clock and small real backoff so the endpoint shows
	// genuine loopback HTTP and genuine waits.
	script := fault.NewScript(p.script...)
	m := mesh.New(mesh.Config{
		Clock:       clock.Real{},
		Script:      script,
		EdgeLocal:   p.edge,
		MidLocal:    p.mid,
		ClientLocal: p.client,
		Retry: retry.Config{
			BaseDelay:  20 * time.Millisecond,
			MaxDelay:   200 * time.Millisecond,
			Multiplier: 2,
			Jitter:     0,
		},
	})
	defer m.Close()

	var deadlineAbs time.Time
	if deadline > 0 {
		deadlineAbs = time.Now().Add(deadline)
	}
	reqID := "live-" + name + "-" + strconv.FormatInt(time.Now().UnixNano(), 36)

	cr := mesh.ClientCall(r.Context(), m, reqID, max, deadlineAbs)

	writeJSON(w, http.StatusOK, map[string]any{
		"preset":      name,
		"description": p.desc,
		"result": map[string]any{
			"http_status": cr.StatusCode,
			"verdict":     cr.Verdict,
			"total_used":  cr.Used,
			"root_max":    max,
			"attempts":    cr.Result.Attempts,
			"error":       errString(cr.Result.Err),
		},
		"timeline": m.Recorder().Timeline(reqID),
	})
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Printf("%s %s (%s)", r.Method, r.URL.Path, time.Since(start))
	})
}
