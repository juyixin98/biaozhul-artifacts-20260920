package faultclient

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"cancelprop/internal/clock"
	"cancelprop/internal/fakesvc"
)

func startService(t *testing.T) *fakesvc.Server {
	t.Helper()
	s := fakesvc.New("test")
	if err := s.Start(); err != nil {
		t.Fatalf("start fake service: %v", err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = s.Shutdown(ctx)
	})
	return s
}

func TestClient_Success(t *testing.T) {
	svc := startService(t)
	c := New("c", svc.URL(), clock.NewRealClock())
	res, err := c.Call(context.Background(), "/work")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.StatusCode != 200 || !strings.Contains(res.Body, `"service":"test"`) {
		t.Fatalf("unexpected result: %+v body=%s", res, res.Body)
	}
}

func TestClient_InjectedRequestFailure(t *testing.T) {
	svc := startService(t)
	c := New("c", svc.URL(), clock.NewRealClock())
	_, err := c.CallWith(context.Background(), "/work", Faults{FailRequest: true})
	if !errors.Is(err, ErrInjected) {
		t.Fatalf("err = %v, want ErrInjected", err)
	}
	if got := svc.Snapshot().Total; got != 0 {
		t.Fatalf("service received %d requests, want 0 for a client-side fault", got)
	}
}

func TestClient_Downstream500(t *testing.T) {
	svc := startService(t)
	c := New("c", svc.URL(), clock.NewRealClock())
	res, err := c.Call(context.Background(), "/work") // no fault yet
	if err != nil {
		t.Fatalf("baseline call failed: %v", err)
	}
	_ = res
	res, err = c.CallWith(context.Background(), "/work", Faults{ForceDownstreamFail: true})
	if err == nil {
		t.Fatal("expected error for forced 500")
	}
	if res == nil || res.StatusCode != 500 {
		t.Fatalf("result = %+v, want status 500 captured", res)
	}
	if got := svc.Snapshot().Failed; got != 1 {
		t.Fatalf("service failed count = %d, want 1", got)
	}
}

func TestClient_FakeClockTimeout(t *testing.T) {
	svc := startService(t)
	fc := clock.NewFakeClock(time.Now())
	c := New("c", svc.URL(), fc)

	// hold_ms on the server uses the REAL clock (server is a real HTTP
	// server). Use a gate instead so it blocks indefinitely, then drive the
	// client's fake-clock timeout.
	done := make(chan error, 1)
	go func() {
		_, err := c.CallWith(context.Background(), "/work?gate=g", Faults{Timeout: 50 * time.Millisecond})
		done <- err
	}()

	waitInflight(t, svc, 1)
	// Nothing fired yet: the call must still be blocked.
	select {
	case err := <-done:
		t.Fatalf("call returned before timeout: %v", err)
	default:
	}
	fc.Advance(50 * time.Millisecond)
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("expected timeout error, got nil")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("call did not return after fake-clock timeout")
	}
	// The server side must observe the canceled request.
	waitCanceled(t, svc, 1)
}

func TestClient_ContextCancelPropagatesToServer(t *testing.T) {
	svc := startService(t)
	c := New("c", svc.URL(), clock.NewRealClock())
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, err := c.Call(ctx, "/work?gate=g")
		done <- err
	}()
	waitInflight(t, svc, 1)
	cancel() // caller gives up
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("expected error after context cancel")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("call did not return after cancel")
	}
	waitCanceled(t, svc, 1)
}

func TestClient_DelayCanceled(t *testing.T) {
	svc := startService(t)
	fc := clock.NewFakeClock(time.Now())
	c := New("c", svc.URL(), fc)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, err := c.CallWith(ctx, "/work", Faults{DelayBefore: time.Hour})
		done <- err
	}()
	waitPendingTimers(t, fc, 1)
	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("expected cancellation error during injected delay")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("delay did not unblock on cancel")
	}
}

func TestClient_SetAndSnapshotFaults(t *testing.T) {
	svc := startService(t)
	c := New("c", svc.URL(), clock.NewRealClock())
	c.SetFaults(Faults{FailRequest: true})
	if !c.SnapshotFaults().FailRequest {
		t.Fatal("faults not stored")
	}
	if _, err := c.Call(context.Background(), "/work"); !errors.Is(err, ErrInjected) {
		t.Fatalf("global faults not applied: %v", err)
	}
	c.SetFaults(Faults{})
	c.CloseIdleConns()
}

func waitInflight(t *testing.T, svc *fakesvc.Server, want int64) {
	t.Helper()
	wait(t, func() bool { return svc.Snapshot().InFlight == want })
}

func waitCanceled(t *testing.T, svc *fakesvc.Server, want int64) {
	t.Helper()
	wait(t, func() bool { return svc.Snapshot().Canceled >= want })
}

func waitPendingTimers(t *testing.T, fc *clock.FakeClock, want int) {
	t.Helper()
	wait(t, func() bool { return fc.PendingTimers() == want })
}

func wait(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatal("condition not met within 2s")
}
