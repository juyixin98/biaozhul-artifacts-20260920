package fakeupstream

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"cbhalfopen/internal/vclock"
)

func TestImmediateModes(t *testing.T) {
	vc := vclock.NewVirtual(time.UnixMilli(0).UTC())
	cases := []struct {
		name   string
		mode   Mode
		status int
		err    error
	}{
		{"ok", ModeOK, http.StatusOK, nil},
		{"fail", ModeFail, http.StatusInternalServerError, nil},
		{"error", ModeError, 0, ErrInjected},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := New(vc, tc.mode)
			status, err := f.Call(context.Background())
			if status != tc.status {
				t.Fatalf("status = %d, want %d", status, tc.status)
			}
			if !errors.Is(err, tc.err) {
				t.Fatalf("err = %v, want %v", err, tc.err)
			}
		})
	}
}

func TestSetModeAppliesToSubsequentCalls(t *testing.T) {
	vc := vclock.NewVirtual(time.UnixMilli(0).UTC())
	f := New(vc, ModeOK)
	if _, err := f.Call(context.Background()); err != nil {
		t.Fatalf("first call: %v", err)
	}
	f.SetMode(ModeFail)
	if status, err := f.Call(context.Background()); err != nil || status != http.StatusInternalServerError {
		t.Fatalf("after SetMode: status=%d err=%v", status, err)
	}
	if got := f.Mode(); got != ModeFail {
		t.Fatalf("mode = %s", got)
	}
}

func TestHangBlocksUntilReleased(t *testing.T) {
	vc := vclock.NewVirtual(time.UnixMilli(0).UTC())
	f := New(vc, ModeHang)

	type callResult struct {
		status int
		err    error
	}
	resCh := make(chan callResult, 1)
	go func() {
		s, e := f.Call(context.Background())
		resCh <- callResult{s, e}
	}()

	waitForPending(t, f, 1)
	if ids := f.Pending(); len(ids) != 1 || ids[0] != 1 {
		t.Fatalf("pending = %v, want [1]", ids)
	}

	if !f.Release(ModeOK) {
		t.Fatal("Release returned false")
	}
	res := <-resCh
	if res.status != http.StatusOK || res.err != nil {
		t.Fatalf("released result = %+v", res)
	}
	attempts := f.Attempts()
	if len(attempts) != 1 || attempts[0].ReleasedAs != ModeOK || attempts[0].StatusCode != http.StatusOK {
		t.Fatalf("attempt log = %+v", attempts)
	}
}

func TestReleaseSpecificIDAndReleaseAll(t *testing.T) {
	vc := vclock.NewVirtual(time.UnixMilli(0).UTC())
	f := New(vc, ModeHang)

	// Start one call at a time and wait for its registration before starting
	// the next: goroutine scheduling does not otherwise guarantee that the
	// fake-assigned IDs follow launch order.
	var chs []chan int
	for i := 0; i < 3; i++ {
		ch := make(chan int, 1)
		go func() {
			status, _ := f.Call(context.Background())
			ch <- status
		}()
		waitForPending(t, f, i+1)
		chs = append(chs, ch)
	}
	c1, c2, c3 := chs[0], chs[1], chs[2]

	// Release the newest by ID; oldest-first default is unaffected.
	if !f.Release(ModeFail, 3) {
		t.Fatal("release id=3 returned false")
	}
	if got := <-c3; got != http.StatusInternalServerError {
		t.Fatalf("call 3 status = %d, want 500", got)
	}
	if ids := f.Pending(); len(ids) != 2 {
		t.Fatalf("pending after targeted release = %v", ids)
	}

	if n := f.ReleaseAll(ModeOK); n != 2 {
		t.Fatalf("ReleaseAll = %d, want 2", n)
	}
	if got := <-c1; got != http.StatusOK {
		t.Fatalf("call 1 status = %d, want 200", got)
	}
	if got := <-c2; got != http.StatusOK {
		t.Fatalf("call 2 status = %d, want 200", got)
	}
}

func TestReleaseWithNothingPending(t *testing.T) {
	vc := vclock.NewVirtual(time.UnixMilli(0).UTC())
	f := New(vc, ModeOK)
	if f.Release(ModeOK) {
		t.Fatal("Release returned true with no pending calls")
	}
	if n := f.ReleaseAll(ModeOK); n != 0 {
		t.Fatalf("ReleaseAll = %d, want 0", n)
	}
}

func TestCallerCancelDoesNotFinishAttemptUntilRelease(t *testing.T) {
	vc := vclock.NewVirtual(time.UnixMilli(0).UTC())
	f := New(vc, ModeHang)

	ctx, cancel := context.WithCancel(context.Background())
	errCh := make(chan error, 1)
	go func() {
		_, err := f.Call(ctx)
		errCh <- err
	}()
	waitForPending(t, f, 1)

	cancel()
	select {
	case err := <-errCh:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("canceled call err = %v, want context.Canceled", err)
		}
	case <-time.After(time.Second):
		t.Fatal("canceled call did not return")
	}

	// The server-side attempt is still parked until explicitly released.
	waitForPending(t, f, 1)
	f.Release(ModeFail)
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if len(f.Pending()) == 0 {
			attempts := f.Attempts()
			last := attempts[len(attempts)-1]
			if last.ReleasedAs != ModeFail {
				t.Fatalf("attempt finalized as %s", last.ReleasedAs)
			}
			return
		}
	}
	t.Fatal("parked attempt never finalized after release")
}

func TestServeHTTPModes(t *testing.T) {
	vc := vclock.NewVirtual(time.UnixMilli(0).UTC())
	f := New(vc, ModeOK)
	srv := httptest.NewServer(f)
	defer srv.Close()

	resp, err := http.Get(srv.URL)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	resp.Body.Close()

	f.SetMode(ModeFail)
	resp, err = http.Get(srv.URL)
	if err != nil {
		t.Fatalf("get fail mode: %v", err)
	}
	if resp.StatusCode != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", resp.StatusCode)
	}
	resp.Body.Close()
}

func TestServeHTTPHangIgnoresClientCancelUntilRelease(t *testing.T) {
	vc := vclock.NewVirtual(time.UnixMilli(0).UTC())
	f := New(vc, ModeHang)
	srv := httptest.NewServer(f)
	defer srv.Close()

	done := make(chan struct{})
	go func() {
		req, _ := http.NewRequest(http.MethodGet, srv.URL, nil)
		_, _ = http.DefaultClient.Do(req) // nolint:bodyclose // expected to be interrupted
		close(done)
	}()
	waitForPending(t, f, 1)

	// A client giving up does not resolve the server-side attempt.
	// httptest keeps the handler parked; release it and let the goroutine end.
	if !f.Release(ModeOK) {
		t.Fatal("release failed")
	}
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("request goroutine still parked after release")
	}
}

func waitForPending(t *testing.T, f *Fake, n int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if len(f.Pending()) >= n {
			return
		}
	}
	t.Fatalf("only %d of %d calls parked", len(f.Pending()), n)
}
