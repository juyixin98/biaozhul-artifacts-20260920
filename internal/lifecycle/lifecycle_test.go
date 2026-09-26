package lifecycle_test

import (
	"encoding/json"
	"testing"
	"time"

	"gracefulshutdown/internal/lifecycle"
)

func TestReportJSONIsValidAndContainsCounters(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 9, 25, 10, 0, 0, 0, time.UTC)
	r := lifecycle.Report{
		StartedAt: now, FinishedAt: now.Add(time.Second), DurationMS: 1000,
		Signals: 2, Completed: 1, Cancelled: 2, Rejected: 3,
		Timeline:   []lifecycle.Event{{Phase: lifecycle.PhaseStopAccept, At: now}},
		CloseOrder: []lifecycle.CloseRecord{{Name: "x", Order: 1, At: now}},
	}
	raw, err := r.JSON()
	if err != nil {
		t.Fatalf("json: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got["signalsReceived"].(float64) != 2 {
		t.Fatalf("signalsReceived=%v", got["signalsReceived"])
	}
	if got["completed"].(float64) != 1 || got["cancelled"].(float64) != 2 {
		t.Fatalf("counters wrong: %v", got)
	}
}

func TestOrderedPhases(t *testing.T) {
	t.Parallel()
	want := []lifecycle.Phase{
		lifecycle.PhaseStopAccept,
		lifecycle.PhaseDraining,
		lifecycle.PhaseCancelling,
		lifecycle.PhaseClosing,
		lifecycle.PhaseClosed,
	}
	for i, p := range want {
		if lifecycle.OrderedPhases[i] != p {
			t.Fatalf("phase %d=%s want %s", i, lifecycle.OrderedPhases[i], p)
		}
	}
}
