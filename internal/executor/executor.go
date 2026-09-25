// Package executor contains alternative, pluggable Executor implementations
// that show how the discrete scheduler maps onto real execution backends.
package executor

import (
	"time"

	"pim/internal/scheduler"
)

// PacedExecutor sleeps for a fixed physical duration per logical tick. When
// combined with a custom wall clock it demonstrates wiring the model to a
// real-time backend; the scheduling decisions remain fully deterministic.
type PacedExecutor struct {
	TickDuration time.Duration
}

// NewPacedExecutor returns an executor pacing every tick at d.
func NewPacedExecutor(d time.Duration) *PacedExecutor { return &PacedExecutor{TickDuration: d} }

// Tick blocks for TickDuration.
func (p *PacedExecutor) Tick(_ scheduler.TaskExecution) {
	time.Sleep(p.TickDuration)
}

// RecordingExecutor records each tick's task and effective priority. It is
// primarily useful in tests that want to assert the processor's run order
// independently of the event stream.
type RecordingExecutor struct {
	Runs []scheduler.TaskExecution
}

// NewRecording returns an empty recording executor.
func NewRecording() *RecordingExecutor { return &RecordingExecutor{} }

// Tick appends the execution view.
func (r *RecordingExecutor) Tick(e scheduler.TaskExecution) {
	r.Runs = append(r.Runs, e)
}

// TaskSequence returns just the ordered task IDs that occupied the CPU.
func (r *RecordingExecutor) TaskSequence() []string {
	out := make([]string, len(r.Runs))
	for i, e := range r.Runs {
		out[i] = e.TaskID
	}
	return out
}
