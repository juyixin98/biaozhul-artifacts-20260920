package analysis

import (
	"math"
	"testing"

	"cgroup-analyzer/internal/cgroup"
	"cgroup-analyzer/internal/fixture"
)

func cpu(usageUsec uint64) cgroup.CPUStat { return cgroup.CPUStat{UsageUsec: usageUsec} }

func memEv(maxV, oomKill uint64) cgroup.MemoryEvents {
	return cgroup.MemoryEvents{Max: maxV, OomKill: oomKill}
}

const fixtureRoot = "../../fixtures"

func approx(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

func loadInstance(t *testing.T, container, instance string) fixture.Instance {
	t.Helper()
	insts, err := fixture.Load(fixtureRoot)
	if err != nil {
		t.Fatalf("load fixtures: %v", err)
	}
	for _, inst := range insts {
		if inst.Container == container && inst.Instance == instance {
			return inst
		}
	}
	t.Fatalf("instance %s/%s not found", container, instance)
	return fixture.Instance{}
}

func hasEventType(evs []Event, typ string) bool {
	for _, e := range evs {
		if e.Type == typ {
			return true
		}
	}
	return false
}

// web/boot-1: 6 samples, 10 s apart, usage_usec rises by exactly 5,000,000
// each interval => 0.5 cores every interval; throttled_usec rises 100,000 =>
// 0.01; oom_kill increments at seq 5; exit.json code 137 + oom_kill => oom.
func TestWebBoot1HandComputed(t *testing.T) {
	r := Analyze(loadInstance(t, "web", "boot-1"))
	if r.SampleCount != 6 {
		t.Fatalf("want 6 samples, got %d", r.SampleCount)
	}
	if len(r.Intervals) != 5 {
		t.Fatalf("want 5 intervals, got %d", len(r.Intervals))
	}
	for i, iv := range r.Intervals {
		if iv.DurationSeconds != 10 {
			t.Fatalf("interval %d: want dt 10s, got %v", i, iv.DurationSeconds)
		}
		if iv.CPUCores == nil || !approx(*iv.CPUCores, 0.5) {
			t.Fatalf("interval %d: want 0.5 cores, got %v", i, iv.CPUCores)
		}
		if iv.ThrottleRatio == nil || !approx(*iv.ThrottleRatio, 0.01) {
			t.Fatalf("interval %d: want throttle 0.01, got %v", i, iv.ThrottleRatio)
		}
		if iv.PSICPUStallUsec == nil || !approx(*iv.PSICPUStallUsec, 50000.0) {
			t.Fatalf("interval %d: want cpu psi 50000 us/s, got %v", i, iv.PSICPUStallUsec)
		}
		if iv.MemUsageRatioEnd == nil || !approx(*iv.MemUsageRatioEnd, 0.390625) {
			t.Fatalf("interval %d: want mem ratio 0.390625, got %v", i, iv.MemUsageRatioEnd)
		}
		if iv.CounterReset || iv.Gap {
			t.Fatalf("interval %d: unexpected reset/gap", i)
		}
	}
	if !hasEventType(r.Events, "oom_kill") {
		t.Fatalf("want oom_kill event, got %+v", r.Events)
	}
	// The increment oom_kill 0 -> 1 is observed at seq 5.
	var oomAt5 bool
	for _, e := range r.Events {
		if e.Type == "oom_kill" && e.Seq == 5 {
			oomAt5 = true
			if len(e.Evidence) == 0 {
				t.Fatal("oom_kill event without evidence")
			}
		}
	}
	if !oomAt5 {
		t.Fatalf("want oom_kill at seq 5, got %+v", r.Events)
	}
	if hasEventType(r.Events, "normal_exit") || hasEventType(r.Events, "incomplete_data") {
		t.Fatalf("boot-1 ends by OOM, got %+v", r.Events)
	}
}

// web/boot-2: same name, new instance, 3 samples at 0.2 cores, no exit.json.
func TestWebBoot2NewInstance(t *testing.T) {
	r := Analyze(loadInstance(t, "web", "boot-2"))
	if r.SampleCount != 3 || len(r.Intervals) != 2 {
		t.Fatalf("unexpected report size: %+v", r)
	}
	for i, iv := range r.Intervals {
		if iv.CPUCores == nil || !approx(*iv.CPUCores, 0.2) {
			t.Fatalf("interval %d: want 0.2 cores, got %v", i, iv.CPUCores)
		}
	}
	if !hasEventType(r.Events, "incomplete_data") {
		t.Fatalf("want incomplete_data for missing exit.json, got %+v", r.Events)
	}
}

// batch/run-1: gap (000003 lost), cpu usage counter resets at 000004,
// memory.current missing at 000004, memory.events max 0 -> 2 at 000005,
// exit code 0 with no oom_kill => normal_exit.
func TestBatchRun1ResetGapAndNormalExit(t *testing.T) {
	r := Analyze(loadInstance(t, "batch", "run-1"))
	if len(r.Intervals) != 3 {
		t.Fatalf("want 3 intervals, got %d", len(r.Intervals))
	}

	// interval 000001 -> 000002: (3,000,000 - 1,000,000)/10s/1e6 = 0.2
	iv := r.Intervals[0]
	if iv.CPUCores == nil || !approx(*iv.CPUCores, 0.2) {
		t.Fatalf("first interval: want 0.2 cores, got %v", iv.CPUCores)
	}

	// interval 000002 -> 000004: gap + reset
	iv = r.Intervals[1]
	if !iv.Gap || len(iv.MissingSamples) != 1 || iv.MissingSamples[0] != 3 {
		t.Fatalf("want gap missing [3], got gap=%v missing=%v", iv.Gap, iv.MissingSamples)
	}
	if !approx(iv.DurationSeconds, 20) {
		t.Fatalf("want 20 s duration, got %v", iv.DurationSeconds)
	}
	if iv.CPUCores != nil {
		t.Fatalf("counter reset must suppress rate, never go negative: got %v", *iv.CPUCores)
	}
	if !iv.CounterReset {
		t.Fatalf("want CounterReset=true")
	}
	if iv.MemCurrentEnd != nil {
		t.Fatalf("memory.current missing at seq 4 must be nil, got %v", iv.MemCurrentEnd)
	}

	// interval 000004 -> 000005: (1,000,500 - 500)/10s/1e6 = 0.1;
	// memory at 100% of limit.
	iv = r.Intervals[2]
	if iv.CPUCores == nil || !approx(*iv.CPUCores, 0.1) {
		t.Fatalf("last interval: want 0.1 cores, got %v", iv.CPUCores)
	}
	if iv.MemUsageRatioEnd == nil || !approx(*iv.MemUsageRatioEnd, 1.0) {
		t.Fatalf("want mem ratio 1.0, got %v", iv.MemUsageRatioEnd)
	}

	if !hasEventType(r.Events, "sample_gap") {
		t.Fatalf("want sample_gap event, got %+v", r.Events)
	}
	if !hasEventType(r.Events, "counter_reset") {
		t.Fatalf("want counter_reset event, got %+v", r.Events)
	}
	if !hasEventType(r.Events, "memory_max_hit") {
		t.Fatalf("want memory_max_hit event, got %+v", r.Events)
	}
	if !hasEventType(r.Events, "normal_exit") {
		t.Fatalf("want normal_exit event, got %+v", r.Events)
	}
	if hasEventType(r.Events, "oom_kill") {
		t.Fatalf("must not report oom_kill: %+v", r.Events)
	}
}

// Synthetic: a counter regression never yields a negative rate.
func TestCounterRegressionNeverNegative(t *testing.T) {
	inst := fixture.Instance{Container: "c", Instance: "i"}
	mk := func(seq int, ts int64, usage uint64) fixture.Sample {
		c := fixture.Sample{Seq: seq, Timestamp: ts}
		cpu := cpu(usage)
		c.CPU = &cpu
		return c
	}
	inst.Samples = []fixture.Sample{mk(1, 0, 5_000_000), mk(2, 10, 1_000_000)}
	r := Analyze(inst)
	if r.Intervals[0].CPUCores != nil {
		t.Fatalf("regressed counter rate must be nil, got %v", *r.Intervals[0].CPUCores)
	}
	if !r.Intervals[0].CounterReset || !hasEventType(r.Events, "counter_reset") {
		t.Fatalf("want counter_reset flagged: %+v", r)
	}
}

// Synthetic: non-positive time delta does not divide by zero or emit a rate.
func TestNonPositiveDelta(t *testing.T) {
	inst := fixture.Instance{Container: "c", Instance: "i"}
	mk := func(seq int, ts int64, usage uint64) fixture.Sample {
		c := fixture.Sample{Seq: seq, Timestamp: ts}
		cpu := cpu(usage)
		c.CPU = &cpu
		return c
	}
	inst.Samples = []fixture.Sample{mk(1, 10, 0), mk(2, 10, 5_000_000)}
	r := Analyze(inst)
	if len(r.Intervals) != 1 || r.Intervals[0].CPUCores != nil {
		t.Fatalf("want no rate for dt=0, got %+v", r.Intervals)
	}
}

// Synthetic: no oom, non-zero exit code => abnormal_exit, not oom_kill.
func TestAbnormalExitIsNotOOM(t *testing.T) {
	ev := memEv(0, 0)
	inst := fixture.Instance{
		Container: "c", Instance: "i",
		Samples: []fixture.Sample{{Seq: 1, Timestamp: 0, MemEvents: &ev}},
		Exit:    &fixture.ExitInfo{Code: 1, Reason: "panic", Time: 5},
	}
	r := Analyze(inst)
	if !hasEventType(r.Events, "abnormal_exit") || hasEventType(r.Events, "oom_kill") {
		t.Fatalf("want abnormal_exit only, got %+v", r.Events)
	}
}
