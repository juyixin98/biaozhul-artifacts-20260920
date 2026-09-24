package service

import (
	"context"
	"testing"
	"time"

	"sensorhealth/internal/domain"
)

// TestStale_EnterAndRecover verifies distinct entry/recovery thresholds and
// that heartbeats keep the device alive.
func TestStale_EnterAndRecover(t *testing.T) {
	h := newHarness(t)
	cfg := fastConfig("t")
	h.registerType("d", "t", cfg)

	base := h.now
	// One sample, then silence.
	h.sample("d", 1, "10", base)
	h.advance(11 * time.Second)
	if err := h.svc.SweepStale(context.Background(), h.now); err != nil {
		t.Fatal(err)
	}
	opens := h.openAlerts("d", domain.KindStale)
	if len(opens) != 1 {
		t.Fatalf("expected 1 open stale alert, got %d", len(opens))
	}
	if opens[0].ConfigVersion == 0 {
		t.Fatalf("config version should be recorded on alert")
	}

	// A single heartbeat after the entry timeout does NOT instantly recover:
	// the recover threshold is shorter (3s) but requires continuous
	// reachability for that long.
	h.heartbeat("d")
	if err := h.svc.SweepStale(context.Background(), h.now); err != nil {
		t.Fatal(err)
	}
	if len(h.openAlerts("d", domain.KindStale)) != 1 {
		t.Fatalf("stale must not recover immediately after one heartbeat")
	}

	// Keep heartbeating; after the recover timeout it heals.
	h.advance(1 * time.Second)
	h.heartbeat("d")
	h.advance(1 * time.Second)
	h.heartbeat("d")
	h.advance(1 * time.Second)
	if err := h.svc.SweepStale(context.Background(), h.now); err != nil {
		t.Fatal(err)
	}
	if len(h.openAlerts("d", domain.KindStale)) != 0 {
		t.Fatalf("stale should recover after continuous heartbeats")
	}
	rec := h.recoveredAlerts("d", domain.KindStale)
	if len(rec) != 1 || rec[0].RecoverRange == nil {
		t.Fatalf("expected recovered alert with recovery range, got %+v", rec)
	}
}

// TestStale_HeartbeatPreventsStale proves a device with no fresh SAMPLES but
// regular heartbeats is never marked stale (heartbeats = reachability).
func TestStale_HeartbeatPreventsStale(t *testing.T) {
	h := newHarness(t)
	cfg := fastConfig("t")
	h.registerType("d", "t", cfg)
	h.sample("d", 1, "10")
	for i := 0; i < 5; i++ {
		h.advance(2 * time.Second)
		h.heartbeat("d")
	}
	if err := h.svc.SweepStale(context.Background(), h.now); err != nil {
		t.Fatal(err)
	}
	if len(h.openAlerts("d", domain.KindStale)) != 0 {
		t.Fatalf("heartbeats must keep device non-stale")
	}
}

// TestFrozen_EntersAndRecovers checks the fixed-value rule: same value with
// advancing seq AND enough device-time span triggers; different values
// recover with a distinct (lower) threshold.
func TestFrozen_EntersAndRecovers(t *testing.T) {
	h := newHarness(t)
	cfg := fastConfig("t")
	h.registerType("d", "t", cfg)

	base := h.now
	for i := int64(1); i <= 4; i++ {
		h.sample("d", i, "42", base.Add(time.Duration(i)*10*time.Millisecond))
	}
	opens := h.openAlerts("d", domain.KindFrozen)
	if len(opens) != 1 {
		t.Fatalf("expected frozen alert, got %d", len(opens))
	}
	a := opens[0]
	if a.TriggerRange.SeqStart != 1 || a.TriggerRange.SeqEnd != 4 {
		t.Fatalf("trigger range wrong: %+v", a.TriggerRange)
	}
	if a.ConfigVersion == 0 {
		t.Fatal("missing config version")
	}

	// One different value must NOT recover (recover count = 2).
	h.sample("d", 5, "43", base.Add(50*time.Millisecond))
	if len(h.openAlerts("d", domain.KindFrozen)) != 1 {
		t.Fatalf("single differing sample must not recover yet")
	}
	// A repeat of the frozen value resets the recovery counter.
	h.sample("d", 6, "42", base.Add(60*time.Millisecond))
	h.sample("d", 7, "44", base.Add(70*time.Millisecond))
	if len(h.openAlerts("d", domain.KindFrozen)) != 1 {
		t.Fatalf("recovery counter should reset when frozen value reappears")
	}
	h.sample("d", 8, "45", base.Add(80*time.Millisecond))
	if len(h.openAlerts("d", domain.KindFrozen)) != 0 {
		t.Fatalf("two consecutive differing samples should recover")
	}
	rec := h.recoveredAlerts("d", domain.KindFrozen)
	if len(rec) != 1 || rec[0].RecoverRange == nil {
		t.Fatalf("expected recovery range, got %+v", rec)
	}
}

// TestFrozen_SameValueButNormalSeqBelowThreshold proves a stationary-but-healthy
// device (same readings, advancing seq) is NOT flagged while under the count
// threshold — the type's config decides tolerance.
func TestFrozen_SameValueButNormalSeqBelowThreshold(t *testing.T) {
	h := newHarness(t)
	cfg := fastConfig("t")
	h.registerType("d", "t", cfg)
	base := h.now
	// 3 same values, threshold is 4.
	for i := int64(1); i <= 3; i++ {
		h.sample("d", i, "7", base.Add(time.Duration(i)*10*time.Millisecond))
	}
	if len(h.openAlerts("d", domain.KindFrozen)) != 0 {
		t.Fatalf("same value under count threshold must be healthy")
	}
	health, err := h.svc.Health(context.Background(), "d")
	if err != nil {
		t.Fatal(err)
	}
	if !health.Healthy {
		t.Fatalf("device should be healthy: %+v", health.OpenAlerts)
	}
}

// TestFrozen_StationaryDeviceTypeNotFlagged uses a lenient contact-sensor
// config: constant readings are physically normal for that type.
func TestFrozen_StationaryDeviceTypeNotFlagged(t *testing.T) {
	h := newHarness(t)
	cfg := fastConfig("contact")
	cfg.FrozenEnterCount = 100
	cfg.FrozenEnterMinDuration = domain.Duration(10 * time.Minute)
	h.registerType("door", "contact", cfg)
	base := h.now
	for i := int64(1); i <= 20; i++ {
		h.sample("door", i, "CLOSED", base.Add(time.Duration(i)*time.Second))
	}
	if len(h.openAlerts("door", domain.KindFrozen)) != 0 {
		t.Fatalf("stationary contact device must not be flagged by temp thresholds")
	}
}

// TestGap_JumpOpensAndBackfillRecovers verifies a sequence hole opens after
// MissingEnterCount, and an out-of-order BATCH backfill closes it.
func TestGap_JumpOpensAndBackfillRecovers(t *testing.T) {
	h := newHarness(t)
	cfg := fastConfig("t")
	h.registerType("d", "t", cfg)
	base := h.now
	h.sample("d", 1, "a", base)
	// Jump 1 -> 5 leaves 3 missing (2,3,4): meets enter count exactly.
	h.sample("d", 5, "e", base.Add(40*time.Millisecond))
	opens := h.openAlerts("d", domain.KindGap)
	if len(opens) != 1 {
		t.Fatalf("expected gap alert, got %d", len(opens))
	}
	if opens[0].TriggerRange.SeqStart != 2 || opens[0].TriggerRange.SeqEnd != 5 {
		t.Fatalf("gap trigger range wrong: %+v", opens[0].TriggerRange)
	}

	// Batch backfill the missing seqs in ONE batch (arrive out of order).
	h.advance(time.Second)
	res, err := h.svc.IngestBatch(context.Background(), "d", []IncomingMessage{
		{Seq: 4, Value: "d", SampledAt: base.Add(30 * time.Millisecond), ReceivedAt: h.now},
		{Seq: 2, Value: "b", SampledAt: base.Add(10 * time.Millisecond), ReceivedAt: h.now},
		{Seq: 3, Value: "c", SampledAt: base.Add(20 * time.Millisecond), ReceivedAt: h.now},
	})
	if err != nil {
		t.Fatal(err)
	}
	if res.Accepted != 3 {
		t.Fatalf("backfill accepted=%d", res.Accepted)
	}
	if len(h.openAlerts("d", domain.KindGap)) != 0 {
		t.Fatalf("gap should recover after full backfill")
	}
	rec := h.recoveredAlerts("d", domain.KindGap)
	if len(rec) != 1 || rec[0].RecoverRange == nil {
		t.Fatalf("expected recovery range, got %+v", rec)
	}
}

// TestGap_BelowEnterThresholdNoAlert ensures a small 1-2 seq hiccup under the
// enter count is tolerated and silently healed by backfill.
func TestGap_BelowEnterThresholdNoAlert(t *testing.T) {
	h := newHarness(t)
	cfg := fastConfig("t")
	h.registerType("d", "t", cfg)
	base := h.now
	h.sample("d", 1, "a", base)
	h.sample("d", 3, "c", base.Add(20*time.Millisecond)) // only seq 2 missing
	if len(h.openAlerts("d", domain.KindGap)) != 0 {
		t.Fatal("2-missing? here only 1 missing, must not alert")
	}
	h.sample("d", 2, "b", base.Add(10*time.Millisecond)) // late backfill
	health, _ := h.svc.Health(context.Background(), "d")
	if !health.Healthy {
		t.Fatalf("small hole + backfill must stay healthy: %+v", health.OpenAlerts)
	}
}

// TestClockRollback_OpensNewEpoch: after an epoch is established, a forward
// sequence with a device clock wound far backwards must split the sequence
// line; open frozen/gap alerts are closed at the boundary and the epoch bumps.
func TestClockRollback_OpensNewEpoch(t *testing.T) {
	h := newHarness(t)
	cfg := fastConfig("t")
	h.registerType("d", "t", cfg)
	base := h.now
	// Establish epoch 0 with frozen samples, trigger frozen.
	for i := int64(1); i <= 4; i++ {
		h.sample("d", i, "9", base.Add(time.Duration(i)*10*time.Millisecond))
	}
	if len(h.openAlerts("d", domain.KindFrozen)) != 1 {
		t.Fatal("setup: frozen should be open")
	}
	// Device clock jumps back 60s (> stale tolerance 10s) while seq advances.
	rollbackTime := base.Add(-60 * time.Second)
	h.advance(time.Second)
	h.sample("d", 5, "100", rollbackTime)

	health, err := h.svc.Health(context.Background(), "d")
	if err != nil {
		t.Fatal(err)
	}
	if health.Epoch != 1 {
		t.Fatalf("expected new epoch 1, got %d", health.Epoch)
	}
	if len(h.openAlerts("d", domain.KindFrozen)) != 0 {
		t.Fatalf("open frozen alert must be closed at epoch boundary")
	}
	if len(h.recoveredAlerts("d", domain.KindFrozen)) != 1 {
		t.Fatalf("frozen should be recorded recovered")
	}
	// A new forward hole in the new epoch works independently.
	h.sample("d", 9, "x", rollbackTime.Add(40*time.Millisecond)) // gap in epoch 1
	if len(h.openAlerts("d", domain.KindGap)) != 1 {
		t.Fatalf("expected fresh gap in new epoch")
	}
}

// TestRestart_PersistsState: an open alert survives a process restart and
// continues to recover correctly.
func TestRestart_PersistsState(t *testing.T) {
	h := newHarness(t)
	cfg := fastConfig("t")
	h.registerType("d", "t", cfg)
	base := h.now
	for i := int64(1); i <= 4; i++ {
		h.sample("d", i, "5", base.Add(time.Duration(i)*10*time.Millisecond))
	}
	if len(h.openAlerts("d", domain.KindFrozen)) != 1 {
		t.Fatal("setup frozen")
	}

	h.reopen() // simulate restart

	health, err := h.svc.Health(context.Background(), "d")
	if err != nil {
		t.Fatal(err)
	}
	if !containsKind(health.OpenAlerts, domain.KindFrozen) {
		t.Fatalf("frozen alert must survive restart: %+v", health.OpenAlerts)
	}
	// Recovery works after restart using persisted state.
	h.sample("d", 5, "6", base.Add(60*time.Millisecond))
	h.sample("d", 6, "7", base.Add(70*time.Millisecond))
	if len(h.openAlerts("d", domain.KindFrozen)) != 0 {
		t.Fatal("frozen should recover after restart")
	}
}

// TestTimestampJitter_DoesNotOpenEpoch verifies small sample-time jitter and
// equal timestamps do not split epochs.
func TestTimestampJitter_DoesNotOpenEpoch(t *testing.T) {
	h := newHarness(t)
	cfg := fastConfig("t")
	h.registerType("d", "t", cfg)
	base := h.now
	h.sample("d", 1, "1", base)
	// Slight backward jitter (well within stale tolerance) on next forward seq.
	h.sample("d", 2, "2", base.Add(-500*time.Millisecond))
	// Equal timestamp.
	h.sample("d", 3, "3", base.Add(-500*time.Millisecond))
	health, _ := h.svc.Health(context.Background(), "d")
	if health.Epoch != 0 {
		t.Fatalf("jitter must not open epoch, got %d", health.Epoch)
	}
	if !health.Healthy {
		t.Fatalf("jittering healthy device must stay healthy: %+v", health.OpenAlerts)
	}
}

// TestDuplicateReplay_IsIdempotent verifies re-sending the same seq is a
// no-op and does not inflate the frozen run.
func TestDuplicateReplay_IsIdempotent(t *testing.T) {
	h := newHarness(t)
	cfg := fastConfig("t")
	h.registerType("d", "t", cfg)
	base := h.now
	for i := int64(1); i <= 3; i++ {
		h.sample("d", i, "5", base.Add(time.Duration(i)*10*time.Millisecond))
	}
	// Replay the same seqs repeatedly.
	for rep := 0; rep < 3; rep++ {
		for i := int64(1); i <= 3; i++ {
			h.sample("d", i, "5", base.Add(time.Duration(i)*10*time.Millisecond))
		}
	}
	if len(h.openAlerts("d", domain.KindFrozen)) != 0 {
		t.Fatalf("duplicates must not extend frozen run past threshold 4")
	}
}

// TestForwardRebase_OpensNewEpoch verifies that a forward sequence jump larger
// than the backfill window is treated as a new sequence line, NOT as thousands
// of missing sequences.
func TestForwardRebase_OpensNewEpoch(t *testing.T) {
	h := newHarness(t)
	cfg := fastConfig("t") // BackfillLookback 50
	h.registerType("d", "t", cfg)
	base := h.now
	h.sample("d", 10, "1", base)
	h.sample("d", 5000, "2", base.Add(time.Second)) // jump far beyond lookback

	health, err := h.svc.Health(context.Background(), "d")
	if err != nil {
		t.Fatal(err)
	}
	if health.Epoch != 1 {
		t.Fatalf("huge forward jump must open epoch 1, got %d", health.Epoch)
	}
	if len(h.openAlerts("d", domain.KindGap)) != 0 {
		t.Fatalf("forward rebase must NOT raise a gap alert")
	}
	if health.LastSeq != 5000 {
		t.Fatalf("high seq in new epoch = %d, want 5000", health.LastSeq)
	}
}

func containsKind(ks []domain.Kind, want domain.Kind) bool {
	for _, k := range ks {
		if k == want {
			return true
		}
	}
	return false
}
