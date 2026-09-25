package scheduler

// Executor abstracts task execution. When a task starts the scheduler
// calls Run; the executor MUST call complete exactly once when the task
// ends (success or failure are not distinguished for scheduling).
type Executor interface {
	Run(task TaskSpec, complete func())
}

// TimedExecutor runs each task for its declared duration on the given
// clock. It is the executor used by the real HTTP service; the same
// executor combined with a FakeClock drives deterministic simulations.
type TimedExecutor struct {
	clock Clock
}

func NewTimedExecutor(clock Clock) *TimedExecutor {
	return &TimedExecutor{clock: clock}
}

func (e *TimedExecutor) Run(task TaskSpec, complete func()) {
	e.clock.After(task.Duration.Duration, complete)
}

// noopExecutor keeps a task running until Complete is called externally.
// Used in unit tests that want manual completion control.
type noopExecutor struct{}

func (noopExecutor) Run(TaskSpec, func()) {}
