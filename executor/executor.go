// Package executor contains the replaceable execution back ends used by the
// scheduler. An executor receives a job payload and a context that is canceled
// (with a cause) when the scheduler kills the job, and reports the time the
// execution actually occupied as measured by the injected clock.
package executor

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"

	"deadlineadm/clock"
)

// Result reports how one execution ended.
type Result struct {
	// Ran is the time the execution occupied, measured executor-side. With a
	// fake clock in tests this is simulated time; in production it is
	// wall-clock time.
	Ran time.Duration
	// Err is nil on success. When the context was canceled it is one of
	// ErrKilled (budget/deadline bound) or ErrCanceled (explicit cancel);
	// otherwise it describes the execution failure.
	Err error
}

// Executor runs one job. Start must be non-blocking: it launches the work and
// returns immediately; exactly one Result is delivered on the channel, which
// is then closed.
type Executor interface {
	Start(ctx context.Context, jobID, payload string) <-chan Result
}

var (
	// ErrKilled means the running job was killed at its declared execution
	// upper bound or at the deadline safety bound.
	ErrKilled = errors.New("executor: killed (declared bound or deadline reached)")
	// ErrCanceled means the running job was stopped by an explicit cancel.
	ErrCanceled = errors.New("executor: canceled")
)

// classify maps a finished context to the package error, or nil when the
// context is still alive (ordinary command failure).
func classify(ctx context.Context) error {
	if ctx.Err() == nil {
		return nil
	}
	if errors.Is(context.Cause(ctx), context.DeadlineExceeded) {
		return ErrKilled
	}
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return ErrKilled
	}
	return ErrCanceled
}

// ---- ScriptExecutor: clock-driven simulated work, for tests and demos ----

// Timer is re-exported from the clock package so executors depend on one
// timer shape (it is structurally identical to clock.Timer).
type Timer = clock.Timer

// Clock is the time source an executor measures against (clock.Clock).
type Clock = clock.Clock

// ScriptExecutor interprets payloads as tiny deterministic "scripts":
//
//	sleep:<millis>            occupy for the given duration, succeed
//	sleep:<millis>,fail       same, but end with an execution error
//	fail:<message>            fail immediately
//
// Sleeping and duration measurement use the injected clock, so a FakeClock
// makes long jobs instant and fully deterministic.
type ScriptExecutor struct {
	Clock Clock

	mu       sync.Mutex
	inflight sync.WaitGroup
	// doneCh is closed/broadcast each time a script goroutine finishes.
	doneMu sync.Mutex
	doneCh chan struct{}
}

// NewScriptExecutor builds a script executor over a clock.
func NewScriptExecutor(c Clock) *ScriptExecutor {
	return &ScriptExecutor{Clock: c, doneCh: make(chan struct{})}
}

// WaitIdle blocks until every started script has delivered its result. Tests
// use it after advancing a fake clock to deterministically drain executor
// goroutines before draining the scheduler actor.
func (e *ScriptExecutor) WaitIdle() { e.inflight.Wait() }

// signalDone marks one script goroutine finished and wakes anyone blocked in
// WaitFinished.
func (e *ScriptExecutor) signalDone() {
	e.doneMu.Lock()
	close(e.doneCh)
	e.doneCh = make(chan struct{})
	e.doneMu.Unlock()
}

// WaitFinished blocks until at least one script goroutine finishes after the
// call starts, or until d elapses. Deterministic test drivers use it after
// firing a single completion/kill waiter; the short timeout keeps a stale
// timer firing (whose owning goroutine already ended via cancellation) from
// blocking the run.
func (e *ScriptExecutor) WaitFinished(d time.Duration) bool {
	e.doneMu.Lock()
	ch := e.doneCh
	e.doneMu.Unlock()
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ch:
		return true
	case <-timer.C:
		return false
	}
}

func parseScript(payload string) (sleep time.Duration, fail bool, msg string, err error) {
	p := strings.TrimSpace(payload)
	if strings.HasPrefix(p, "sleep:") {
		rest := strings.TrimPrefix(p, "sleep:")
		parts := strings.SplitN(rest, ",", 2)
		ms, perr := strconv.Atoi(strings.TrimSpace(parts[0]))
		if perr != nil || ms < 0 {
			return 0, false, "", fmt.Errorf("invalid sleep script %q: need sleep:<nonnegative millis>", payload)
		}
		if len(parts) == 2 && strings.TrimSpace(parts[1]) == "fail" {
			fail = true
		}
		return time.Duration(ms) * time.Millisecond, fail, "", nil
	}
	if strings.HasPrefix(p, "fail:") {
		return 0, true, strings.TrimSpace(strings.TrimPrefix(p, "fail:")), nil
	}
	return 0, false, "", fmt.Errorf("unrecognized script %q (want sleep:<ms>[,fail] or fail:<msg>)", payload)
}

// deliverOK sends a natural-completion result for a scripted sleep.
func deliverOK(out chan<- Result, c Clock, start time.Time, willFail bool, msg string) {
	var rerr error
	if willFail {
		if msg != "" {
			rerr = errors.New(msg)
		} else {
			rerr = errors.New("script execution failed")
		}
	}
	out <- Result{Ran: c.Now().Sub(start), Err: rerr}
	close(out)
}

func (e *ScriptExecutor) Start(ctx context.Context, jobID, payload string) <-chan Result {
	out := make(chan Result, 1)
	sleep, willFail, msg, err := parseScript(payload)
	if err != nil {
		out <- Result{Ran: 0, Err: err}
		close(out)
		return out
	}

	e.inflight.Add(1)
	start := e.Clock.Now()
	// A zero-duration script completes immediately without a timer: a fake
	// clock never fires timers on its own, so waiting on a zero timer would
	// block forever, and a real zero timer would race delivery anyway.
	if sleep <= 0 {
		var rerr error
		if willFail {
			if msg != "" {
				rerr = errors.New(msg)
			} else {
				rerr = errors.New("script execution failed")
			}
		}
		out <- Result{Ran: 0, Err: rerr}
		close(out)
		e.inflight.Done()
		return out
	}
	// Register the completion timer synchronously, before Start returns to
	// the scheduler. The scheduler installs the job's kill-bound context only
	// afterwards, so at an exact tie (honest job finishing on its declared
	// bound) the completion event is registered first and wins — the job is
	// recorded as on-time rather than spuriously killed.
	timer := e.Clock.NewTimer(sleep)
	go func() {
		defer e.inflight.Done()
		defer e.signalDone()
		t := timer
		tryComplete := func() bool {
			select {
			case <-t.C():
				deliverOK(out, e.Clock, start, willFail, msg)
				return true
			default:
				return false
			}
		}
		// Deterministic tie handling: at the declared bound the completion
		// timer and kill context can both be ready at once; natural completion
		// always wins (an honest job ending exactly on its bound is on time).
		if tryComplete() {
			return
		}
		select {
		case <-t.C():
			deliverOK(out, e.Clock, start, willFail, msg)
		case <-ctx.Done():
			if tryComplete() {
				return
			}
			t.Stop()
			out <- Result{Ran: e.Clock.Now().Sub(start), Err: classify(ctx)}
			close(out)
		}
	}()
	return out
}

// ---- CommandExecutor: real local processes for the HTTP server ----

// CommandExecutor runs payloads via "sh -c" as local processes. A process
// still running when ctx is canceled is terminated by exec.CommandContext.
// This is a single-machine, local-only interface: bind it to trusted
// networks only.
type CommandExecutor struct{}

// NewCommandExecutor builds a local command executor.
func NewCommandExecutor() *CommandExecutor { return &CommandExecutor{} }

func (e *CommandExecutor) Start(ctx context.Context, jobID, payload string) <-chan Result {
	out := make(chan Result, 1)
	start := time.Now()
	cmd := exec.CommandContext(ctx, "sh", "-c", payload)
	if err := cmd.Start(); err != nil {
		out <- Result{Ran: 0, Err: fmt.Errorf("start command: %w", err)}
		close(out)
		return out
	}
	go func() {
		werr := cmd.Wait()
		ran := time.Since(start)
		if werr != nil {
			if ce := classify(ctx); ce != nil {
				out <- Result{Ran: ran, Err: ce}
			} else {
				out <- Result{Ran: ran, Err: werr}
			}
		} else {
			out <- Result{Ran: ran, Err: nil}
		}
		close(out)
	}()
	return out
}
