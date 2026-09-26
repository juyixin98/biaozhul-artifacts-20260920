package client

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestChargeSuccess(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Idempotency-Key") != "K9" {
			t.Errorf("missing forwarded key, got %q", r.Header.Get("Idempotency-Key"))
		}
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"charge":  map[string]any{"id": "ch_1", "amount": 100, "currency": "USD"},
			"deduped": false,
		})
	}))
	defer ts.Close()

	c := New(ts.URL, time.Second)
	var traced int
	c.Trace = func(a Attempt) { traced++ }

	res := c.Charge(context.Background(), 1, ChargeRequest{Amount: 100, Currency: "USD", IdempotencyKey: "K9"})
	if res.Outcome != OutcomeSuccess || res.Charge == nil || res.Charge.ID != "ch_1" {
		t.Fatalf("result = %+v", res)
	}
	if traced != 1 {
		t.Fatalf("trace callbacks = %d, want 1", traced)
	}
}

func TestChargeClassifies500AsFailed(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, `{"error":"boom"}`, http.StatusInternalServerError)
	}))
	defer ts.Close()

	c := New(ts.URL, time.Second)
	res := c.Charge(context.Background(), 1, ChargeRequest{Amount: 1})
	if res.Outcome != OutcomeFailed || res.Err == nil {
		t.Fatalf("result = %+v", res)
	}
}

func TestChargeClassifiesResetAsAmbiguous(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if hj, ok := w.(http.Hijacker); ok {
			conn, _, _ := hj.Hijack()
			_ = conn.Close()
			return
		}
	}))
	defer ts.Close()

	c := New(ts.URL, 500*time.Millisecond)
	res := c.Charge(context.Background(), 1, ChargeRequest{Amount: 1})
	if res.Outcome != OutcomeAmbiguous {
		t.Fatalf("outcome = %s, want ambiguous", res.Outcome)
	}
}

func TestChargeClassifiesHangAsAmbiguous(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(2 * time.Second)
		_, _ = w.Write([]byte("{}"))
	}))
	defer ts.Close()

	c := New(ts.URL, 150*time.Millisecond)
	start := time.Now()
	res := c.Charge(context.Background(), 1, ChargeRequest{Amount: 1})
	if res.Outcome != OutcomeAmbiguous {
		t.Fatalf("outcome = %s, want ambiguous", res.Outcome)
	}
	if time.Since(start) > time.Second {
		t.Fatalf("client did not honor timeout: %s", time.Since(start))
	}
}
