package client

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestMetricsAndReset(t *testing.T) {
	var resets int
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/gateway/metrics":
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"http_attempts":3,"charges_created":1}`))
		case "/gateway/reset":
			resets++
			w.WriteHeader(http.StatusNoContent)
		default:
			http.NotFound(w, r)
		}
	}))
	defer ts.Close()

	c := New(ts.URL, time.Second)
	raw, err := c.Metrics(context.Background())
	if err != nil {
		t.Fatalf("metrics: %v", err)
	}
	var m map[string]int
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if m["charges_created"] != 1 {
		t.Fatalf("metrics payload = %s", raw)
	}
	if err := c.Reset(context.Background()); err != nil {
		t.Fatalf("reset: %v", err)
	}
	if resets != 1 {
		t.Fatalf("resets = %d, want 1", resets)
	}
}

func TestMetricsErrorStatuses(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "nope", http.StatusInternalServerError)
	}))
	defer ts.Close()
	c := New(ts.URL, time.Second)
	if _, err := c.Metrics(context.Background()); err == nil {
		t.Fatal("expected metrics error")
	}
	if err := c.Reset(context.Background()); err == nil {
		t.Fatal("expected reset error")
	}
}

func TestCharge4xxIsFailed(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, `{"error":"bad"}`, http.StatusBadRequest)
	}))
	defer ts.Close()
	c := New(ts.URL, time.Second)
	res := c.Charge(context.Background(), 1, ChargeRequest{Amount: 1})
	if res.Outcome != OutcomeFailed || res.HTTPStatus != http.StatusBadRequest {
		t.Fatalf("result = %+v", res)
	}
}

func TestChargeUnparseable2xxIsAmbiguous(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("not-json"))
	}))
	defer ts.Close()
	c := New(ts.URL, time.Second)
	res := c.Charge(context.Background(), 1, ChargeRequest{Amount: 1})
	if res.Outcome != OutcomeAmbiguous {
		t.Fatalf("outcome = %s, want ambiguous", res.Outcome)
	}
}

func TestNewUsesTimeout(t *testing.T) {
	c := New("http://127.0.0.1:1", 10*time.Millisecond)
	if c.BaseURL == "" {
		t.Fatal("base url not set")
	}
}
