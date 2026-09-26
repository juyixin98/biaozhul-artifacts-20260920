// Command faultclient is the fault-injection client. It drives a running
// server through the acceptance scenario — long requests, streaming
// requests and repeated shutdown signals interleaved — and emits a
// structured JSON report of every check. Exit code is non-zero if any
// check fails.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// Check is one verified expectation.
type Check struct {
	Name   string `json:"name"`
	Passed bool   `json:"passed"`
	Detail string `json:"detail,omitempty"`
}

// RequestResult captures the outcome of one in-flight request.
type RequestResult struct {
	Label    string `json:"label"`
	Status   int    `json:"status"`
	Body     string `json:"body"`
	Duration string `json:"duration"`
	Err      string `json:"err,omitempty"`
}

// State mirrors the server's /state payload (only the fields we assert on).
type State struct {
	Phase            string `json:"phase"`
	Accepting        bool   `json:"accepting"`
	InFlight         int64  `json:"in_flight"`
	DrainTimedOut    bool   `json:"drain_timed_out"`
	ShutdownSignals  uint64 `json:"shutdown_signals"`
	RejectedRequests uint64 `json:"rejected_requests"`
	RejectedTasks    uint64 `json:"rejected_tasks"`
	PhaseLog         []struct {
		Phase string `json:"phase"`
	} `json:"phase_log"`
	CloseOrder []struct {
		Order int    `json:"order"`
		Name  string `json:"name"`
	} `json:"close_order"`
	Ledger []struct {
		ID      uint64 `json:"id"`
		Path    string `json:"path"`
		Kind    string `json:"kind"`
		Outcome string `json:"outcome"`
	} `json:"ledger"`
}

// Report is the structured test result.
type Report struct {
	Scenario  string          `json:"scenario"`
	StartedAt time.Time       `json:"started_at"`
	EndedAt   time.Time       `json:"ended_at"`
	Checks    []Check         `json:"checks"`
	Requests  []RequestResult `json:"requests"`
	Final     *State          `json:"final_state,omitempty"`
	Passed    bool            `json:"passed"`
}

func main() {
	base := flag.String("base", "http://127.0.0.1:8080", "server base URL")
	out := flag.String("out", "", "optional path to write the JSON report")
	flag.Parse()

	r := &runner{base: *base, client: &http.Client{Timeout: 30 * time.Second}}
	r.report.Scenario = "graceful-shutdown acceptance: long+stream requests interleaved with repeated shutdown signals"
	r.report.StartedAt = time.Now()
	r.run()
	r.report.EndedAt = time.Now()
	r.report.Passed = true
	for _, c := range r.report.Checks {
		if !c.Passed {
			r.report.Passed = false
			break
		}
	}

	data, _ := json.MarshalIndent(r.report, "", "  ")
	fmt.Println(string(data))
	if *out != "" {
		if err := os.WriteFile(*out, data, 0o644); err != nil {
			fmt.Fprintf(os.Stderr, "write report: %v\n", err)
		}
	}
	if !r.report.Passed {
		os.Exit(1)
	}
}

type runner struct {
	base   string
	client *http.Client
	report Report
	mu     sync.Mutex
}

func (r *runner) check(name string, passed bool, detail string) {
	r.report.Checks = append(r.report.Checks, Check{Name: name, Passed: passed, Detail: detail})
}

func (r *runner) get(path string) (int, []byte, error) {
	resp, err := r.client.Get(r.base + path)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	return resp.StatusCode, body, err
}

func (r *runner) post(path string) (int, []byte, error) {
	resp, err := r.client.Post(r.base+path, "application/json", bytes.NewReader(nil))
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	return resp.StatusCode, body, err
}

// fire starts a request in the background and records its result.
func (r *runner) fire(wg *sync.WaitGroup, label, path string) {
	wg.Add(1)
	go func() {
		defer wg.Done()
		start := time.Now()
		code, body, err := r.get(path)
		res := RequestResult{Label: label, Status: code, Duration: time.Since(start).Round(time.Millisecond).String()}
		if err != nil {
			res.Err = err.Error()
		}
		if len(body) > 400 {
			res.Body = string(body[:400]) + "...(truncated)"
		} else {
			res.Body = strings.TrimSpace(string(body))
		}
		r.mu.Lock()
		r.report.Requests = append(r.report.Requests, res)
		r.mu.Unlock()
	}()
}

func (r *runner) run() {
	// 1. Liveness and readiness are separate and both healthy before shutdown.
	code, _, err := r.get("/healthz")
	r.check("liveness_ok_before_shutdown", err == nil && code == 200, fmt.Sprintf("GET /healthz -> %d", code))
	code, _, err = r.get("/readyz")
	r.check("readiness_ok_before_shutdown", err == nil && code == 200, fmt.Sprintf("GET /readyz -> %d", code))

	// 2. Interleave in-flight work with shutdown signals:
	//    - a long request that fits in the drain window (must complete)
	//    - a streaming request that fits (must complete)
	//    - a too-long request (must be explicitly cancelled)
	//    - a background task (must complete during drain)
	var wg sync.WaitGroup
	r.fire(&wg, "long-request", "/work?ms=800&q=long")
	r.fire(&wg, "stream-request", "/stream?chunks=4&interval_ms=150")
	r.fire(&wg, "too-long-request", "/work?ms=30000&q=stuck")
	code, _, err = r.post("/task?msg=pre-shutdown-job")
	r.check("background_task_accepted_before_shutdown", err == nil && code == 202, fmt.Sprintf("POST /task -> %d", code))

	time.Sleep(300 * time.Millisecond) // let the requests get accepted

	// 3. Repeated shutdown signals must be idempotent.
	code, body, err := r.post("/shutdown")
	r.check("first_shutdown_signal_accepted", err == nil && code == 202, fmt.Sprintf("POST /shutdown -> %d", code))
	code2, _, err2 := r.post("/shutdown")
	r.check("second_shutdown_signal_idempotent", err2 == nil && code2 == 202, fmt.Sprintf("POST /shutdown again -> %d", code2))
	_ = body

	// 4. After stop-accepting: readiness flips, liveness stays, no new work.
	time.Sleep(100 * time.Millisecond)
	code, _, _ = r.get("/readyz")
	r.check("readiness_fails_after_stop_accepting", code == 503, fmt.Sprintf("GET /readyz -> %d", code))
	code, _, _ = r.get("/healthz")
	r.check("liveness_still_ok_after_stop_accepting", code == 200, fmt.Sprintf("GET /healthz -> %d", code))
	code, _, _ = r.get("/work?ms=10")
	r.check("new_request_rejected_after_stop_accepting", code == 503, fmt.Sprintf("GET /work -> %d", code))
	code, _, _ = r.post("/task?msg=late-job")
	r.check("no_new_background_tasks_after_stop_accepting", code == 503, fmt.Sprintf("POST /task -> %d", code))

	// 5. Wait for in-flight requests to resolve.
	wg.Wait()
	byLabel := map[string]RequestResult{}
	for _, res := range r.report.Requests {
		byLabel[res.Label] = res
	}
	longReq := byLabel["long-request"]
	r.check("long_request_completed", longReq.Status == 200 && strings.Contains(longReq.Body, `"completed"`),
		fmt.Sprintf("status=%d body=%s", longReq.Status, longReq.Body))
	streamReq := byLabel["stream-request"]
	streamOK := streamReq.Status == 200 && strings.Contains(streamReq.Body, "chunk 4/4")
	r.check("stream_request_completed", streamOK, fmt.Sprintf("status=%d body=%q", streamReq.Status, streamReq.Body))
	stuck := byLabel["too-long-request"]
	r.check("too_long_request_explicitly_cancelled", stuck.Status == 499 && strings.Contains(stuck.Body, `"cancelled"`),
		fmt.Sprintf("status=%d body=%s", stuck.Status, stuck.Body))

	// 6. Wait for the closing phase to finish, then inspect final state.
	var final State
	deadline := time.Now().Add(20 * time.Second)
	for {
		code, body, err := r.get("/state")
		if err == nil && code == 200 {
			_ = json.Unmarshal(body, &final)
			if final.Phase == "done" {
				break
			}
		}
		if time.Now().After(deadline) {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	r.report.Final = &final
	r.check("shutdown_reached_done_phase", final.Phase == "done", fmt.Sprintf("phase=%s", final.Phase))
	r.check("drain_timed_out_as_expected", final.DrainTimedOut, fmt.Sprintf("drain_timed_out=%v", final.DrainTimedOut))
	r.check("shutdown_signals_counted", final.ShutdownSignals >= 2, fmt.Sprintf("signals=%d", final.ShutdownSignals))
	r.check("rejected_requests_counted", final.RejectedRequests >= 1, fmt.Sprintf("rejected=%d", final.RejectedRequests))
	r.check("rejected_tasks_counted", final.RejectedTasks >= 1, fmt.Sprintf("rejected=%d", final.RejectedTasks))

	// 7. Phase order must be exactly the four phases, each once.
	var phases []string
	for _, p := range final.PhaseLog {
		phases = append(phases, p.Phase)
	}
	want := []string{"receiving", "draining", "cancelling", "closing", "done"}
	r.check("phase_sequence_exact", strings.Join(phases, ",") == strings.Join(want, ","),
		fmt.Sprintf("phases=%v", phases))

	// 8. Resources closed in LIFO order: fake-queue (registered last) first.
	lifo := len(final.CloseOrder) == 2 &&
		final.CloseOrder[0].Name == "fake-queue" && final.CloseOrder[0].Order == 1 &&
		final.CloseOrder[1].Name == "fake-db" && final.CloseOrder[1].Order == 2
	r.check("resources_closed_in_lifo_order", lifo, fmt.Sprintf("close_order=%+v", final.CloseOrder))

	// 9. Every accepted request has a terminal outcome: completed or cancelled.
	allTerminal := len(final.Ledger) > 0
	completed, cancelled := 0, 0
	for _, e := range final.Ledger {
		switch e.Outcome {
		case "completed":
			completed++
		case "cancelled":
			cancelled++
		default:
			allTerminal = false
		}
	}
	r.check("all_accepted_work_has_terminal_outcome", allTerminal,
		fmt.Sprintf("ledger=%d entries, completed=%d cancelled=%d", len(final.Ledger), completed, cancelled))
	r.check("both_outcomes_observed", completed > 0 && cancelled > 0,
		fmt.Sprintf("completed=%d cancelled=%d", completed, cancelled))
}
