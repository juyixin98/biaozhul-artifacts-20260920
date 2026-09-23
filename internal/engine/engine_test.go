package engine_test

import (
	"context"
	"testing"
	"time"

	"sensorhealth/internal/clock"
	"sensorhealth/internal/engine"
	"sensorhealth/internal/model"
	"sensorhealth/internal/store"
)

type harness struct {
	ctx context.Context
	st  *store.Store
	eng *engine.Engine
	clk *clock.Virtual
	t   *testing.T
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	dir := t.TempDir()
	st, err := store.Open(context.Background(), dir+"/test.db")
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	vclk := clock.NewVirtual(time.Date(2026, 9, 23, 10, 0, 0, 0, time.UTC))
	eng, err := engine.New(context.Background(), st, vclk)
	if err != nil {
		t.Fatalf("engine: %v", err)
	}
	return &harness{ctx: context.Background(), st: st, eng: eng, clk: vclk, t: t}
}

func (h *harness) putType(d model.DeviceType) model.DeviceType {
	h.t.Helper()
	out, err := h.st.UpsertDeviceType(h.ctx, d)
	if err != nil {
		h.t.Fatalf("upsert type: %v", err)
	}
	return out
}

func (h *harness) ingest(batch []model.Message) *engine.IngestResult {
	h.t.Helper()
	res, err := h.eng.Ingest(h.ctx, batch, engine.IngestOptions{})
	if err != nil {
		h.t.Fatalf("ingest: %v", err)
	}
	return res
}

func dataMsg(dev, typ string, seq int64, sample time.Time, v float64) model.Message {
	return model.Message{DeviceID: dev, Type: typ, Seq: &seq,
		SampleTime: sample.Format(time.RFC3339Nano), Value: &v}
}

func hbMsg(dev, typ string, sample time.Time) model.Message {
	return model.Message{DeviceID: dev, Type: typ,
		SampleTime: sample.Format(time.RFC3339Nano), IsHeartbeat: true}
}

func ruleState(h *harness, dev, rule string) model.RuleHealth {
	h.t.Helper()
	hh, err := h.eng.Health(h.ctx, dev)
	if err != nil || hh == nil {
		h.t.Fatalf("health %s: %v", dev, err)
	}
	return hh.Rules[rule]
}

func openEvents(h *harness, dev, rule string) []model.Event {
	h.t.Helper()
	b := true
	evs, err := h.st.ListEvents(h.ctx, store.EventFilter{DeviceID: dev, Rule: rule, Open: &b})
	if err != nil {
		h.t.Fatalf("events: %v", err)
	}
	return evs
}

// Stationary devices of a type with fixed detection disabled must never be
// flagged: identical values with advancing sequence stay healthy.
func TestStationaryDeviceNotBroken(t *testing.T) {
	h := newHarness(t)
	h.putType(model.DeviceType{
		Type: "door", StaleEnterSec: 1000, StaleRecoverSec: 100,
		FixedWindowCount: 0, GapRecoverGapless: 2, ClockSkewTolMs: 500,
	})
	t0 := h.clk.Now()
	msgs := make([]model.Message, 0, 20)
	for i := int64(1); i <= 20; i++ {
		msgs = append(msgs, dataMsg("door1", "door", i, t0.Add(time.Duration(i)*time.Second), 0))
	}
	h.ingest(msgs)
	if rs := ruleState(h, "door1", model.RuleFixed); rs.State != model.StateOK {
		t.Fatalf("fixed state = %s, want OK for stationary door contact", rs.State)
	}
	if rs := ruleState(h, "door1", model.RuleGap); rs.State != model.StateOK {
		t.Fatalf("gap state = %s, want OK", rs.State)
	}
	if rs := ruleState(h, "door1", model.RuleStale); rs.State != model.StateOK {
		t.Fatalf("stale state = %s, want OK", rs.State)
	}
}

// A moving-sensor type enters fixed_value after the configured run length,
// and the event carries the trigger sample interval + config version.
func TestFixedValueEnterIntervalAndVersion(t *testing.T) {
	h := newHarness(t)
	cfg := h.putType(model.DeviceType{
		Type: "temperature", StaleEnterSec: 1000, StaleRecoverSec: 100,
		FixedWindowCount: 5, FixedTolerance: 0.1,
		GapRecoverGapless: 2, ClockSkewTolMs: 500,
	})
	t0 := h.clk.Now()
	msgs := make([]model.Message, 0, 5)
	for i := int64(1); i <= 5; i++ {
		// differences <= 0.1 tolerance count as equal
		v := 20.0
		if i == 3 {
			v = 20.05
		}
		msgs = append(msgs, dataMsg("s1", "temperature", i, t0.Add(time.Duration(i)*time.Second), v))
	}
	h.ingest(msgs)
	rs := ruleState(h, "s1", model.RuleFixed)
	if rs.State != model.StateAlert {
		t.Fatalf("fixed state = %s, want ALERT", rs.State)
	}
	evs := openEvents(h, "s1", model.RuleFixed)
	if len(evs) != 1 {
		t.Fatalf("open fixed events = %d, want 1", len(evs))
	}
	ev := evs[0]
	if ev.ConfigVersion != cfg.Version {
		t.Fatalf("config version = %d, want %d", ev.ConfigVersion, cfg.Version)
	}
	if ev.StartSeq == nil || *ev.StartSeq != 1 || ev.EndSeq == nil || *ev.EndSeq != 5 {
		t.Fatalf("trigger interval seq = %v..%v, want 1..5", ev.StartSeq, ev.EndSeq)
	}
	if ev.StartSample == nil || ev.EndSample == nil {
		t.Fatal("trigger interval sample times must both be set")
	}
}

// Recovery from fixed_value needs a genuinely changing value.
func TestFixedValueRecoverOnChange(t *testing.T) {
	h := newHarness(t)
	h.putType(model.DeviceType{
		Type: "temperature", StaleEnterSec: 1000, StaleRecoverSec: 100,
		FixedWindowCount: 3, GapRecoverGapless: 2, ClockSkewTolMs: 500,
	})
	t0 := h.clk.Now()
	h.ingest([]model.Message{
		dataMsg("s1", "temperature", 1, t0.Add(time.Second), 20),
		dataMsg("s1", "temperature", 2, t0.Add(2*time.Second), 20),
		dataMsg("s1", "temperature", 3, t0.Add(3*time.Second), 20),
	})
	if ruleState(h, "s1", model.RuleFixed).State != model.StateAlert {
		t.Fatal("want fixed ALERT")
	}
	h.ingest([]model.Message{
		dataMsg("s1", "temperature", 4, t0.Add(4*time.Second), 21),
	})
	if rs := ruleState(h, "s1", model.RuleFixed); rs.State != model.StateOK {
		t.Fatalf("fixed state after change = %s, want OK", rs.State)
	}
	b := false
	evs, _ := h.st.ListEvents(h.ctx, store.EventFilter{DeviceID: "s1", Rule: model.RuleFixed, Open: &b})
	if len(evs) != 1 || evs[0].RecoveredAt == nil {
		t.Fatalf("want one recovered fixed event, got %+v", evs)
	}
}

// A forward sequence jump opens a gap alert naming the missing range;
// N gap-free samples then recover it (enter vs recover thresholds differ).
func TestSequenceGapEnterAndRecover(t *testing.T) {
	h := newHarness(t)
	h.putType(model.DeviceType{
		Type: "temperature", StaleEnterSec: 1000, StaleRecoverSec: 100,
		FixedWindowCount: 0, GapRecoverGapless: 3, ClockSkewTolMs: 500,
	})
	t0 := h.clk.Now()
	h.ingest([]model.Message{
		dataMsg("s1", "temperature", 1, t0.Add(time.Second), 1),
		dataMsg("s1", "temperature", 5, t0.Add(2*time.Second), 2),
	})
	evs := openEvents(h, "s1", model.RuleGap)
	if len(evs) != 1 {
		t.Fatalf("open gap events = %d, want 1", len(evs))
	}
	if evs[0].GapStart == nil || *evs[0].GapStart != 2 ||
		evs[0].GapEnd == nil || *evs[0].GapEnd != 4 {
		t.Fatalf("missing range = %v..%v, want 2..4", evs[0].GapStart, evs[0].GapEnd)
	}
	// Two clean samples are below the recovery threshold of 3.
	h.ingest([]model.Message{dataMsg("s1", "temperature", 6, t0.Add(3*time.Second), 3)})
	h.ingest([]model.Message{dataMsg("s1", "temperature", 7, t0.Add(4*time.Second), 4)})
	if ruleState(h, "s1", model.RuleGap).State != model.StateAlert {
		t.Fatal("gap must stay ALERT after only 2 gap-free samples (threshold 3)")
	}
	h.ingest([]model.Message{dataMsg("s1", "temperature", 8, t0.Add(5*time.Second), 5)})
	if ruleState(h, "s1", model.RuleGap).State != model.StateOK {
		t.Fatal("gap must recover after 3 gap-free samples")
	}
}

// Ordered batch backfill: older (timestamp, seq) messages arriving late are
// accepted and marked, do not advance the high-water mark, and do not raise
// a false gap against already-seen sequence numbers.
func TestBatchBackfill(t *testing.T) {
	h := newHarness(t)
	h.putType(model.DeviceType{
		Type: "temperature", StaleEnterSec: 1000, StaleRecoverSec: 100,
		FixedWindowCount: 0, GapRecoverGapless: 2, ClockSkewTolMs: 500,
	})
	t0 := h.clk.Now()
	// Real-time arrives first...
	h.ingest([]model.Message{
		dataMsg("s1", "temperature", 10, t0.Add(10*time.Second), 10),
		dataMsg("s1", "temperature", 11, t0.Add(11*time.Second), 11),
	})
	// ...then an ordered historical batch 1..9 is delivered late,
	// explicitly tagged as a backfill replay.
	batch := make([]model.Message, 0, 9)
	for i := int64(1); i <= 9; i++ {
		batch = append(batch, dataMsg("s1", "temperature", i, t0.Add(time.Duration(i)*time.Second), float64(i)))
	}
	res, err := h.eng.Ingest(h.ctx, batch, engine.IngestOptions{Backfill: true})
	if err != nil {
		t.Fatalf("backfill ingest: %v", err)
	}
	for _, it := range res.Items {
		if !it.Accepted || !it.Backfill {
			t.Fatalf("item %d: accepted=%v backfill=%v reason=%s", it.Index, it.Accepted, it.Backfill, it.Reason)
		}
	}
	if rs := ruleState(h, "s1", model.RuleGap); rs.State != model.StateOK {
		t.Fatalf("gap state = %s, backfill must not raise gap", rs.State)
	}
	msgs, err := h.st.ListMessages(h.ctx, "s1", 0)
	if err != nil || len(msgs) != 11 {
		t.Fatalf("stored messages = %d (err=%v), want 11", len(msgs), err)
	}
}

// Device clock rollback: a backwards sample jump beyond skew tolerance opens
// a new sequence epoch; the sequence restarts at 1 without a gap alert.
func TestClockRollbackStartsNewEpoch(t *testing.T) {
	h := newHarness(t)
	h.putType(model.DeviceType{
		Type: "temperature", StaleEnterSec: 1000, StaleRecoverSec: 100,
		FixedWindowCount: 0, GapRecoverGapless: 2, ClockSkewTolMs: 500,
	})
	t0 := h.clk.Now()
	h.ingest([]model.Message{
		dataMsg("s1", "temperature", 100, t0.Add(100*time.Second), 1),
		dataMsg("s1", "temperature", 101, t0.Add(101*time.Second), 2),
	})
	// Device reboots: clock jumps back, seq restarts.
	h.ingest([]model.Message{
		dataMsg("s1", "temperature", 1, t0.Add(time.Second), 3),
		dataMsg("s1", "temperature", 2, t0.Add(2*time.Second), 4),
	})
	hh, _ := h.eng.Health(h.ctx, "s1")
	if hh.Epoch != 1 {
		t.Fatalf("epoch = %d, want 1 after clock rollback", hh.Epoch)
	}
	if hh.LastSeq == nil || *hh.LastSeq != 2 {
		t.Fatalf("last seq = %v, want 2 in new epoch", hh.LastSeq)
	}
	if rs := ruleState(h, "s1", model.RuleGap); rs.State != model.StateOK {
		t.Fatalf("gap state = %s, restarted sequence must not be a gap", rs.State)
	}
}

// A backwards move within the configured skew tolerance is jitter, not a
// rollback: no new epoch.
func TestSmallClockJitterIsTolerated(t *testing.T) {
	h := newHarness(t)
	h.putType(model.DeviceType{
		Type: "temperature", StaleEnterSec: 1000, StaleRecoverSec: 100,
		FixedWindowCount: 0, GapRecoverGapless: 2, ClockSkewTolMs: 1000,
	})
	t0 := h.clk.Now()
	h.ingest([]model.Message{
		dataMsg("s1", "temperature", 1, t0.Add(time.Second), 1),
		dataMsg("s1", "temperature", 2, t0.Add(2*time.Second), 2),
	})
	// 300 ms backwards, under the 1000 ms tolerance.
	h.ingest([]model.Message{
		dataMsg("s1", "temperature", 3, t0.Add(2*time.Second-300*time.Millisecond), 3),
	})
	hh, _ := h.eng.Health(h.ctx, "s1")
	if hh.Epoch != 0 {
		t.Fatalf("epoch = %d, jitter within tolerance must not open new epoch", hh.Epoch)
	}
}

// Stale alert enters on the silence window (enter threshold) and clears on
// the next heartbeat (recovery path), proving device time vs receive time
// are distinct and heartbeats count as liveness.
func TestStaleEnterAndHeartbeatRecover(t *testing.T) {
	h := newHarness(t)
	h.putType(model.DeviceType{
		Type: "temperature", StaleEnterSec: 30, StaleRecoverSec: 10,
		FixedWindowCount: 0, GapRecoverGapless: 2, ClockSkewTolMs: 500,
	})
	t0 := h.clk.Now()
	h.ingest([]model.Message{dataMsg("s1", "temperature", 1, t0, 1)})

	h.clk.Advance(31 * time.Second)
	fired, err := h.eng.Sweep(h.ctx)
	if err != nil || fired != 1 {
		t.Fatalf("sweep fired=%d err=%v, want 1", fired, err)
	}
	if ruleState(h, "s1", model.RuleStale).State != model.StateAlert {
		t.Fatal("want stale ALERT after 31s silence")
	}
	// A heartbeat with its own (possibly lagging) device timestamp proves
	// the device is alive; receive time is now.
	h.ingest([]model.Message{hbMsg("s1", "temperature", h.clk.Now().Add(-5*time.Second))})
	if rs := ruleState(h, "s1", model.RuleStale); rs.State != model.StateOK {
		t.Fatalf("stale state = %s after heartbeat, want OK", rs.State)
	}
}

// Same value but advancing sequence on a fixed-detecting type must not
// alert before the configured run length.
func TestSameValueNormalSequence(t *testing.T) {
	h := newHarness(t)
	h.putType(model.DeviceType{
		Type: "temperature", StaleEnterSec: 1000, StaleRecoverSec: 100,
		FixedWindowCount: 10, GapRecoverGapless: 2, ClockSkewTolMs: 500,
	})
	t0 := h.clk.Now()
	for i := int64(1); i <= 9; i++ {
		h.ingest([]model.Message{dataMsg("s1", "temperature", i, t0.Add(time.Duration(i)*time.Second), 42)})
	}
	if rs := ruleState(h, "s1", model.RuleFixed); rs.State != model.StateOK {
		t.Fatalf("fixed state = %s after 9 samples, threshold is 10", rs.State)
	}
}
