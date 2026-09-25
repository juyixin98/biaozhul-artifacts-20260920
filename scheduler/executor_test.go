package scheduler

import (
	"testing"
)

// scriptedExecutor demonstrates replaceability: it ignores Program and replays
// a fixed script of instructions per task, driving the same kernel.
type scriptedExecutor struct {
	scripts map[string][]Instr
}

func (e *scriptedExecutor) Name() string { return "scripted" }
func (e *scriptedExecutor) Init(t *TaskRuntime, _ Program) {
	t.prog = e.scripts[t.ID]
	t.pc = 0
}
func (e *scriptedExecutor) Current(t *TaskRuntime) (Instr, bool) {
	if t.pc >= len(t.prog) {
		return Instr{}, false
	}
	return t.prog[t.pc], true
}
func (e *scriptedExecutor) Advance(t *TaskRuntime) {
	t.pc++
	t.Remaining = 0
}

// TestPluggableExecutor runs the kernel with an executor that supplies its own
// script at run time, proving the scheduling policy is independent of program
// representation.
func TestPluggableExecutor(t *testing.T) {
	// Config declares throwaway programs; the scripted executor overrides them.
	cfg := Config{
		Locks: []LockSpec{{ID: "A"}},
		Tasks: []TaskSpec{
			{ID: "x", Base: 5, Arrival: 0, Program: Program{{Op: OpCPU, Ticks: 1}}},
			{ID: "y", Base: 9, Arrival: 1, Program: Program{{Op: OpCPU, Ticks: 1}}},
		},
	}
	exec := &scriptedExecutor{scripts: map[string][]Instr{
		"x": {{Op: OpCPU, Ticks: 4}},
		"y": {{Op: OpCPU, Ticks: 2}},
	}}
	s, err := New(cfg, WithExecutor(exec))
	if err != nil {
		t.Fatal(err)
	}
	r := s.Run()
	if !r.Completed {
		t.Fatalf("should complete: %q", r.Fatal)
	}
	if got := findTask(r, "y").FinishTick; got != 3 {
		t.Errorf("y finish = %d, want 3", got)
	}
	if got := findTask(r, "x").FinishTick; got != 6 {
		t.Errorf("x finish = %d, want 6", got)
	}
}
