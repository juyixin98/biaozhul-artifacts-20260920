package tree

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"canceltree/internal/client"
)

// Run executes the task tree. It blocks until every subtask has stopped and
// every declared cleanup has finished, even when ctx is canceled (client
// disconnect) or a fatal sibling triggers cancellation.
//
// The returned Report is always fully populated; callers decide whether they
// can still write it to an HTTP response.
func (e *Engine) Run(ctx context.Context, reqID string, tasks []TaskSpec) Report {
	report := Report{RequestID: reqID, StartedAt: e.clk.Now()}
	if len(tasks) == 0 {
		report.Status = TreeSucceeded
		report.Outcomes = []TaskOutcome{}
		report.FinishedAt = e.clk.Now()
		return report
	}

	// siblingCtx is the child every task hangs off. It cancels when the root
	// cancels (client disconnect) OR when the first fatal failure fires.
	siblingCtx, cancelSiblings := context.WithCancel(ctx)
	defer cancelSiblings()

	var (
		fatalMu     sync.Mutex
		fatalFired  bool
		fatalTaskID string
	)
	markFatal := func(id string) {
		fatalMu.Lock()
		if !fatalFired {
			fatalFired = true
			fatalTaskID = id
		}
		fatalMu.Unlock()
		// Idempotent and safe to call from many task goroutines.
		cancelSiblings()
	}
	wasFatal := func() (bool, string) {
		fatalMu.Lock()
		defer fatalMu.Unlock()
		return fatalFired, fatalTaskID
	}

	outcomes := make([]TaskOutcome, len(tasks))
	var wg sync.WaitGroup
	for i := range tasks {
		spec := e.resolve(tasks[i]) // per-iteration copy, resolved once
		wg.Add(1)
		go func(index int) {
			defer wg.Done()
			outcomes[index] = e.runTask(siblingCtx, ctx, spec, markFatal)
		}(i)
	}
	wg.Wait()

	report.Outcomes = outcomes
	fired, id := wasFatal()
	switch {
	case ctx.Err() != nil:
		// Client disconnect wins final classification when both happen in
		// the same window; the fatal task is still visible in outcomes.
		report.Status = TreeCanceled
		report.CanceledBy = CanceledByClient
	case fired:
		report.Status = TreeFailed
		report.FatalTaskID = id
	default:
		// No fatal edge and the client stayed connected. A tree still
		// fails when any task actually failed or timed out (its work did
		// not complete); tasks canceled by a fatal sibling are already
		// covered by the fired branch above.
		report.Status = TreeSucceeded
		for _, o := range outcomes {
			if o.Status == StatusFailed || o.CanceledBy == CanceledByTimeout {
				report.Status = TreeFailed
				break
			}
		}
	}
	report.FinishedAt = e.clk.Now()
	report.ElapsedMS = report.FinishedAt.Sub(report.StartedAt).Milliseconds()
	return report
}

// resolve returns a copy of spec with relative URLs resolved against the
// engine base URL. Unresolvable specs keep their original target so the
// failure surfaces as an ordinary task failure.
func (e *Engine) resolve(spec TaskSpec) TaskSpec {
	out := spec
	if abs, err := client.ResolveURL(e.base, spec.Call.URL); err == nil {
		out.Call = spec.Call
		out.Call.URL = abs
	}
	if spec.Cleanup != nil {
		c := *spec.Cleanup
		if abs, err := client.ResolveURL(e.base, c.URL); err == nil {
			c.URL = abs
		}
		out.Cleanup = &c
	}
	return out
}

// runTask executes one subtask and its cleanup. root is the request root
// context (canceled on client disconnect); siblingCtx additionally cancels
// when a fatal sibling fires.
func (e *Engine) runTask(
	siblingCtx, root context.Context,
	spec TaskSpec, markFatal func(string),
) TaskOutcome {
	out := TaskOutcome{ID: spec.ID, Fatal: spec.Fatal, StartedAt: e.clk.Now()}

	taskCtx, cancelTask := context.WithCancel(siblingCtx)
	timedOut := e.watchTimeout(taskCtx, cancelTask, spec.Timeout)

	result := e.cl.Do(taskCtx, spec.Call)
	out.Result = result

	rootCanceled := root.Err() != nil
	switch {
	case result.OK():
		out.Status = StatusSucceeded
	case taskCtx.Err() != nil || resultIsCanceled(result):
		// The call was torn down mid-flight by timeout, fatal sibling or
		// client disconnect — distinguish which for the structured report.
		out.Status = StatusCanceled
		out.CanceledBy = classifyCancel(timedOut(), rootCanceled)
	default:
		// A real answer that is not 2xx, or a local client fault, while the
		// task was still running. Fatal ones cancel the sibling tree.
		out.Status = StatusFailed
		if spec.Fatal {
			markFatal(spec.ID)
		}
	}
	cancelTask()

	if spec.Cleanup != nil {
		e.runCleanup(root, &out, spec.Cleanup)
	}

	out.FinishedAt = e.clk.Now()
	out.DurationMS = out.FinishedAt.Sub(out.StartedAt).Milliseconds()
	return out
}

// watchTimeout cancels cancel when d elapses on the engine clock. The
// returned function reports whether that timeout actually fired. The watcher
// exits when the task context ends, so no goroutine outlives the request.
func (e *Engine) watchTimeout(taskCtx context.Context, cancel context.CancelFunc, d time.Duration) func() bool {
	if d <= 0 {
		return func() bool { return false }
	}
	var fired atomic.Bool
	t := e.clk.Timer(d)
	go func() {
		defer t.Stop()
		select {
		case <-t.C():
			fired.Store(true)
			cancel()
		case <-taskCtx.Done():
		}
	}()
	return fired.Load
}

// runCleanup performs the resource-release action after the task stops. It
// deliberately detaches from the request context (which is usually already
// canceled) but bounds the work with its own deadline driven by the engine
// clock, so a stuck cleanup cannot leak the request either.
func (e *Engine) runCleanup(root context.Context, out *TaskOutcome, spec *CleanupSpec) {
	cleanCtx, cancel := context.WithCancel(context.WithoutCancel(root))
	defer cancel()

	d := spec.Timeout
	if d <= 0 {
		d = e.cleanD
	}
	deadline := e.clk.Timer(d)
	go func() {
		defer deadline.Stop()
		select {
		case <-deadline.C():
			cancel()
		case <-cleanCtx.Done():
		}
	}()

	start := e.clk.Now()
	res := e.cl.Do(cleanCtx, client.Call{
		TaskID: "cleanup:" + out.ID,
		Method: "POST",
		URL:    spec.URL,
	})
	out.CleanupMS = e.clk.Now().Sub(start).Milliseconds()
	out.CleanupRan = res.OK()
	if !out.CleanupRan {
		if res.Error != "" {
			out.CleanupError = res.Error
		} else {
			out.CleanupError = fmt.Sprintf("cleanup returned non-2xx status %d", res.StatusCode)
		}
	}
}

func classifyCancel(timedOut, rootCanceled bool) string {
	switch {
	case timedOut:
		return CanceledByTimeout
	case rootCanceled:
		return CanceledByClient
	default:
		return CanceledByFatalSibling
	}
}

func resultIsCanceled(r client.Result) bool {
	return r.Error != "" &&
		(strings.Contains(r.Error, "context canceled") ||
			strings.Contains(r.Error, "deadline exceeded"))
}

func orDuration(a, b time.Duration) time.Duration {
	if a > 0 {
		return a
	}
	return b
}
