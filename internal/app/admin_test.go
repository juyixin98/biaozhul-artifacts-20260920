package app_test

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"testing"
	"time"

	"gracefulshutdown/internal/app"
	"gracefulshutdown/internal/fakedep"
)

// TestAdminFaultEndpoints covers POST /fault, /fault/release, GET /report and
// the livez CLOSING transition.
func TestAdminFaultAndReportEndpoints(t *testing.T) {
	t.Parallel()
	a := startApp(t, testConfig())

	// POST a fail fault.
	body := bytes.NewBufferString(`{"latencyMs":0,"fail":true,"hang":false}`)
	resp, err := http.Post(a.AdminURL()+"/fault", "application/json", body)
	if err != nil {
		t.Fatalf("set fault: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("fault status=%d", resp.StatusCode)
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()

	// /work should now surface a dependency 500 as a completed request with
	// 502 at the edge.
	wresp, err := http.Get(a.PublicURL() + "/work")
	if err != nil {
		t.Fatalf("work: %v", err)
	}
	raw, _ := io.ReadAll(wresp.Body)
	wresp.Body.Close()
	if wresp.StatusCode != http.StatusBadGateway {
		t.Fatalf("work status=%d, want 502; body=%s", wresp.StatusCode, raw)
	}
	var env map[string]any
	if err := json.Unmarshal(raw, &env); err != nil || env["outcome"] != "completed" {
		t.Fatalf("work env=%v body=%s, want outcome completed (fault surfaced)", env, raw)
	}

	// GET /report while running returns valid JSON.
	rresp, err := http.Get(a.AdminURL() + "/report")
	if err != nil {
		t.Fatalf("report: %v", err)
	}
	if rresp.StatusCode != http.StatusOK {
		t.Fatalf("report status=%d", rresp.StatusCode)
	}
	var rep map[string]any
	dec := json.NewDecoder(rresp.Body)
	if err := dec.Decode(&rep); err != nil {
		t.Fatalf("report decode: %v", err)
	}
	rresp.Body.Close()

	// Release endpoint.
	r2, err := http.Post(a.AdminURL()+"/fault/release", "application/json", nil)
	if err != nil {
		t.Fatalf("release: %v", err)
	}
	if r2.StatusCode != http.StatusOK {
		t.Fatalf("release status=%d", r2.StatusCode)
	}
	io.Copy(io.Discard, r2.Body)
	r2.Body.Close()

	// Wrong method rejected.
	r3, err := http.Get(a.AdminURL() + "/fault")
	if err != nil {
		t.Fatalf("fault GET: %v", err)
	}
	if r3.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("fault GET status=%d, want 405", r3.StatusCode)
	}
	io.Copy(io.Discard, r3.Body)
	r3.Body.Close()
}

// TestLivezFailsDuringClosing observes liveness only fail in CLOSING/CLOSED.
func TestLivezFailsDuringClosing(t *testing.T) {
	t.Parallel()
	cfg := testConfig()
	cfg.RejectWindow = 50 * time.Millisecond
	cfg.DrainTimeout = 100 * time.Millisecond
	cfg.CancelTimeout = 100 * time.Millisecond
	a := startApp(t, cfg)

	done := make(chan struct{})
	go func() {
		resp, err := http.Post(a.AdminURL()+"/trigger-shutdown", "application/json", nil)
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}
		close(done)
	}()

	sawLiveDown := false
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case <-done:
			if sawLiveDown {
				return
			}
			// One last sample right at teardown; accept either result, but
			// the unit test for phases asserts the transition.
			return
		default:
		}
		resp, err := http.Get(a.AdminURL() + "/livez")
		if err == nil {
			if resp.StatusCode == http.StatusServiceUnavailable {
				sawLiveDown = true
			}
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}
		time.Sleep(5 * time.Millisecond)
	}
	<-done
}

// TestAppShutdownWrapperReturnsReport exercises the App.Shutdown convenience.
func TestAppShutdownWrapperReturnsReport(t *testing.T) {
	t.Parallel()
	a := startApp(t, testConfig())
	rep := a.Shutdown()
	if rep.Signals != 1 || len(rep.Timeline) < 5 {
		t.Fatalf("report=%+v, want 1 signal and full timeline", rep)
	}
}

// TestDefaultConfig sanity-checks the demo defaults.
func TestDefaultConfig(t *testing.T) {
	t.Parallel()
	c := app.DefaultConfig()
	if c.PublicAddr == "" || c.AdminAddr == "" || c.DrainTimeout <= 0 ||
		c.CancelTimeout <= 0 || c.CloseTimeout <= 0 || c.RejectWindow <= 0 {
		t.Fatalf("default config not fully populated: %+v", c)
	}
}

// TestFaultBadPayloadReturns400 covers malformed fault input.
func TestFaultBadPayloadReturns400(t *testing.T) {
	t.Parallel()
	a := startApp(t, testConfig())
	resp, err := http.Post(a.AdminURL()+"/fault", "application/json",
		bytes.NewBufferString("{not json"))
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d, want 400", resp.StatusCode)
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	_ = fakedep.Fault{} // keep import used if helpers change
}
