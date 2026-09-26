package app_test

import (
	"bufio"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"gracefulshutdown/internal/app"
	"gracefulshutdown/internal/fakedep"
)

func testConfig() app.Config {
	return app.Config{
		PublicAddr:    "127.0.0.1:0",
		AdminAddr:     "127.0.0.1:0",
		RejectWindow:  300 * time.Millisecond,
		DrainTimeout:  900 * time.Millisecond,
		CancelTimeout: 900 * time.Millisecond,
		CloseTimeout:  time.Second,
	}
}

func startApp(t *testing.T, cfg app.Config) *app.App {
	t.Helper()
	a := app.New(cfg)
	if err := a.Start(); err != nil {
		t.Fatalf("start: %v", err)
	}
	return a
}

// getEnvelope decodes a JSON response body.
func getEnvelope(t *testing.T, url string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	env := map[string]any{}
	_ = json.Unmarshal(raw, &env)
	return resp.StatusCode, env
}

func TestReadyAndLiveProbesAreSplitDuringShutdown(t *testing.T) {
	t.Parallel()
	cfg := testConfig()
	cfg.DrainTimeout = 2 * time.Second // long drain window to observe split probes
	cfg.RejectWindow = 500 * time.Millisecond
	a := startApp(t, cfg)

	// Before shutdown: both probes healthy.
	if code, env := getEnvelope(t, a.AdminURL()+"/readyz"); code != 200 || env["ready"] != true {
		t.Fatalf("readyz before shutdown: code=%d env=%v", code, env)
	}
	if code, env := getEnvelope(t, a.AdminURL()+"/livez"); code != 200 || env["alive"] != true {
		t.Fatalf("livez before shutdown: code=%d env=%v", code, env)
	}

	// A hung request keeps shutdown in DRAINING.
	a.SetFault(fakedep.Fault{Hang: true})
	workDone := make(chan struct{})
	go func() {
		_, _ = http.Get(a.PublicURL() + "/work")
		close(workDone)
	}()
	time.Sleep(150 * time.Millisecond)

	triggerDone := make(chan struct{})
	go func() {
		resp, err := http.Post(a.AdminURL()+"/trigger-shutdown", "application/json", nil)
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}
		close(triggerDone)
	}()

	// Observe: readiness fails (503) while liveness still reports alive=true.
	sawReadyDownLiveUp := false
	deadline := time.Now().Add(2500 * time.Millisecond)
	for time.Now().Before(deadline) {
		rr, err := http.Get(a.AdminURL() + "/readyz")
		readyDown := err == nil && rr.StatusCode == 503
		if rr != nil {
			io.Copy(io.Discard, rr.Body)
			rr.Body.Close()
		}
		lr, err2 := http.Get(a.AdminURL() + "/livez")
		liveUp := err2 == nil && lr.StatusCode == 200
		var liveEnv map[string]any
		if lr != nil {
			raw, _ := io.ReadAll(lr.Body)
			_ = json.Unmarshal(raw, &liveEnv)
			lr.Body.Close()
		}
		if readyDown && liveUp && liveEnv["phase"] != "CLOSING" && liveEnv["phase"] != "CLOSED" {
			sawReadyDownLiveUp = true
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !sawReadyDownLiveUp {
		t.Fatal("never observed readiness=failing while liveness=up during drain")
	}

	select {
	case <-triggerDone:
	case <-time.After(5 * time.Second):
		t.Fatal("shutdown trigger did not return")
	}
	<-workDone
}

func TestLongRequestsCompleteDuringDrain(t *testing.T) {
	t.Parallel()
	cfg := testConfig()
	cfg.RejectWindow = 100 * time.Millisecond
	a := startApp(t, cfg)
	a.SetFault(fakedep.Fault{Latency: 200 * time.Millisecond})

	type result struct {
		code int
		out  string
	}
	results := make(chan result, 3)
	for i := 0; i < 3; i++ {
		go func() {
			resp, err := http.Get(a.PublicURL() + "/work")
			if err != nil {
				results <- result{0, "request-error"}
				return
			}
			defer resp.Body.Close()
			var env map[string]any
			raw, _ := io.ReadAll(resp.Body)
			_ = json.Unmarshal(raw, &env)
			out, _ := env["outcome"].(string)
			results <- result{resp.StatusCode, out}
		}()
	}
	time.Sleep(120 * time.Millisecond) // requests in flight

	resp, err := http.Post(a.AdminURL()+"/trigger-shutdown", "application/json", nil)
	if err != nil {
		t.Fatalf("trigger: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()

	for i := 0; i < 3; i++ {
		r := <-results
		if r.code != http.StatusOK || r.out != "completed" {
			t.Fatalf("request %d: code=%d outcome=%s, want 200/completed", i, r.code, r.out)
		}
	}

	var rep map[string]any
	if err := json.Unmarshal(body, &rep); err != nil {
		t.Fatalf("report: %v", err)
	}
	if rep["completed"].(float64) != 3 {
		t.Fatalf("completed=%v, want 3", rep["completed"])
	}
	if rep["cancelled"].(float64) != 0 {
		t.Fatalf("cancelled=%v, want 0", rep["cancelled"])
	}
	if rep["inFlightAtClose"].(float64) != 0 {
		t.Fatalf("inFlightAtClose=%v, want 0", rep["inFlightAtClose"])
	}
}

func TestHungRequestStreamAndJobAreExplicitlyCancelled(t *testing.T) {
	t.Parallel()
	cfg := testConfig()
	cfg.RejectWindow = 100 * time.Millisecond
	a := startApp(t, cfg)
	a.SetFault(fakedep.Fault{Hang: true})

	// Hung long request.
	longDone := make(chan string, 1)
	go func() {
		resp, err := http.Get(a.PublicURL() + "/work")
		if err != nil {
			longDone <- "request-error"
			return
		}
		defer resp.Body.Close()
		var env map[string]any
		raw, _ := io.ReadAll(resp.Body)
		_ = json.Unmarshal(raw, &env)
		out, _ := env["outcome"].(string)
		longDone <- out
	}()

	// Streaming request.
	streamDone := make(chan string, 1)
	go func() {
		resp, err := http.Get(a.PublicURL() + "/stream?duration=30s")
		if err != nil {
			streamDone <- "request-error"
			return
		}
		defer resp.Body.Close()
		last := ""
		sc := bufio.NewScanner(resp.Body)
		for sc.Scan() {
			line := sc.Text()
			if strings.HasPrefix(line, "event:") {
				last = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
			}
		}
		streamDone <- last
	}()

	// Long-running background job.
	bgResp, err := http.Post(a.PublicURL()+"/bg?duration=30s", "application/json", nil)
	if err != nil {
		t.Fatalf("bg: %v", err)
	}
	io.Copy(io.Discard, bgResp.Body)
	bgResp.Body.Close()

	time.Sleep(150 * time.Millisecond)

	resp, err := http.Post(a.AdminURL()+"/trigger-shutdown", "application/json", nil)
	if err != nil {
		t.Fatalf("trigger: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()

	select {
	case out := <-longDone:
		if out != "cancelled" {
			t.Fatalf("long request outcome=%s, want cancelled", out)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("hung long request never resolved")
	}
	select {
	case last := <-streamDone:
		if last != "cancelled" {
			t.Fatalf("stream last event=%s, want cancelled", last)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("stream never ended with a terminal event")
	}

	var rep struct {
		Cancelled           int `json:"cancelled"`
		Completed           int `json:"completed"`
		BackgroundSpawned   int `json:"backgroundSpawned"`
		BackgroundAfterStop int `json:"backgroundSpawnedAfterStopAccept"`
		InFlightAtClose     int `json:"inFlightAtClose"`
		CloseOrder          []struct {
			Name  string `json:"name"`
			Order int    `json:"order"`
		} `json:"closeOrder"`
	}
	if err := json.Unmarshal(body, &rep); err != nil {
		t.Fatalf("decode report: %v", err)
	}
	if rep.Cancelled != 3 { // hung long request + stream + background job
		t.Fatalf("cancelled=%d, want 3 (long+stream+job)", rep.Cancelled)
	}
	if rep.BackgroundSpawned != 1 || rep.BackgroundAfterStop != 0 {
		t.Fatalf("bg spawned=%d afterStop=%d, want 1/0", rep.BackgroundSpawned, rep.BackgroundAfterStop)
	}
	if rep.InFlightAtClose != 0 {
		t.Fatalf("inFlightAtClose=%d, want 0", rep.InFlightAtClose)
	}
	wantOrder := []string{"admin-http-listener", "http-client", "fake-external-dependency"}
	if len(rep.CloseOrder) != 3 {
		t.Fatalf("closeOrder=%v, want 3 resources", rep.CloseOrder)
	}
	for i, want := range wantOrder {
		if rep.CloseOrder[i].Name != want || rep.CloseOrder[i].Order != i+1 {
			t.Fatalf("closeOrder[%d]=%+v, want %s/%d", i, rep.CloseOrder[i], want, i+1)
		}
	}
}

func TestNewWorkAndBackgroundRejectedAfterStopAccept(t *testing.T) {
	t.Parallel()
	cfg := testConfig()
	cfg.RejectWindow = 2 * time.Second // wide window so 503s are observable
	cfg.DrainTimeout = 3 * time.Second
	a := startApp(t, cfg)
	a.SetFault(fakedep.Fault{Hang: true})

	workDone := make(chan struct{})
	go func() {
		_, _ = http.Get(a.PublicURL() + "/work")
		close(workDone)
	}()
	time.Sleep(150 * time.Millisecond)

	go func() {
		resp, err := http.Post(a.AdminURL()+"/trigger-shutdown", "application/json", nil)
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}
	}()

	// Wait for readiness to flip (phase 1), then attempt new work inside the
	// reject window: foreground and background requests must get explicit 503.
	waitReady503(t, a.AdminURL())

	r1, err := http.Get(a.PublicURL() + "/work")
	if err != nil {
		t.Fatalf("post-cut work request: %v", err)
	}
	body1, _ := io.ReadAll(r1.Body)
	r1.Body.Close()
	if r1.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("post-cut work status=%d, want 503", r1.StatusCode)
	}

	r2, err := http.Post(a.PublicURL()+"/bg?duration=5s", "application/json", nil)
	if err != nil {
		t.Fatalf("post-cut bg request: %v", err)
	}
	body2, _ := io.ReadAll(r2.Body)
	r2.Body.Close()
	if r2.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("post-cut bg status=%d, want 503", r2.StatusCode)
	}

	// Both rejections must be explicit, structured envelopes.
	for name, raw := range map[string][]byte{"work": body1, "bg": body2} {
		var env map[string]any
		if err := json.Unmarshal(raw, &env); err != nil || env["outcome"] != "rejected" {
			t.Fatalf("%s rejection body=%s, want outcome=rejected", name, raw)
		}
	}

	// Wait for the full shutdown sequence to end (resources closed) so no
	// listeners leak across parallel tests.
	select {
	case <-a.Coordinator().Done():
	case <-time.After(6 * time.Second):
		t.Fatal("shutdown did not finish after rejecting late work")
	}
	<-workDone
}

func TestRepeatedSignalsAreCountedAndShutdownStillCompletes(t *testing.T) {
	t.Parallel()
	cfg := testConfig()
	a := startApp(t, cfg)
	a.SetFault(fakedep.Fault{Hang: true})

	longDone := make(chan struct{})
	go func() {
		resp, err := http.Get(a.PublicURL() + "/work")
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}
		close(longDone)
	}()
	time.Sleep(150 * time.Millisecond)

	resp, err := http.Post(a.AdminURL()+"/trigger-shutdown?signals=3", "application/json", nil)
	if err != nil {
		t.Fatalf("trigger: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()

	select {
	case <-longDone:
	case <-time.After(5 * time.Second):
		t.Fatal("hung request did not resolve after repeated-signal shutdown")
	}

	var rep struct {
		Signals   int `json:"signalsReceived"`
		Cancelled int `json:"cancelled"`
		Timeline  []struct {
			Phase string `json:"phase"`
		} `json:"timeline"`
	}
	if err := json.Unmarshal(body, &rep); err != nil {
		t.Fatalf("decode report: %v\nbody: %s", err, body)
	}
	if rep.Signals != 3 {
		t.Fatalf("signals=%d, want 3", rep.Signals)
	}
	if len(rep.Timeline) < 5 {
		t.Fatalf("timeline len=%d, want all 5 phases", len(rep.Timeline))
	}
	last := rep.Timeline[len(rep.Timeline)-1].Phase
	if last != "CLOSED" {
		t.Fatalf("last phase=%s, want CLOSED", last)
	}
	if rep.Cancelled < 1 {
		t.Fatalf("cancelled=%d, want >=1 (hung request escalated then cancelled)", rep.Cancelled)
	}
}

func waitReady503(t *testing.T, adminURL string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := http.Get(adminURL + "/readyz")
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if resp.StatusCode == http.StatusServiceUnavailable {
				return
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("readiness never flipped to 503")
}
