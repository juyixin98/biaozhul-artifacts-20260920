package chain

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/example/retrybudget/internal/retry"
)

// okHandler is a terminal next-hop that always succeeds.
func okHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":true}`))
	})
}

func newTestLayer(next http.Handler) *Layer {
	return &Layer{
		Name:          "test-layer",
		DefaultBudget: 4,
		Caller:        retry.Caller{},
		Next:          HandlerTransport{Name: "test", Handler: next},
		Store:         retry.NewBudgetStore(),
	}
}

func TestLayerRootBudgetAndSuccess(t *testing.T) {
	layer := newTestLayer(okHandler())
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/call", strings.NewReader("{}"))
	req.Header.Set(retry.HeaderIdempotent, "true")
	layer.ServeHTTP(rec, req)

	if rec.Code != 200 {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"ok":true`) {
		t.Fatalf("body = %s", rec.Body.String())
	}
}

func TestLayerPropagatesBudgetIDAndRemaining(t *testing.T) {
	var gotID, gotRemaining string
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotID = r.Header.Get(retry.HeaderBudgetID)
		gotRemaining = r.Header.Get(retry.HeaderBudgetRemaining)
		w.Write([]byte(`{}`))
	})
	layer := newTestLayer(next)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/call", nil)
	layer.ServeHTTP(rec, req)

	if gotID == "" {
		t.Fatal("budget ID was not propagated downstream")
	}
	if gotRemaining != "3" { // default 4, one consumed by this layer's attempt
		t.Fatalf("remaining = %q, want 3", gotRemaining)
	}
}

func TestLayerMapsBudgetExhaustionTo429(t *testing.T) {
	layer := newTestLayer(okHandler())
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/call", nil)
	req.Header.Set(retry.HeaderBudgetRemaining, "0") // already exhausted upstream
	layer.ServeHTTP(rec, req)

	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want 429", rec.Code)
	}
	var body map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body not JSON: %v", err)
	}
	if body["error"] != "budget_exhausted" {
		t.Fatalf("error = %q, want budget_exhausted", body["error"])
	}
}

func TestLayerSharedStorePreventsAmplification(t *testing.T) {
	rec := NewRecorder()
	failing := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	})
	inner := &Layer{
		Name:          "inner",
		DefaultBudget: 4,
		Caller:        retry.Caller{},
		Next:          HandlerTransport{Name: "inner->leaf", Handler: failing, Recorder: rec},
		Store:         retry.NewBudgetStore(),
	}
	outer := &Layer{
		Name:          "outer",
		DefaultBudget: 4,
		Caller:        retry.Caller{},
		Next:          HandlerTransport{Name: "outer->inner", Handler: inner, Recorder: rec},
		Store:         inner.Store,
	}
	req := httptest.NewRequest(http.MethodGet, "/call", nil)
	req.Header.Set(retry.HeaderIdempotent, "true")
	outer.ServeHTTP(httptest.NewRecorder(), req)

	if total := rec.Total(); total > 4 {
		t.Fatalf("total attempts %d exceeded root budget 4", total)
	}
}
