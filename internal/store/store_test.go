package store_test

import (
	"testing"

	"alertfsm/internal/model"
	"alertfsm/internal/store"
)

func TestSnapshotRoundTrip(t *testing.T) {
	dir := t.TempDir()

	st, err := store.New(dir)
	if err != nil {
		t.Fatal(err)
	}
	st.SetClockFresh(12345)
	st.Lock()
	st.PutRuleLocked(model.Rule{ID: "r1", Metric: "m", Threshold: 1, Direction: "above",
		PendingFor: 1000, RecoveryFor: 2000, NoDataFor: 3000, Enabled: true})
	st.PutStateLocked(model.State{RuleID: "r1", Metric: "m", Status: model.StatusAlerting,
		EnteredAtMS: 100, LastSeenMS: 200, LastValue: 9, WatermarkMS: 300})
	st.AddSampleLocked(model.Sample{Metric: "m", TSMS: 200, Value: 9})
	st.AddEventLocked(model.Event{TSMS: 300, RuleID: "r1", Metric: "m", Type: model.EventFiring, To: model.StatusAlerting})
	st.Unlock()
	if err := st.Save(); err != nil {
		t.Fatal(err)
	}

	// Reopen: everything must survive.
	st2, err := store.New(dir)
	if err != nil {
		t.Fatal(err)
	}
	if st2.Clock() != 12345 {
		t.Fatalf("clock=%d", st2.Clock())
	}
	st2.Lock()
	r, ok := st2.GetRuleLocked("r1")
	st2.Unlock()
	if !ok || r.PendingFor != 1000 || r.NoDataFor != 3000 {
		t.Fatalf("rule not restored: %+v ok=%v", r, ok)
	}
	if got := st2.ListSamples("m", 0, 0); len(got) != 1 {
		t.Fatalf("samples not restored: %+v", got)
	}
	if evs := st2.ListEvents(0, "", 0); len(evs) != 1 || evs[0].Type != model.EventFiring {
		t.Fatalf("events not restored: %+v", evs)
	}
}

func TestDuplicateSampleFirstWins(t *testing.T) {
	st, err := store.New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	st.Lock()
	dup1 := st.AddSampleLocked(model.Sample{Metric: "m", TSMS: 10, Value: 1})
	dup2 := st.AddSampleLocked(model.Sample{Metric: "m", TSMS: 10, Value: 2})
	// Out-of-order insertion stays sorted.
	st.AddSampleLocked(model.Sample{Metric: "m", TSMS: 5, Value: 0})
	st.AddSampleLocked(model.Sample{Metric: "m", TSMS: 20, Value: 3})
	st.Unlock()

	if dup1 || !dup2 {
		t.Fatalf("duplicate flags: %v %v", dup1, dup2)
	}
	got := st.ListSamples("m", 0, 0)
	if len(got) != 3 {
		t.Fatalf("want 3 unique samples, got %+v", got)
	}
	if got[0].TSMS != 5 || got[1].TSMS != 10 || got[2].TSMS != 20 {
		t.Fatalf("not sorted: %+v", got)
	}
	if got[1].Value != 1 {
		t.Fatalf("duplicate overwrote first value: %+v", got[1])
	}
}
