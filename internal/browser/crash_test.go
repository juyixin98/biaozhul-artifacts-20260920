package browser

import (
	"context"
	"net/http/httptest"
	"testing"
	"time"

	"sitevitals/internal/demo"
	"sitevitals/internal/models"
	"sitevitals/internal/whitelist"
)

// TestCollect_BrowserCrash_KillProcess kills the Chromium process while a
// navigation is in flight: the collection must return BROWSER_CRASH (not a
// timeout), and after Restart the instance must serve a new collection. This
// is the "browser exit is distinguished and the browser resource recovered"
// guarantee.
func TestCollect_BrowserCrash_KillProcess(t *testing.T) {
	srv := httptest.NewServer(demo.NewServer())
	defer srv.Close()

	inst := NewInstance(Options{Headless: true})
	startCtx, startCancel := context.WithTimeout(context.Background(), 30*time.Second)
	if err := inst.Start(startCtx); err != nil {
		startCancel()
		t.Skipf("chromium unavailable: %v", err)
	}
	startCancel()
	t.Cleanup(func() { _ = inst.Close() })

	prof, _ := whitelist.Profile(models.ViewportDesktop)
	matcher := whitelist.NewMatcher([]models.Site{
		{Name: "demo", Origin: srv.URL, PathPrefix: "/", Enabled: true},
	})
	c := NewCollector(inst, false)

	collectDone := make(chan error, 1)
	go func() {
		_, _, err := c.Collect(context.Background(), CollectOptions{
			TargetURL: srv.URL + "/hang", Profile: prof, Matcher: matcher,
			NavTimeout: 20 * time.Second, Settle: time.Second,
		})
		collectDone <- err
	}()

	// Let the navigation start, then kill the Chromium process outright.
	time.Sleep(800 * time.Millisecond)
	inst.mu.Lock()
	proc := inst.cmd
	inst.mu.Unlock()
	if proc == nil || proc.Process == nil {
		t.Fatal("no launched process to kill")
	}
	if err := proc.Process.Kill(); err != nil {
		t.Fatalf("kill chrome: %v", err)
	}

	select {
	case err := <-collectDone:
		ce, ok := AsCollectError(err)
		if !ok || ce.Code != CodeBrowserCrash {
			t.Fatalf("expected BROWSER_CRASH, got %v", err)
		}
	case <-time.After(8 * time.Second):
		t.Fatal("collection did not return promptly after browser kill")
	}

	// Crashed channel must have fired, and Restart brings a fresh browser.
	select {
	case <-inst.Crashed():
	default:
		t.Fatal("Crashed channel did not fire")
	}
	restartCtx, rc := context.WithTimeout(context.Background(), 30*time.Second)
	defer rc()
	if err := inst.Restart(restartCtx); err != nil {
		t.Fatalf("restart after crash: %v", err)
	}

	// The recovered instance must fully serve another collection.
	ctx2, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	m, _, err := NewCollector(inst, false).Collect(ctx2, CollectOptions{
		TargetURL: srv.URL + "/normal", Profile: prof, Matcher: matcher,
		NavTimeout: 15 * time.Second, Settle: time.Second,
	})
	if err != nil {
		t.Fatalf("collection after restart: %v", err)
	}
	if m.FCPMS == nil {
		t.Fatal("metrics not collected after browser restart")
	}
}
