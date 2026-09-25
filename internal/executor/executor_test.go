package executor

import (
	"testing"

	"pim/internal/scenario"
	"pim/internal/scheduler"
)

func TestRecordingExecutorCapturesRunOrder(t *testing.T) {
	rec := NewRecording()
	res, err := scheduler.RunWith(scenario.ClassicInversion(true),
		scheduler.Config{Executor: rec})
	if err != nil {
		t.Fatalf("RunWith: %v", err)
	}
	if len(rec.Runs) != int(res.FinishTime) {
		t.Fatalf("recorded %d ticks, makespan = %d", len(rec.Runs), res.FinishTime)
	}
	// Every recorded view must carry valid timing data and a monotonic tick.
	lastTick := int64(0)
	for _, e := range rec.Runs {
		if e.TaskID == "" {
			t.Errorf("empty task id in execution view: %+v", e)
		}
		if e.Tick <= lastTick {
			t.Errorf("tick not monotonic: %d after %d", e.Tick, lastTick)
		}
		lastTick = e.Tick
	}
	// Low must execute at least one tick at inherited priority 3 (the
	// whole point of the protocol in this workload).
	sawInherited := false
	for _, e := range rec.Runs {
		if e.TaskID == "Low" && e.EffectivePriority == 3 {
			sawInherited = true
		}
	}
	if !sawInherited {
		t.Error("recording never shows Low running at inherited priority 3")
	}
}
