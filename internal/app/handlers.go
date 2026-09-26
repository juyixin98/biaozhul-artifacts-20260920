package app

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"gracefulshutdown/internal/fakedep"
	"gracefulshutdown/internal/lifecycle"
	"gracefulshutdown/internal/shutdown"
)

// replyJSON writes a JSON envelope.
func replyJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// handleWork is a "long request": it calls the (fake) external dependency and
// waits for the result. No response headers are written until the call returns,
// so a phase-3 cancellation can still produce an explicit result for the
// client instead of a dropped connection.
func (a *App) handleWork(w http.ResponseWriter, r *http.Request) {
	id := a.newID("work")
	h, ok := a.coord.Begin(id, "long")
	if !ok {
		replyJSON(w, http.StatusServiceUnavailable, map[string]any{
			"id": id, "outcome": string(lifecycle.OutcomeRejected),
			"error": "server is shutting down; new work is not accepted",
		})
		return
	}

	ctx, cancel := a.workContext(r)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, a.dep.URL()+"/work", nil)
	if err != nil {
		a.coord.Finish(h, lifecycle.OutcomeCancelled, "build request: "+err.Error())
		replyJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	resp, err := a.client.Do(req)
	if err != nil {
		outcome, detail := classifyDepError(ctx, err)
		a.coord.Finish(h, outcome, detail)
		replyJSON(w, statusForOutcome(outcome), map[string]any{
			"id": id, "outcome": string(outcome), "error": detail,
		})
		return
	}
	defer resp.Body.Close()

	// The dependency may answer (e.g. 499 client-gone) exactly as our context
	// is cancelled in phase 3; the request context is authoritative.
	if ctx.Err() != nil {
		detail := fmt.Sprintf("in-flight call cancelled during shutdown (dep status %d)", resp.StatusCode)
		a.coord.Finish(h, lifecycle.OutcomeCancelled, detail)
		replyJSON(w, http.StatusServiceUnavailable, map[string]any{
			"id": id, "outcome": string(lifecycle.OutcomeCancelled), "error": detail,
		})
		return
	}

	if resp.StatusCode >= 500 {
		detail := fmt.Sprintf("dependency returned HTTP %d", resp.StatusCode)
		a.coord.Finish(h, lifecycle.OutcomeCompleted, detail)
		replyJSON(w, http.StatusBadGateway, map[string]any{
			"id": id, "outcome": string(lifecycle.OutcomeCompleted),
			"note": "request finished; dependency reported an injected fault", "error": detail,
		})
		return
	}

	a.coord.Finish(h, lifecycle.OutcomeCompleted, "dependency responded 200")
	replyJSON(w, http.StatusOK, map[string]any{
		"id": id, "outcome": string(lifecycle.OutcomeCompleted),
		"dependencyStatus": resp.StatusCode,
	})
}

// handleStream is a streaming (SSE) request. It commits HTTP 200 immediately and
// emits ticks; on phase-3 cancellation it emits an explicit terminal event.
func (a *App) handleStream(w http.ResponseWriter, r *http.Request) {
	id := a.newID("stream")
	h, ok := a.coord.Begin(id, "stream")
	if !ok {
		replyJSON(w, http.StatusServiceUnavailable, map[string]any{
			"id": id, "outcome": string(lifecycle.OutcomeRejected),
			"error": "server is shutting down; new streams are not accepted",
		})
		return
	}

	duration := 5 * time.Second
	if d := r.URL.Query().Get("duration"); d != "" {
		if parsed, perr := time.ParseDuration(d); perr == nil {
			duration = parsed
		}
	}

	ctx, cancel := a.workContext(r)
	defer cancel()

	flusher, _ := w.(http.Flusher)
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(http.StatusOK)
	fmt.Fprintf(w, "event: open\ndata: {\"id\":%q}\n\n", id)
	if flusher != nil {
		flusher.Flush()
	}

	deadline := time.NewTimer(duration)
	defer deadline.Stop()
	ticker := time.NewTicker(200 * time.Millisecond)
	defer ticker.Stop()

	ticks := 0
	for {
		select {
		case <-ctx.Done():
			outcome := lifecycle.OutcomeCancelled
			if errors.Is(r.Context().Err(), context.Canceled) && a.workRoot.Err() == nil {
				outcome = lifecycle.OutcomeCancelled
			}
			fmt.Fprintf(w, "event: cancelled\ndata: {\"id\":%q,\"ticks\":%d,\"reason\":\"shutdown phase CANCELLING\"}\n\n", id, ticks)
			if flusher != nil {
				flusher.Flush()
			}
			a.coord.Finish(h, outcome, "stream cut by shutdown cancellation")
			return
		case <-deadline.C:
			fmt.Fprintf(w, "event: done\ndata: {\"id\":%q,\"ticks\":%d}\n\n", id, ticks)
			if flusher != nil {
				flusher.Flush()
			}
			a.coord.Finish(h, lifecycle.OutcomeCompleted, fmt.Sprintf("stream sent %d ticks", ticks))
			return
		case t := <-ticker.C:
			ticks++
			fmt.Fprintf(w, "event: tick\ndata: {\"n\":%d,\"at\":%q}\n\n", ticks, t.Format(time.RFC3339Nano))
			if flusher != nil {
				flusher.Flush()
			}
		}
	}
}

// handleBackground spawns one detached background job. Spawning is gated on the
// coordinator: once STOP_ACCEPT begins the request gets HTTP 503 and no job is
// created, guaranteeing no new background tasks after traffic is cut.
func (a *App) handleBackground(w http.ResponseWriter, r *http.Request) {
	id := a.newID("bgjob")
	h, ok := a.coord.BeginBackground(id)
	if !ok {
		replyJSON(w, http.StatusServiceUnavailable, map[string]any{
			"id": id, "outcome": string(lifecycle.OutcomeRejected),
			"error": "shutdown in progress; background tasks are not admitted",
		})
		return
	}

	duration := 1 * time.Second
	if d := r.URL.Query().Get("duration"); d != "" {
		if parsed, perr := time.ParseDuration(d); perr == nil {
			duration = parsed
		}
	}

	replyJSON(w, http.StatusAccepted, map[string]any{
		"id": id, "outcome": "accepted", "duration": duration.String(),
	})

	// Detached job: survives the spawning HTTP request, tracked by coordinator.
	go a.runBackgroundJob(h, duration)
}

func (a *App) runBackgroundJob(h shutdownHandle, duration time.Duration) {
	deadline := time.NewTimer(duration)
	defer deadline.Stop()
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-a.workRoot.Done():
			a.coord.FinishBackground(h, lifecycle.OutcomeCancelled,
				"background job cancelled in phase CANCELLING")
			return
		case <-deadline.C:
			a.coord.FinishBackground(h, lifecycle.OutcomeCompleted,
				"background job finished its scheduled work")
			return
		case <-ticker.C:
		}
	}
}

// handleReady is the readiness probe. It fails the instant STOP_ACCEPT begins.
func (a *App) handleReady(w http.ResponseWriter, _ *http.Request) {
	if a.coord.Phase() != lifecycle.PhaseRunning {
		replyJSON(w, http.StatusServiceUnavailable, map[string]any{
			"ready": false, "phase": string(a.coord.Phase()),
		})
		return
	}
	resp, err := a.client.Get(a.dep.URL() + "/health")
	ready := err == nil && resp.StatusCode == http.StatusOK
	if resp != nil {
		_ = resp.Body.Close()
	}
	if !ready {
		replyJSON(w, http.StatusServiceUnavailable, map[string]any{"ready": false, "reason": "dependency not ready"})
		return
	}
	replyJSON(w, http.StatusOK, map[string]any{"ready": true, "phase": string(a.coord.Phase())})
}

// handleLive is the liveness probe. It stays up for the whole draining process
// and only fails once resources are being torn down in CLOSING.
func (a *App) handleLive(w http.ResponseWriter, _ *http.Request) {
	p := a.coord.Phase()
	if p == lifecycle.PhaseClosing || p == lifecycle.PhaseClosed {
		replyJSON(w, http.StatusServiceUnavailable, map[string]any{"alive": false, "phase": string(p)})
		return
	}
	replyJSON(w, http.StatusOK, map[string]any{"alive": true, "phase": string(p)})
}

// handleReport returns the live structured report as JSON.
func (a *App) handleReport(w http.ResponseWriter, _ *http.Request) {
	rep := a.coord.Report()
	replyJSON(w, http.StatusOK, rep)
}

// handleTrigger fires shutdown signal(s) over HTTP (demos without OS signals).
// Repeated calls or signals=N model duplicate signals. The connection is
// hijacked BEFORE signalling: a hijacked connection is tracked by neither
// Shutdown nor Close, so when phase 4 closes the admin listener it neither
// waits on nor tears down this request. The handler then waits for the
// finalized report and writes it raw over the hijacked connection.
func (a *App) handleTrigger(w http.ResponseWriter, r *http.Request) {
	n := 1
	if s := r.URL.Query().Get("signals"); s != "" {
		if v, err := strconv.Atoi(s); err == nil && v > 0 {
			n = v
		}
	}

	hj, ok := w.(http.Hijacker)
	if !ok {
		// Non-hijackable transport: fall back to signalling and responding,
		// accepting that this connection may itself keep Shutdown waiting up
		// to the grace period (the force-close then drops it).
		for i := 0; i < n; i++ {
			a.coord.NotifySignal()
		}
		rep := <-a.coord.SubscribeReport()
		replyJSON(w, http.StatusOK, rep)
		return
	}
	conn, brw, err := hj.Hijack()
	if err != nil {
		for i := 0; i < n; i++ {
			a.coord.NotifySignal()
		}
		rep := <-a.coord.SubscribeReport()
		replyJSON(w, http.StatusOK, rep)
		return
	}
	defer conn.Close()

	needAck := a.coord.ExpectFinalizeAck()
	sub := a.coord.SubscribeReport()
	for i := 0; i < n; i++ {
		a.coord.NotifySignal()
	}
	rep := <-sub
	body, jerr := rep.JSON()
	if jerr != nil {
		body = []byte(`{"error":"failed to encode report"}`)
	}
	fmt.Fprintf(brw, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"+
		"Content-Length: %d\r\nConnection: close\r\n\r\n", len(body))
	_, _ = brw.Write(body)
	_ = brw.Flush()
	if needAck {
		a.coord.FinalizeAck()
	}
}

// SetFault is a convenience for tests/demos to inject faults without an HTTP API.
func (a *App) SetFault(f fakedep.Fault) { a.dep.SetFault(f) }

// handleFault sets the fake dependency fault: POST {"latencyMs":100,"fail":false,"hang":false}.
func (a *App) handleFault(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		replyJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
		return
	}
	var body struct {
		LatencyMS int64 `json:"latencyMs"`
		Fail      bool  `json:"fail"`
		Hang      bool  `json:"hang"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		replyJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	f := fakedep.Fault{Fail: body.Fail, Hang: body.Hang}
	if body.LatencyMS > 0 {
		f.Latency = time.Duration(body.LatencyMS) * time.Millisecond
	}
	a.dep.SetFault(f)
	replyJSON(w, http.StatusOK, map[string]any{
		"latencyMs": body.LatencyMS, "fail": body.Fail, "hang": body.Hang,
	})
}

// handleFaultRelease unblocks hanging fake-dependency calls.
func (a *App) handleFaultRelease(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		replyJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
		return
	}
	a.dep.ReleaseHangs()
	replyJSON(w, http.StatusOK, map[string]string{"released": "ok"})
}

func classifyDepError(ctx context.Context, err error) (lifecycle.Outcome, string) {
	if ctx.Err() != nil {
		return lifecycle.OutcomeCancelled,
			fmt.Sprintf("in-flight call cancelled during shutdown: %v", err)
	}
	return lifecycle.OutcomeCompleted, fmt.Sprintf("dependency call failed: %v", err)
}

func statusForOutcome(o lifecycle.Outcome) int {
	if o == lifecycle.OutcomeCancelled {
		return http.StatusServiceUnavailable
	}
	return http.StatusBadGateway
}

// shutdownHandle is an alias so the background job signature stays decoupled.
type shutdownHandle = shutdown.Handle
