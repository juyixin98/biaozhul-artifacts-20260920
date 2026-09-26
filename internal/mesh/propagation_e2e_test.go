package mesh

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"retrybudget/internal/clock"
	"retrybudget/internal/fault"
	"retrybudget/internal/propagation"
	"retrybudget/internal/report"
	"retrybudget/internal/retry"
)

// TestLeafReceivesPropagatedBudgetHeaders proves the wire contract end to
// end: a root budget created at the client arrives at the leaf as absolute
// deadline, total cap and upstream-used count.
func TestLeafReceivesPropagatedBudgetHeaders(t *testing.T) {
	var got propagation.Values
	probe := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		v, err := propagation.Parse(r.Header)
		if err != nil {
			t.Errorf("leaf parse: %v", err)
		}
		got = v
		w.Header().Set(propagation.HeaderUsed, "4")
		w.Header().Set(headerVerdict, VerdictOK)
		w.WriteHeader(http.StatusOK)
	}))
	defer probe.Close()

	clk := clock.NewAuto(time.Unix(1_700_000_000, 0))
	m := &Mesh{
		cfg: Config{
			Clock:     clk,
			Script:    fault.NewScript(fault.OK()),
			Recorder:  report.NewCollector(),
			EdgeLocal: 1, MidLocal: 1, ClientLocal: 1,
			Retry: retry.Config{BaseDelay: time.Millisecond},
		},
		HTTP: http.DefaultClient,
	}

	// edge -> probe (instead of the usual leaf chain): build one layer whose
	// downstream is the probe and call it through ClientCall by pointing a
	// minimal chain at it.
	layer := httptest.NewServer(NewLayerHandler(LayerConfig{
		Name: "probe-edge", NextURL: probe.URL, Client: http.DefaultClient,
		Clock: clk, Recorder: m.cfg.Recorder,
		Retry: m.cfg.Retry, LocalMax: 1,
	}))
	defer layer.Close()
	m.Edge = layer

	deadline := clk.Now().Add(2 * time.Second)
	cr := ClientCall(context.Background(), m, "hdr-probe", 8, deadline)
	if cr.Result.Err != nil {
		t.Fatalf("call: %v", cr.Result.Err)
	}
	if !got.HasBudget || got.Max != 8 {
		t.Fatalf("leaf saw max=%d has=%v, want 8", got.Max, got.HasBudget)
	}
	// client(1) + edge(1) reservations happened before the leaf contact.
	if got.Used < 2 {
		t.Fatalf("leaf saw used=%d, want upstream usage propagated (>=2)", got.Used)
	}
	if got.RequestID != "hdr-probe" {
		t.Fatalf("request id=%q", got.RequestID)
	}
	if got.Deadline.IsZero() {
		t.Fatal("absolute deadline must be propagated")
	}
}
