package scheduler

import (
	"context"
	"fmt"
	"os/exec"
	"time"
)

// ---------------------------------------------------------------------------
// SimExecutor: tasks "run" for their declared duration. Designed for use with
// FakeClock: it arms an AfterFunc on the clock, so completion happens exactly
// at virtual now+duration with no real delay and in deterministic order.
// ---------------------------------------------------------------------------

// SimExecutor completes tasks after their Duration. With a FakeClock this is
// instantaneous and deterministic; with a RealClock it waits real time.
type SimExecutor struct {
	clock Clock
	out   chan ExecResult
}

// NewSimExecutor creates a simulated executor on the given clock.
func NewSimExecutor(clock Clock) *SimExecutor {
	return &SimExecutor{clock: clock, out: make(chan ExecResult, 1024)}
}

// Start schedules a synthetic completion after t.Duration.
func (e *SimExecutor) Start(t *Task, now time.Time) error {
	if t.Duration <= 0 {
		return fmt.Errorf("sim executor requires positive duration, got %s", t.Duration)
	}
	taskID := t.ID
	e.clock.AfterFunc(t.Duration, func() {
		e.out <- ExecResult{TaskID: taskID}
	})
	return nil
}

// Results returns the completion channel.
func (e *SimExecutor) Results() <-chan ExecResult { return e.out }

// ---------------------------------------------------------------------------
// ProcessExecutor: runs an OS command per task. Used with RealClock only.
// ---------------------------------------------------------------------------

// ProcessExecutor executes task.Spec as a shell command. Environment variables
// carrying the task identity and resource grant are exposed to the command.
type ProcessExecutor struct {
	out chan ExecResult
}

// NewProcessExecutor creates an executor that runs real processes.
func NewProcessExecutor() *ProcessExecutor {
	return &ProcessExecutor{out: make(chan ExecResult, 1024)}
}

// Start runs `sh -c <spec>` asynchronously and reports its exit result.
func (e *ProcessExecutor) Start(t *Task, now time.Time) error {
	if t.Spec == "" {
		return fmt.Errorf("process executor requires a non-empty command spec")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cmd := exec.CommandContext(ctx, "sh", "-c", t.Spec)
	cmd.Env = append(cmd.Environ(),
		"DRF_TASK_ID="+t.ID,
		"DRF_TENANT_ID="+t.TenantID,
		fmt.Sprintf("DRF_CPU_MILLICPU=%d", t.Req.CPU),
		fmt.Sprintf("DRF_MEMORY_MIB=%d", t.Req.Memory),
	)
	t.cancel = cancel
	go func() {
		err := cmd.Run()
		e.out <- ExecResult{TaskID: t.ID, Err: err}
	}()
	return nil
}

// Results returns the completion channel.
func (e *ProcessExecutor) Results() <-chan ExecResult { return e.out }
