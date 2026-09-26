package fakesvc

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
	"time"
)

func start(t *testing.T) *Server {
	t.Helper()
	s := New("svc")
	if err := s.Start(); err != nil {
		t.Fatalf("start: %v", err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = s.Shutdown(ctx)
	})
	return s
}

func get(t *testing.T, url string) *http.Response {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	return resp
}

func TestFake_HealthAndStats(t *testing.T) {
	s := start(t)
	resp := get(t, s.URL()+"/healthz")
	if resp.StatusCode != 200 {
		t.Fatalf("health status %d", resp.StatusCode)
	}
	_ = resp.Body.Close()

	resp = get(t, s.URL()+"/stats")
	var st Stats
	if err := json.NewDecoder(resp.Body).Decode(&st); err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if st.Name != "svc" {
		t.Fatalf("stats name = %q", st.Name)
	}
}

func TestFake_WorkHoldAndComplete(t *testing.T) {
	s := start(t)
	resp := get(t, s.URL()+"/work?hold_ms=20")
	if resp.StatusCode != 200 {
		t.Fatalf("status %d", resp.StatusCode)
	}
	_ = resp.Body.Close()
	st := s.Snapshot()
	if st.Completed != 1 || st.Total != 1 {
		t.Fatalf("counters wrong: %+v", st)
	}
}

func TestFake_ForcedFail(t *testing.T) {
	s := start(t)
	req, _ := http.NewRequest(http.MethodGet, s.URL()+"/work", nil)
	req.Header.Set("X-Fail", "1")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 500 {
		t.Fatalf("status %d, want 500", resp.StatusCode)
	}
	_ = resp.Body.Close()
	if s.Snapshot().Failed != 1 {
		t.Fatal("failed count not incremented")
	}
}

func TestFake_GateReleaseEndpointAndIdempotency(t *testing.T) {
	s := start(t)
	done := make(chan int, 1)
	go func() {
		resp := get(t, s.URL()+"/work?gate=g1")
		done <- resp.StatusCode
		_ = resp.Body.Close()
	}()
	waitFor(t, func() bool { return s.Snapshot().InFlight == 1 })

	resp := get(t, s.URL()+"/admin/release?gate=g1")
	if resp.StatusCode != 200 {
		t.Fatalf("release endpoint status %d", resp.StatusCode)
	}
	_ = resp.Body.Close()
	// Releasing an unknown gate is a safe no-op.
	s.Release("never-registered")

	select {
	case code := <-done:
		if code != 200 {
			t.Fatalf("work status after release %d", code)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("gated work never completed")
	}
}

func TestFake_ReleaseWithoutGateIs400(t *testing.T) {
	s := start(t)
	resp := get(t, s.URL()+"/admin/release")
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status %d, want 400", resp.StatusCode)
	}
	_ = resp.Body.Close()
}

func TestFake_CanceledClientCounted(t *testing.T) {
	s := start(t)
	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, s.URL()+"/work?gate=cancel", nil)
	go func() {
		time.Sleep(20 * time.Millisecond)
		cancel()
	}()
	resp, err := http.DefaultClient.Do(req)
	if err == nil {
		_ = resp.Body.Close()
	}
	waitFor(t, func() bool { return s.Snapshot().Canceled == 1 })
	if s.Snapshot().Canceled != 1 {
		t.Fatal("canceled client not counted")
	}
}

func TestFake_ConnectionCounterDrains(t *testing.T) {
	s := start(t)
	noKeepAlive := &http.Client{Transport: &http.Transport{DisableKeepAlives: true}}
	resp, err := noKeepAlive.Get(s.URL() + "/work")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	waitFor(t, func() bool { return s.Snapshot().ActiveConns == 0 })
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatal("condition not met")
}
