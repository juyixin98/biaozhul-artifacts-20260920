package upstream

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"canceltree/internal/testutil"
)

func TestWorkDelaySucceeds(t *testing.T) {
	f := New()
	defer f.Close()

	resp, err := http.Get(f.URL() + "/work?delay=10ms")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	body, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(body), `"status":"ok"`) {
		t.Fatalf("body = %s", body)
	}
	st := f.Snapshot()
	if st.Completed != 1 || st.Inflight != 0 {
		t.Fatalf("stats = %+v", st)
	}
}

func TestWorkFailInjection(t *testing.T) {
	f := New()
	defer f.Close()
	resp, err := http.Get(f.URL() + "/work?fail=1&status=500")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", resp.StatusCode)
	}
	if st := f.Snapshot(); st.Failed != 1 {
		t.Fatalf("failed = %d, want 1", st.Failed)
	}
}

func TestWorkHoldBlocksUntilReleased(t *testing.T) {
	f := New()
	defer f.ReleaseAll()
	defer f.Close()

	done := make(chan int, 1)
	go func() {
		resp, err := http.Get(f.URL() + "/work?hold=1&hold_id=h1")
		if err != nil {
			done <- -1
			return
		}
		_ = resp.Body.Close()
		done <- resp.StatusCode
	}()

	testutil.Eventually(t, time.Second, func() bool {
		return f.Snapshot().Inflight == 1
	}, "held request should be inflight")

	select {
	case code := <-done:
		t.Fatalf("held request returned early with %d", code)
	case <-time.After(30 * time.Millisecond):
	}

	f.Release("h1")
	select {
	case code := <-done:
		if code != http.StatusOK {
			t.Fatalf("status = %d, want 200 after release", code)
		}
	case <-time.After(time.Second):
		t.Fatal("request did not complete after release")
	}
	if st := f.Snapshot(); st.ActiveHolds != 0 || st.Inflight != 0 {
		t.Fatalf("stats = %+v, want no holds/inflight", st)
	}
}

func TestWorkCountsContextCancellation(t *testing.T) {
	f := New()
	defer f.ReleaseAll()
	defer f.Close()

	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, f.URL()+"/work?hold=1&hold_id=h3", nil)
	errCh := make(chan error, 1)
	go func() {
		resp, err := http.DefaultClient.Do(req)
		if err == nil {
			_ = resp.Body.Close()
		}
		errCh <- err
	}()

	testutil.Eventually(t, time.Second, func() bool {
		return f.Snapshot().Inflight == 1
	}, "request should be held")

	cancel()
	select {
	case err := <-errCh:
		if err == nil {
			t.Fatal("expected a transport error after cancellation")
		}
	case <-time.After(time.Second):
		t.Fatal("request did not abort after cancel")
	}

	testutil.Eventually(t, time.Second, func() bool {
		return f.Snapshot().Canceled >= 1
	}, "upstream should count the canceled call")
	if st := f.Snapshot(); st.Inflight != 0 {
		t.Fatalf("inflight = %d, want 0", st.Inflight)
	}
}

func TestWorkHoldRequiresHoldID(t *testing.T) {
	f := New()
	defer f.Close()
	resp, err := http.Get(f.URL() + "/work?hold=1")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d body=%s", resp.StatusCode, body)
	}
}

func TestWorkBadDurationAndStatusFallBack(t *testing.T) {
	f := New()
	defer f.Close()
	// Garbage delay parses as 0 (immediate), garbage status defaults to 503.
	resp, err := http.Get(f.URL() + "/work?fail=1&status=abc&delay=notaduration")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", resp.StatusCode)
	}
}

func TestReleaseAllAndUnknownRelease(t *testing.T) {
	f := New()
	defer f.Close()
	f.Release("does-not-exist") // must be a safe no-op

	done := make(chan struct{}, 2)
	for _, id := range []string{"x1", "x2"} {
		go func(id string) {
			resp, err := http.Get(f.URL() + "/work?hold=1&hold_id=" + id)
			if err == nil {
				_ = resp.Body.Close()
			}
			done <- struct{}{}
		}(id)
	}
	testutil.Eventually(t, time.Second, func() bool {
		return f.Snapshot().Inflight == 2
	}, "both holds should be inflight")
	f.ReleaseAll()
	for i := 0; i < 2; i++ {
		select {
		case <-done:
		case <-time.After(time.Second):
			t.Fatal("held call did not finish after ReleaseAll")
		}
	}
}

func TestCleanupEndpoint(t *testing.T) {
	f := New()
	defer f.Close()
	resp, err := http.Get(f.URL() + "/cleanup?delay=ignored")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("status = %d, want 204", resp.StatusCode)
	}
	if f.Snapshot().Cleanups != 1 {
		t.Fatalf("cleanups = %d, want 1", f.Snapshot().Cleanups)
	}
}

func TestStatsAndAdminReleaseMethodGuards(t *testing.T) {
	f := New()
	defer f.Close()
	resp, err := http.Get(f.URL() + "/stats")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("stats status = %d", resp.StatusCode)
	}

	// GET on the POST-only release admin endpoint is rejected.
	resp2, err := http.Get(f.URL() + "/admin/release?id=all")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp2.Body.Close()
	if resp2.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("release GET status = %d, want 405", resp2.StatusCode)
	}
}

func TestResetEndpointClosesConnection(t *testing.T) {
	f := New()
	defer f.Close()
	_, err := http.Get(f.URL() + "/reset")
	if err == nil {
		t.Fatal("expected transport error from reset endpoint")
	}
	if f.Snapshot().Failed < 1 {
		t.Fatal("reset endpoint should count its aborted connection as failed")
	}
}
