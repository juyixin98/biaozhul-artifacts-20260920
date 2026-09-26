package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func newTestGateway(t *testing.T) (*httptest.Server, *Server) {
	t.Helper()
	s := NewServer(nil)
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(ts.Close)
	return ts, s
}

func postCharge(t *testing.T, ts *httptest.Server, body, fault string) (int, []byte, error) {
	t.Helper()
	req, _ := http.NewRequestWithContext(context.Background(), http.MethodPost, ts.URL+"/gateway/charge", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	if fault != "" {
		req.Header.Set("X-Fault", fault)
	}
	resp, err := (&http.Client{Timeout: time.Second}).Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, raw, nil
}

func TestKeyedChargeDeduplicated(t *testing.T) {
	ts, _ := newTestGateway(t)
	body := `{"idempotency_key":"K1","amount":100,"currency":"USD"}`

	status, raw, err := postCharge(t, ts, body, "")
	if err != nil || status != http.StatusCreated {
		t.Fatalf("first: status=%d err=%v", status, err)
	}
	var first struct {
		Charge Charge `json:"charge"`
	}
	if err := json.Unmarshal(raw, &first); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	for i := 0; i < 3; i++ {
		status, raw, err = postCharge(t, ts, body, "")
		if err != nil || status != http.StatusOK {
			t.Fatalf("retry %d: status=%d err=%v", i, status, err)
		}
		var again struct {
			Charge  Charge `json:"charge"`
			Deduped bool   `json:"deduped"`
		}
		if err := json.Unmarshal(raw, &again); err != nil {
			t.Fatalf("unmarshal retry: %v", err)
		}
		if !again.Deduped || again.Charge.ID != first.Charge.ID {
			t.Fatalf("retry not deduped: %+v", again)
		}
	}

	// GET metrics: exactly one charge despite four HTTP attempts.
	resp, err := http.Get(ts.URL + "/gateway/metrics")
	if err != nil {
		t.Fatalf("metrics: %v", err)
	}
	defer resp.Body.Close()
	var m Metrics
	if err := json.NewDecoder(resp.Body).Decode(&m); err != nil {
		t.Fatalf("decode metrics: %v", err)
	}
	if m.Charges != 1 {
		t.Fatalf("charges = %d, want 1", m.Charges)
	}
	if m.HTTPAttempts != 4 {
		t.Fatalf("http attempts = %d, want 4", m.HTTPAttempts)
	}
}

func TestKeylessChargesAreNotDeduplicated(t *testing.T) {
	ts, _ := newTestGateway(t)
	body := `{"amount":100,"currency":"USD"}`
	for i := 0; i < 3; i++ {
		status, _, err := postCharge(t, ts, body, "")
		if err != nil || status != http.StatusCreated {
			t.Fatalf("charge %d: status=%d err=%v", i, status, err)
		}
	}
	resp, err := http.Get(ts.URL + "/gateway/metrics")
	if err != nil {
		t.Fatalf("metrics: %v", err)
	}
	defer resp.Body.Close()
	var m Metrics
	_ = json.NewDecoder(resp.Body).Decode(&m)
	if m.Charges != 3 {
		t.Fatalf("keyless charges = %d, want 3", m.Charges)
	}
}

func TestResetAfterChargeFaultFiresOnce(t *testing.T) {
	ts, s := newTestGateway(t)
	body := `{"idempotency_key":"K2","amount":50,"currency":"EUR"}`

	// First attempt: connection dropped after charging.
	_, _, err := postCharge(t, ts, body, FaultResetAfterCharge)
	if err == nil {
		t.Fatal("expected connection error on injected reset-after-charge")
	}
	// Retry with the same key: gateway reports the existing charge, no
	// second charge created.
	status, _, err := postCharge(t, ts, body, FaultResetAfterCharge)
	if err != nil || status != http.StatusOK {
		t.Fatalf("retry: status=%d err=%v", status, err)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.charges) != 1 {
		t.Fatalf("charges = %d, want 1", len(s.charges))
	}
	if len(s.faults) != 1 {
		t.Fatalf("faults fired = %d, want 1", len(s.faults))
	}
}

func TestError500DoesNotCharge(t *testing.T) {
	ts, s := newTestGateway(t)
	status, _, err := postCharge(t, ts, `{"idempotency_key":"K3","amount":10}`, FaultError500)
	if err != nil {
		t.Fatalf("unexpected transport error: %v", err)
	}
	if status != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", status)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.charges) != 0 {
		t.Fatalf("charges after 500 = %d, want 0", len(s.charges))
	}
}
