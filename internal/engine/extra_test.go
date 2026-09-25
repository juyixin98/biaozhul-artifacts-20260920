package engine_test

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/example/hysteresis-alerter/internal/engine"
)

// TestTickTo covers forward jump evaluation and backward rejection.
func TestTickTo(t *testing.T) {
	e := newTestEngine()
	r := baseRule("r1", "m")
	r.NoDataFor = engine.Duration{Duration: time.Minute}
	if err := e.CreateRule(r); err != nil {
		t.Fatal(err)
	}
	target := tc.Add(time.Minute)
	evs, err := e.TickTo(target)
	if err != nil {
		t.Fatal(err)
	}
	if len(evs) != 1 || evs[0].Type != engine.EventNodata {
		t.Fatalf("TickTo over no-data boundary: %v", typesOf(evs))
	}
	if !e.Now().Equal(target) {
		t.Fatalf("clock=%s want %s", e.Now(), target)
	}
	// No-op when equal.
	if evs, err := e.TickTo(target); err != nil || evs != nil {
		t.Fatalf("TickTo equal: evs=%v err=%v", evs, err)
	}
	// Backward rejected.
	if _, err := e.TickTo(target.Add(-time.Second)); err == nil {
		t.Fatal("TickTo backward must fail")
	}
}

// TestSnapshotRoundTripInEngine exercises Export/Restore directly.
func TestSnapshotRoundTripInEngine(t *testing.T) {
	e := newTestEngine()
	if err := e.CreateRule(baseRule("r1", "cpu")); err != nil {
		t.Fatal(err)
	}
	send(e, tc, "cpu", 90)

	snap := e.Export()
	if !snap.ClockNow.Equal(tc) || len(snap.Rules) != 1 || len(snap.Samples["cpu"]) != 1 {
		t.Fatalf("unexpected snapshot: %+v", snap)
	}

	e2 := newTestEngine()
	if err := e2.Restore(snap); err != nil {
		t.Fatal(err)
	}
	if !e2.Now().Equal(tc) {
		t.Fatalf("restored clock=%s", e2.Now())
	}
	if stateOf(e2, "r1") != engine.StatePending {
		t.Fatal("restored state must be pending")
	}
	// Restore must reject nil.
	if err := e2.Restore(nil); err == nil {
		t.Fatal("nil snapshot must error")
	}

	// Invalid rule in snapshot must error; unknown state entry is replaced
	// with a fresh runtime state.
	bad := &engine.Snapshot{
		Rules: []engine.Rule{{ID: "x", Metric: "m", Operator: "?", Threshold: 1}},
	}
	if err := e2.Restore(bad); err == nil {
		t.Fatal("invalid rule in snapshot must error")
	}
	zeroClock := &engine.Snapshot{
		ClockNow: time.Time{},
		Rules:    []engine.Rule{baseRule("fresh", "g")},
		States:   map[string]engine.RuntimeState{"fresh": {State: "bogus"}},
	}
	e3 := newTestEngine()
	if err := e3.Restore(zeroClock); err != nil {
		t.Fatal(err)
	}
	if !e3.Now().Equal(engine.Epoch) {
		t.Fatalf("zero clock must restore as Epoch, got %s", e3.Now())
	}
	if stateOf(e3, "fresh") != engine.StateInactive {
		t.Fatal("bogus persisted state must reset to inactive")
	}
}

// TestListAndQueryFilters covers ListRules, event since/rule filters and
// sample range/limit filtering.
func TestListAndQueryFilters(t *testing.T) {
	e := newTestEngine()
	r1 := baseRule("r1", "cpu")
	r2 := baseRule("r2", "cpu")
	r2.Threshold = 50
	if err := e.CreateRule(r1); err != nil {
		t.Fatal(err)
	}
	if err := e.CreateRule(r2); err != nil {
		t.Fatal(err)
	}
	if got := e.ListRules(); len(got) != 2 || got[0].Rule.ID != "r1" || got[1].Rule.ID != "r2" {
		t.Fatalf("ListRules order wrong: %+v", got)
	}
	send(e, tc, "cpu", 90)
	send(e, tc.Add(60*time.Second), "cpu", 90) // both rules fire
	if _, ok := e.GetRule("nope"); ok {
		t.Fatal("GetRule on missing id must report false")
	}

	// rule_id filter
	if evs := e.QueryEvents("r1", true, nil); len(evs) != 1 || evs[0].RuleID != "r1" {
		t.Fatalf("rule filter returned %+v", evs)
	}
	// since filter (boundary inclusive-exclusive semantics: Before(since))
	since := tc.Add(60 * time.Second)
	evs := e.QueryEvents("", true, &since)
	for _, ev := range evs {
		if ev.TS.Before(since) {
			t.Fatalf("since filter leaked event at %s", ev.TS)
		}
	}
	if len(evs) != 2 {
		t.Fatalf("since filter count=%d want 2", len(evs))
	}

	// sample range filter + limit, newest first
	from := tc.Add(30 * time.Second)
	to := tc.Add(90 * time.Second)
	send(e, tc.Add(120*time.Second), "cpu", 90)
	got := e.QuerySamples("cpu", &from, &to, 10)
	if len(got) != 1 || !got[0].TS.Equal(tc.Add(60*time.Second)) {
		t.Fatalf("range query=%+v want only t+60s", got)
	}
	if got := e.QuerySamples("cpu", nil, nil, 1); len(got) != 1 || !got[0].TS.Equal(tc.Add(120*time.Second)) {
		t.Fatalf("limit-1 newest query=%+v", got)
	}
	if got := e.QuerySamples("missing-metric", nil, nil, 0); len(got) != 0 {
		t.Fatalf("unknown metric must return empty, got %+v", got)
	}
}

// TestNodataResumeHotChains: a hot sample resuming from nodata emits
// data_resumed then fires immediately when trigger_for=0.
func TestNodataResumeHotChains(t *testing.T) {
	e := newTestEngine()
	r := engine.Rule{
		ID: "r1", Metric: "m", Operator: engine.OpGreaterThan, Threshold: 10,
		TriggerFor: engine.Duration{}, RecoverFor: engine.Duration{},
		NoDataFor: engine.Duration{Duration: time.Minute},
	}
	if err := e.CreateRule(r); err != nil {
		t.Fatal(err)
	}
	send(e, tc, "m", 1)
	tick(e, time.Minute) // nodata
	evs := send(e, tc.Add(time.Minute), "m", 99)
	if got := typesOf(evs); len(got) != 2 || got[0] != engine.EventDataResumed || got[1] != engine.EventFiring {
		t.Fatalf("hot resume chain: %v want [data_resumed firing]", got)
	}
	if stateOf(e, "r1") != engine.StateFiring {
		t.Fatal("chained state must end in firing")
	}
}

// TestSampleRetentionCap verifies only the newest samples are retained.
func TestSampleRetentionCap(t *testing.T) {
	e := newTestEngine()
	items := make([]engine.IngestItem, 0, 1005)
	for i := 0; i < 1005; i++ {
		items = append(items, engine.IngestItem{
			Metric: "m", TS: tc.Add(time.Duration(i) * time.Second), Value: float64(i),
		})
	}
	e.Ingest(items)
	got := e.QuerySamples("m", nil, nil, 0)
	if len(got) != 1000 {
		t.Fatalf("retained=%d want 1000", len(got))
	}
	// Newest retained = index 1004; oldest retained = index 5.
	if got[len(got)-1].Value != 5 || got[0].Value != 1004 {
		t.Fatalf("retention window wrong: oldest=%v newest=%v", got[len(got)-1].Value, got[0].Value)
	}
}

// TestDurationAndTimeJSON exercises the custom JSON types.
func TestDurationAndTimeJSON(t *testing.T) {
	var d engine.Duration
	if err := json.Unmarshal([]byte(`"90s"`), &d); err != nil || d.Duration != 90*time.Second {
		t.Fatalf("string duration: %v %s", err, d.Duration)
	}
	if err := json.Unmarshal([]byte(`120`), &d); err != nil || d.Duration != 2*time.Minute {
		t.Fatalf("numeric duration: %v %s", err, d.Duration)
	}
	if err := json.Unmarshal([]byte(`-5`), &d); err == nil {
		t.Fatal("negative numeric duration must error")
	}
	if err := json.Unmarshal([]byte(`"nonsense"`), &d); err == nil {
		t.Fatal("bad duration string must error")
	}
	if err := json.Unmarshal([]byte(`true`), &d); err == nil {
		t.Fatal("boolean duration must error")
	}
	raw, _ := json.Marshal(engine.Duration{Duration: 3 * time.Minute})
	if string(raw) != `"3m0s"` {
		t.Fatalf("duration marshal=%s", raw)
	}

	var vt engine.VTime
	if err := json.Unmarshal([]byte(`"2026-03-01T10:00:00Z"`), &vt); err != nil || vt.Year() != 2026 {
		t.Fatalf("rfc3339 vtime: %v", err)
	}
	if err := json.Unmarshal([]byte(`1767225600`), &vt); err != nil || vt.UTC().Format(time.RFC3339) != "2026-01-01T00:00:00Z" {
		t.Fatalf("numeric vtime: %v %s", err, vt.Time)
	}
	if err := json.Unmarshal([]byte(`"not-a-time"`), &vt); err == nil {
		t.Fatal("bad time string must error")
	}
	if err := json.Unmarshal([]byte(`false`), &vt); err == nil {
		t.Fatal("boolean time must error")
	}
	if _, err := json.Marshal(vt); err != nil {
		t.Fatalf("vtime marshal: %v", err)
	}
}

// TestPersistCallbackFires verifies WithPersist is invoked on mutations.
func TestPersistCallbackFires(t *testing.T) {
	e := newTestEngine()
	calls := 0
	e.WithPersist(func(*engine.Snapshot) { calls++ })
	before := calls
	if err := e.CreateRule(baseRule("r1", "m")); err != nil {
		t.Fatal(err)
	}
	if calls <= before {
		t.Fatal("persist callback not invoked on create")
	}
	send(e, tc, "m", 1)
	if calls <= before {
		t.Fatal("persist callback not invoked on ingest")
	}
}
