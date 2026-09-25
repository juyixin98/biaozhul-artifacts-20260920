package scheduler

import "fmt"

// TaskRuntime is a task's mutable run-time state, handed to the Executor.
// Scheduling fields are exported; bookkeeping is accessible via methods so an
// alternative Executor implementation can drive the simulation differently.
type TaskRuntime struct {
	ID        string
	Base      int
	Effective int
	State     TaskState
	Arrival   int64

	prog Program
	pc   int
	// Remaining is the number of CPU ticks left in the current cpu instruction.
	Remaining int

	held      []string // locks currently held, in acquisition order
	waitingOn string   // lock this task is blocked on ("" if not blocked)

	cpuTicks     int64
	blockedTicks int64
	readyTicks   int64
	boosts       int
	finishTick   int64
}

// Program returns the task's instruction list.
func (t *TaskRuntime) Program() Program { return t.prog }

// PC returns the index of the next instruction to execute.
func (t *TaskRuntime) PC() int { return t.pc }

// HeldLocks returns the locks the task currently holds (acquisition order).
func (t *TaskRuntime) HeldLocks() []string { return append([]string(nil), t.held...) }

// Holds reports whether the task holds lock id.
func (t *TaskRuntime) Holds(id string) bool {
	for _, h := range t.held {
		if h == id {
			return true
		}
	}
	return false
}

// WaitingOn returns the lock id the task is blocked on ("" if none).
func (t *TaskRuntime) WaitingOn() string { return t.waitingOn }

// Executor abstracts "how a task consumes its program". The scheduler kernel
// decides when a task runs or blocks; the executor tracks program position and
// decodes instructions. Supplying a custom Executor lets a caller run scripts,
// instrumented programs, or replayed traces through the same scheduler.
type Executor interface {
	// Name identifies the executor (recorded nowhere mandatory; useful in logs).
	Name() string
	// Init binds a fresh task to its program.
	Init(t *TaskRuntime, p Program)
	// Current returns the instruction at the task's program counter and false
	// when the program has finished.
	Current(t *TaskRuntime) (Instr, bool)
	// Advance moves past the current instruction. It is called when a lock
	// instruction has been satisfied (acquired after possibly blocking) or an
	// unlock instruction has been performed. CPU instructions consume ticks via
	// the task's Remaining counter and do not call Advance until it reaches 0.
	Advance(t *TaskRuntime)
}

// ProgramExecutor is the default Executor: it walks Program instructions in
// order, tracking a per-task program counter.
type ProgramExecutor struct{}

func NewProgramExecutor() *ProgramExecutor { return &ProgramExecutor{} }

func (ProgramExecutor) Name() string { return "program" }

func (ProgramExecutor) Init(t *TaskRuntime, p Program) {
	t.prog = p
	t.pc = 0
	t.Remaining = 0
}

func (ProgramExecutor) Current(t *TaskRuntime) (Instr, bool) {
	if t.pc >= len(t.prog) {
		return Instr{}, false
	}
	return t.prog[t.pc], true
}

func (ProgramExecutor) Advance(t *TaskRuntime) {
	if t.pc < len(t.prog) {
		t.pc++
	}
	t.Remaining = 0
}

// Validate checks a single program for static errors (unknown/duplicate
// instructions are checked against the set of declared locks).
func Validate(p Program, locks map[string]struct{}) error {
	for i, in := range p {
		switch in.Op {
		case OpCPU:
			if in.Ticks <= 0 {
				return fmt.Errorf("instruction %d: cpu requires ticks > 0", i)
			}
		case OpLock, OpUnlock:
			if in.Lock == "" {
				return fmt.Errorf("instruction %d: %s requires a lock", i, in.Op)
			}
			if locks != nil {
				if _, ok := locks[in.Lock]; !ok {
					return fmt.Errorf("instruction %d: unknown lock %q", i, in.Lock)
				}
			}
		default:
			return fmt.Errorf("instruction %d: unknown op %q", i, in.Op)
		}
	}
	return nil
}
