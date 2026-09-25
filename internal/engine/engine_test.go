package engine_test

import (
	"testing"

	"alertfsm/internal/engine"
	"alertfsm/internal/model"
	"alertfsm/internal/store"
)

func newTestEngine(t *testing.T) *engine.Engine {
	t.Helper()
	st, err := store.New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	st.SetClockFresh(1_000_000)
	return engine.New(st)
}

func cpuRule(id string) model.Rule {
	return model.Rule{
		ID:          id,
		Metric:      "cpu.usage",
		Threshold:   80,
		Direction:   model.DirectionAbove,
		PendingFor:  60_000,
		RecoveryFor: 30_000,
		NoDataFor:   120_000,
	}
}

func ingest(t *testing.T, e *engine.Engine, ts int64, metric string, v float64) engine.IngestResult {
	t.Helper()
	res, err := e.Ingest([]model.Sample{{Metric: metric, TSMS: ts, Value: v}})
	if err != nil {
		t.Fatalf("ingest ts=%d v=%v: %v", ts, v, err)
	}
	return res
}

func status(e *engine.Engine, id string) model.State {
	st := e.Store()
	st.Lock()
	defer st.Unlock()
	s, _ := st.GetStateLocked(id)
	return s
}

// TestFullLifecycle covers ok -> pending -> alerting -> recovering -> ok and
// asserts events are produced ONLY at transitions.
func TestFullLifecycle(t *testing.T) {
	e := newTestEngine(t)
	if _, err := e.CreateRule(cpuRule("r1")); err != nil {
		t.Fatal(err)
	}
	const T0 = int64(1_000_000)

	// A healthy sample leaves the rule in ok.
	ingest(t, e, T0, "cpu.usage", 50)
	if got := status(e, "r1").Status; got != model.StatusOK {
		t.Fatalf("after good sample: %s, want ok", got)
	}

	// Breach at T0+10s -> pending, no event yet.
	ingest(t, e, T0+10_000, "cpu.usage", 90)
	if got := status(e, "r1").Status; got != model.StatusPending {
		t.Fatalf("after breach: %s, want pending", got)
	}

	// Still breaching at T0+40s: only 30s sustained, must stay pending.
	ingest(t, e, T0+40_000, "cpu.usage", 95)
	if got := status(e, "r1").Status; got != model.StatusPending {
		t.Fatalf("30s breach: %s, want pending", got)
	}

	// Tick to exactly T0+70s: 60s sustained -> firing.
	if _, err := e.Tick(T0 + 70_000); err != nil {
		t.Fatal(err)
	}
	if got := status(e, "r1").Status; got != model.StatusAlerting {
		t.Fatalf("after 60s breach: %s, want alerting", got)
	}
	evs := eventsOf(e, "r1")
	if len(evs) != 1 || evs[0].Type != model.EventFiring {
		t.Fatalf("want 1 firing event, got %+v", evs)
	}
	if evs[0].TSMS != T0+70_000 {
		t.Fatalf("firing at %d, want %d", evs[0].TSMS, T0+70_000)
	}

	// More breaching samples while firing must NOT create events.
	ingest(t, e, T0+80_000, "cpu.usage", 99)
	ingest(t, e, T0+90_000, "cpu.usage", 99)
	if evs := eventsOf(e, "r1"); len(evs) != 1 {
		t.Fatalf("repeated firing samples produced events: %+v", evs)
	}

	// A good sample at T0+100s starts recovery.
	ingest(t, e, T0+100_000, "cpu.usage", 10)
	if got := status(e, "r1").Status; got != model.StatusRecovering {
		t.Fatalf("after good sample: %s, want recovering", got)
	}
	if evs := eventsOf(e, "r1"); len(evs) != 1 {
		t.Fatalf("entering recovering must not emit an event")
	}

	// Good sample 20s later: recovery requires 30s, still recovering.
	ingest(t, e, T0+120_000, "cpu.usage", 20)
	if got := status(e, "r1").Status; got != model.StatusRecovering {
		t.Fatalf("20s recovery: %s, want recovering", got)
	}

	// Tick to T0+130s -> resolved.
	if _, err := e.Tick(T0 + 130_000); err != nil {
		t.Fatal(err)
	}
	if got := status(e, "r1").Status; got != model.StatusOK {
		t.Fatalf("after 30s recovery: %s, want ok", got)
	}
	evs = eventsOf(e, "r1")
	if len(evs) != 2 || evs[1].Type != model.EventResolved {
		t.Fatalf("want resolved as 2nd event, got %+v", evs)
	}
	if evs[1].TSMS != T0+130_000 {
		t.Fatalf("resolved at %d, want %d", evs[1].TSMS, T0+130_000)
	}
}

// TestJitterAroundThreshold drives values flapping across the threshold:
// a pending run must reset on every good sample, and a recovering run must
// return to alerting on a breach. No notification fires until sustained.
func TestJitterAroundThreshold(t *testing.T) {
	e := newTestEngine(t)
	if _, err := e.CreateRule(cpuRule("j1")); err != nil {
		t.Fatal(err)
	}
	const T0 = int64(1_000_000)

	// Breach/good alternating every 20s for 2 minutes. Pending never lasts
	// 60s, so no firing may occur.
	ts := T0
	for i := 0; i < 6; i++ {
		ingest(t, e, ts, "cpu.usage", 90) // breach
		ts += 20_000
		ingest(t, e, ts, "cpu.usage", 10) // good -> pending run reset
		ts += 20_000
	}
	if got := status(e, "j1").Status; got != model.StatusOK {
		t.Fatalf("jitter left status %s, want ok", got)
	}
	if evs := eventsOf(e, "j1"); len(evs) != 0 {
		t.Fatalf("jitter produced %d events, want 0: %+v", len(evs), evs)
	}

	// Now sustain a breach long enough to fire.
	ingest(t, e, ts, "cpu.usage", 90)
	if _, err := e.Tick(ts + 60_000); err != nil {
		t.Fatal(err)
	}
	if got := status(e, "j1").Status; got != model.StatusAlerting {
		t.Fatalf("sustained breach: %s, want alerting", got)
	}
	if len(eventsOf(e, "j1")) != 1 {
		t.Fatalf("want exactly 1 firing event")
	}

	// Recover, but flap once: good -> breach before 30s must re-alert
	// WITHOUT a new firing event.
	recoverStart := ts + 60_000
	ingest(t, e, recoverStart+5_000, "cpu.usage", 10)
	if got := status(e, "j1").Status; got != model.StatusRecovering {
		t.Fatalf("good while firing: %s, want recovering", got)
	}
	ingest(t, e, recoverStart+20_000, "cpu.usage", 95)
	if got := status(e, "j1").Status; got != model.StatusAlerting {
		t.Fatalf("breach during recovery: %s, want alerting", got)
	}
	if evs := eventsOf(e, "j1"); len(evs) != 1 {
		t.Fatalf("flap back to alerting must not emit an event, got %+v", evs)
	}

	// Sustained recovery finally resolves.
	ingest(t, e, recoverStart+25_000, "cpu.usage", 10)
	if _, err := e.Tick(recoverStart + 25_000 + 30_000); err != nil {
		t.Fatal(err)
	}
	if got := status(e, "j1").Status; got != model.StatusOK {
		t.Fatalf("sustained recovery: %s, want ok", got)
	}
	evs := eventsOf(e, "j1")
	if len(evs) != 2 || evs[1].Type != model.EventResolved {
		t.Fatalf("want resolved event, got %+v", evs)
	}
}

// TestNoData verifies the missing-data state from ok, pending and alerting,
// and that new samples leave no_data correctly.
func TestNoData(t *testing.T) {
	e := newTestEngine(t)
	if _, err := e.CreateRule(cpuRule("n1")); err != nil {
		t.Fatal(err)
	}
	const T0 = int64(1_000_000)

	// One good sample, then silence. At last_seen + no_data_for - 1 the
	// data is still fresh; at exactly last_seen + no_data_for the rule
	// transitions to no_data (step semantics, matching the pending deadline).
	ingest(t, e, T0, "cpu.usage", 50)
	if _, err := e.Tick(T0 + 119_999); err != nil {
		t.Fatal(err)
	}
	if got := status(e, "n1").Status; got != model.StatusOK {
		t.Fatalf("just before no_data deadline: %s, want ok", got)
	}
	if _, err := e.Tick(T0 + 120_000); err != nil {
		t.Fatal(err)
	}
	if got := status(e, "n1").Status; got != model.StatusNoData {
		t.Fatalf("at no_data deadline: %s, want no_data", got)
	}
	evs := eventsOf(e, "n1")
	if len(evs) != 1 || evs[0].Type != model.EventNoDataStart {
		t.Fatalf("want 1 no_data_start, got %+v", evs)
	}

	// Further ticks in no_data emit nothing.
	if _, err := e.Tick(T0 + 300_000); err != nil {
		t.Fatal(err)
	}
	if evs := eventsOf(e, "n1"); len(evs) != 1 {
		t.Fatalf("ticking in no_data emitted events: %+v", evs)
	}

	// A good sample resumes directly to ok with no_data_end.
	ingest(t, e, T0+400_000, "cpu.usage", 42)
	if got := status(e, "n1").Status; got != model.StatusOK {
		t.Fatalf("good sample after no_data: %s, want ok", got)
	}
	evs = eventsOf(e, "n1")
	if len(evs) != 2 || evs[1].Type != model.EventNoDataEnd || evs[1].To != model.StatusOK {
		t.Fatalf("want no_data_end->ok, got %+v", evs)
	}

	// no_data from pending: breach then silence before pending completes.
	ingest(t, e, T0+500_000, "cpu.usage", 90)
	if got := status(e, "n1").Status; got != model.StatusPending {
		t.Fatalf("breach: %s, want pending", got)
	}
	if _, err := e.Tick(T0 + 500_000 + 120_001); err != nil {
		t.Fatal(err)
	}
	if got := status(e, "n1").Status; got != model.StatusNoData {
		t.Fatalf("silence after breach: %s, want no_data", got)
	}
	// A breaching sample resumes into pending.
	ingest(t, e, T0+700_000, "cpu.usage", 99)
	if got := status(e, "n1").Status; got != model.StatusPending {
		t.Fatalf("breach sample after no_data: %s, want pending", got)
	}
	evs = eventsOf(e, "n1")
	last := evs[len(evs)-1]
	if last.Type != model.EventNoDataEnd || last.To != model.StatusPending {
		t.Fatalf("want no_data_end->pending, got %+v", last)
	}
}

// TestNoDataFromAlerting covers data stopping while an alert is firing.
func TestNoDataFromAlerting(t *testing.T) {
	e := newTestEngine(t)
	r := cpuRule("na")
	if _, err := e.CreateRule(r); err != nil {
		t.Fatal(err)
	}
	const T0 = int64(1_000_000)

	ingest(t, e, T0, "cpu.usage", 90)
	if _, err := e.Tick(T0 + r.PendingFor.Milliseconds()); err != nil {
		t.Fatal(err)
	}
	if got := status(e, "na").Status; got != model.StatusAlerting {
		t.Fatalf("want alerting, got %s", got)
	}
	// Silence beyond no_data_for.
	if _, err := e.Tick(T0 + r.PendingFor.Milliseconds() + r.NoDataFor.Milliseconds() + 1); err != nil {
		t.Fatal(err)
	}
	if got := status(e, "na").Status; got != model.StatusNoData {
		t.Fatalf("want no_data from alerting, got %s", got)
	}
	evs := eventsOf(e, "na")
	if len(evs) != 2 || evs[0].Type != model.EventFiring || evs[1].Type != model.EventNoDataStart {
		t.Fatalf("unexpected events: %+v", evs)
	}
}

// TestOutOfOrder verifies samples with timestamps older than the evaluated
// watermark are stored (queryable) but never drive the state machine.
func TestOutOfOrder(t *testing.T) {
	e := newTestEngine(t)
	if _, err := e.CreateRule(cpuRule("o1")); err != nil {
		t.Fatal(err)
	}
	const T0 = int64(1_000_000)

	// Advance the clock well into the future with healthy data.
	ingest(t, e, T0+500_000, "cpu.usage", 10)

	// A late breaching sample from the past must be ignored by the FSM.
	res, err := e.Ingest([]model.Sample{{Metric: "cpu.usage", TSMS: T0 + 10_000, Value: 99}})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Late) != 1 {
		t.Fatalf("want 1 late sample, got %+v", res)
	}
	if got := status(e, "o1").Status; got != model.StatusOK {
		t.Fatalf("late sample changed state to %s", got)
	}
	if evs := eventsOf(e, "o1"); len(evs) != 0 {
		t.Fatalf("late sample produced events: %+v", evs)
	}

	// The late sample is still stored and queryable.
	st := e.Store()
	got := st.ListSamples("cpu.usage", T0, T0+20_000)
	if len(got) != 1 || got[0].Value != 99 {
		t.Fatalf("late sample not stored/queryable: %+v", got)
	}

	// Clock can never go backwards.
	if _, err := e.Tick(T0); err == nil {
		t.Fatal("backwards tick accepted")
	}
}

// TestDuplicateSamplesDoNotAccumulate sends the same (metric, ts) twice with
// different values and verifies duration accounting ignores it entirely.
func TestDuplicateSamplesDoNotAccumulate(t *testing.T) {
	e := newTestEngine(t)
	r := cpuRule("d1")
	if _, err := e.CreateRule(r); err != nil {
		t.Fatal(err)
	}
	const T0 = int64(1_000_000)

	// Breach starts at T0.
	ingest(t, e, T0, "cpu.usage", 90)
	// Re-send the exact same sample (even with a new value): duplicate.
	res, err := e.Ingest([]model.Sample{
		{Metric: "cpu.usage", TSMS: T0, Value: 90},
		{Metric: "cpu.usage", TSMS: T0, Value: 10},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Duplicate) != 2 {
		t.Fatalf("want 2 duplicates, got %+v", res)
	}
	if got := status(e, "d1"); got.LastValue != 90 {
		t.Fatalf("duplicate changed last value to %v, want 90", got.LastValue)
	}

	// Even "advancing" by re-sending an old timestamp doesn't move time.
	res2, _ := e.Ingest([]model.Sample{{Metric: "cpu.usage", TSMS: T0, Value: 90}})
	if len(res2.Duplicate) != 1 {
		t.Fatalf("want duplicate, got %+v", res2)
	}
	if got := e.Clock(); got != T0 {
		t.Fatalf("duplicate moved clock to %d, want %d", got, T0)
	}

	// Real elapsed time is measured from the first breach sample; a new
	// breaching sample at T0+59s leaves it pending, T0+60s via tick fires.
	ingest(t, e, T0+59_000, "cpu.usage", 91)
	if got := status(e, "d1").Status; got != model.StatusPending {
		t.Fatalf("59s breach: %s", got)
	}
	if _, err := e.Tick(T0 + 60_000); err != nil {
		t.Fatal(err)
	}
	if got := status(e, "d1").Status; got != model.StatusAlerting {
		t.Fatalf("60s breach: %s", got)
	}
	if evs := eventsOf(e, "d1"); len(evs) != 1 {
		t.Fatalf("want 1 event, got %+v", evs)
	}
}

// TestConfigChangeResets verifies updating a rule wipes its state and emits a
// reset notification.
func TestConfigChangeResets(t *testing.T) {
	e := newTestEngine(t)
	r := cpuRule("c1")
	if _, err := e.CreateRule(r); err != nil {
		t.Fatal(err)
	}
	const T0 = int64(1_000_000)
	ingest(t, e, T0, "cpu.usage", 90)
	if _, err := e.Tick(T0 + 60_000); err != nil {
		t.Fatal(err)
	}
	if got := status(e, "c1").Status; got != model.StatusAlerting {
		t.Fatalf("setup: want alerting, got %s", got)
	}

	// Change the threshold: explicit reset to ok + reset event.
	r.Threshold = 95
	if _, err := e.UpdateRule(r); err != nil {
		t.Fatal(err)
	}
	st := status(e, "c1")
	if st.Status != model.StatusOK {
		t.Fatalf("after update: %s, want ok", st.Status)
	}
	if st.LastSeenMS != 0 {
		t.Fatalf("after update last_seen=%d, want 0 (fresh run)", st.LastSeenMS)
	}
	evs := eventsOf(e, "c1")
	if len(evs) != 2 || evs[1].Type != model.EventReset || evs[1].From != model.StatusAlerting || evs[1].To != model.StatusOK {
		t.Fatalf("want reset alerting->ok, got %+v", evs)
	}

	// Under the new threshold the old value (90) is healthy.
	ingest(t, e, T0+70_000, "cpu.usage", 90)
	if got := status(e, "c1").Status; got != model.StatusOK {
		t.Fatalf("90 vs new threshold 95: %s, want ok", got)
	}
}

// TestPendingRunRestarts ensures a good sample arriving BEFORE the pending
// deadline cancels the run; the next breach starts a fresh pending clock.
func TestPendingRunRestarts(t *testing.T) {
	e := newTestEngine(t)
	r := cpuRule("p1")
	if _, err := e.CreateRule(r); err != nil {
		t.Fatal(err)
	}
	const T0 = int64(1_000_000)
	ingest(t, e, T0, "cpu.usage", 90)        // pending run starts
	ingest(t, e, T0+59_000, "cpu.usage", 90) // still pending at 59s
	ingest(t, e, T0+59_500, "cpu.usage", 10) // good before the deadline: run canceled
	ingest(t, e, T0+62_000, "cpu.usage", 90) // new run starts at 62s
	if _, err := e.Tick(T0 + 62_000 + 59_000); err != nil {
		t.Fatal(err)
	}
	if got := status(e, "p1").Status; got != model.StatusPending {
		t.Fatalf("new run only 59s old: %s, want pending", got)
	}
	if _, err := e.Tick(T0 + 62_000 + 60_000); err != nil {
		t.Fatal(err)
	}
	if got := status(e, "p1").Status; got != model.StatusAlerting {
		t.Fatalf("new run 60s old: %s, want alerting", got)
	}
	if evs := eventsOf(e, "p1"); len(evs) != 1 {
		t.Fatalf("want single firing, got %+v", evs)
	}
}

// TestBatchOrdering verifies a batch sent shuffled is evaluated in timestamp
// order, so a whole breach window delivered at once fires correctly.
func TestBatchOrdering(t *testing.T) {
	e := newTestEngine(t)
	if _, err := e.CreateRule(cpuRule("b1")); err != nil {
		t.Fatal(err)
	}
	const T0 = int64(1_000_000)
	batch := []model.Sample{
		{Metric: "cpu.usage", TSMS: T0 + 60_000, Value: 90},
		{Metric: "cpu.usage", TSMS: T0, Value: 90},
		{Metric: "cpu.usage", TSMS: T0 + 30_000, Value: 90},
	}
	if _, err := e.Ingest(batch); err != nil {
		t.Fatal(err)
	}
	// Sorted processing: breach starts at T0 and the T0+60s timer fires
	// within the batch.
	if got := status(e, "b1").Status; got != model.StatusAlerting {
		t.Fatalf("shuffled batch: %s, want alerting", got)
	}
}

// TestZeroDurations checks pending_for=0 fires on the breaching sample itself.
func TestZeroPendingFor(t *testing.T) {
	e := newTestEngine(t)
	r := model.Rule{ID: "z1", Metric: "m", Threshold: 1, Direction: model.DirectionAbove, PendingFor: 0, RecoveryFor: 0, NoDataFor: 0}
	if _, err := e.CreateRule(r); err != nil {
		t.Fatal(err)
	}
	const T0 = int64(1_000_000)
	ingest(t, e, T0, "m", 2)
	if got := status(e, "z1").Status; got != model.StatusAlerting {
		t.Fatalf("pending_for=0: %s, want alerting", got)
	}
	if evs := eventsOf(e, "z1"); len(evs) != 1 || evs[0].Type != model.EventFiring {
		t.Fatalf("want immediate firing, got %+v", evs)
	}
	ingest(t, e, T0+1_000, "m", 0)
	if got := status(e, "z1").Status; got != model.StatusOK {
		t.Fatalf("recovery_for=0: %s, want ok", got)
	}
}

// TestBelowDirection exercises the below-threshold direction.
func TestBelowDirection(t *testing.T) {
	e := newTestEngine(t)
	r := model.Rule{ID: "lo1", Metric: "disk.free_gb", Threshold: 5, Direction: model.DirectionBelow, PendingFor: 10_000, RecoveryFor: 10_000, NoDataFor: 100_000}
	if _, err := e.CreateRule(r); err != nil {
		t.Fatal(err)
	}
	const T0 = int64(1_000_000)
	ingest(t, e, T0, "disk.free_gb", 4)
	if _, err := e.Tick(T0 + 10_000); err != nil {
		t.Fatal(err)
	}
	if got := status(e, "lo1").Status; got != model.StatusAlerting {
		t.Fatalf("below threshold: %s", got)
	}
	ingest(t, e, T0+20_000, "disk.free_gb", 6)
	if _, err := e.Tick(T0 + 30_000); err != nil {
		t.Fatal(err)
	}
	if got := status(e, "lo1").Status; got != model.StatusOK {
		t.Fatalf("recovered above: %s", got)
	}
	evs := eventsOf(e, "lo1")
	if len(evs) != 2 || evs[0].Type != model.EventFiring || evs[1].Type != model.EventResolved {
		t.Fatalf("unexpected events: %+v", evs)
	}
}

// eventsOf returns events for a rule via the public store accessor.
func eventsOf(e *engine.Engine, ruleID string) []model.Event {
	return e.Store().ListEvents(0, ruleID, 0)
}

// TestMultipleRulesSameMetric verifies one sample is applied to every enabled
// rule watching that metric, each with its own state.
func TestMultipleRulesSameMetric(t *testing.T) {
	e := newTestEngine(t)
	warn := model.Rule{ID: "warn", Metric: "cpu", Threshold: 70, Direction: model.DirectionAbove,
		PendingFor: 0, RecoveryFor: 0, NoDataFor: 0}
	crit := model.Rule{ID: "crit", Metric: "cpu", Threshold: 90, Direction: model.DirectionAbove,
		PendingFor: 0, RecoveryFor: 0, NoDataFor: 0}
	if _, err := e.CreateRule(warn); err != nil {
		t.Fatal(err)
	}
	if _, err := e.CreateRule(crit); err != nil {
		t.Fatal(err)
	}
	const T0 = int64(1_000_000)

	ingest(t, e, T0, "cpu", 80) // breaches warn (70), not crit (90)
	if got := status(e, "warn").Status; got != model.StatusAlerting {
		t.Fatalf("warn: %s", got)
	}
	if got := status(e, "crit").Status; got != model.StatusOK {
		t.Fatalf("crit: %s, want ok", got)
	}

	ingest(t, e, T0+1_000, "cpu", 95) // breaches both
	if got := status(e, "crit").Status; got != model.StatusAlerting {
		t.Fatalf("crit: %s, want alerting", got)
	}
	if evs := e.Store().ListEvents(0, "", 0); len(evs) != 2 {
		t.Fatalf("want 2 firing events total, got %+v", evs)
	}

	ingest(t, e, T0+2_000, "cpu", 60) // resolves both
	if status(e, "warn").Status != model.StatusOK || status(e, "crit").Status != model.StatusOK {
		t.Fatalf("both should resolve: warn=%s crit=%s", status(e, "warn").Status, status(e, "crit").Status)
	}
	if evs := e.Store().ListEvents(0, "", 0); len(evs) != 4 {
		t.Fatalf("want 4 events (2 firing + 2 resolved), got %d", len(evs))
	}
}
