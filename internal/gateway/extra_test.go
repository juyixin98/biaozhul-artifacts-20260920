package gateway

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"testing"
	"time"
)

func TestMethodNotAllowed(t *testing.T) {
	ts, _ := newTestGateway(t)
	for _, path := range []string{"/gateway/charge", "/gateway/reset"} {
		req, _ := http.NewRequest(http.MethodGet, ts.URL+path, nil)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusMethodNotAllowed {
			t.Fatalf("%s status = %d, want 405", path, resp.StatusCode)
		}
	}
}

func TestBadJSONRejected(t *testing.T) {
	ts, s := newTestGateway(t)
	req, _ := http.NewRequest(http.MethodPost, ts.URL+"/gateway/charge", bytes.NewBufferString("nope"))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("http: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", resp.StatusCode)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.charges) != 0 {
		t.Fatalf("charges=%d, want 0 (bad JSON rejected before charging)", len(s.charges))
	}
}

func TestResetClearsState(t *testing.T) {
	ts, s := newTestGateway(t)
	if _, _, err := postCharge(t, ts, `{"idempotency_key":"KR","amount":1}`, ""); err != nil {
		t.Fatalf("charge: %v", err)
	}
	req, _ := http.NewRequest(http.MethodPost, ts.URL+"/gateway/reset", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("reset: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("reset status = %d", resp.StatusCode)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.attempt != 0 || len(s.charges) != 0 || len(s.byKey) != 0 {
		t.Fatalf("state not reset: %+v", s.charges)
	}
}

func TestResetBeforeChargeDoesNotChargeThenSucceeds(t *testing.T) {
	ts, s := newTestGateway(t)
	body := `{"idempotency_key":"K4","amount":7,"currency":"USD"}`

	// First call: connection dropped BEFORE any charge.
	_, _, err := postCharge(t, ts, body, FaultResetBeforeCharge)
	if err == nil {
		t.Fatal("expected connection error")
	}
	s.mu.Lock()
	if len(s.charges) != 0 {
		s.mu.Unlock()
		t.Fatal("reset-before-charge must not charge")
	}
	s.mu.Unlock()

	// Retry: the one-shot fault is consumed, charge succeeds.
	status, _, err := postCharge(t, ts, body, FaultResetBeforeCharge)
	if err != nil || status != http.StatusCreated {
		t.Fatalf("retry: status=%d err=%v", status, err)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.charges) != 1 {
		t.Fatalf("charges = %d, want 1", len(s.charges))
	}
}

func TestHangFaultTimesOutClient(t *testing.T) {
	ts, s := newTestGateway(t)
	ctx, cancel := context.WithTimeout(context.Background(), 400*time.Millisecond)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, ts.URL+"/gateway/charge",
		bytes.NewBufferString(`{"idempotency_key":"KH","amount":3}`))
	req.Header.Set("X-Fault", FaultHang)
	resp, err := http.DefaultClient.Do(req)
	if err == nil {
		_, _ = io.Copy(io.Discard, resp.Body)
		_ = resp.Body.Close()
		t.Fatal("expected client timeout")
	}
	// Despite no response, the charge was recorded before the hang.
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		s.mu.Lock()
		n := len(s.charges)
		s.mu.Unlock()
		if n == 1 {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("charge was not recorded before hang")
}
