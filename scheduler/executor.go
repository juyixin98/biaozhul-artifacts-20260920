package scheduler

import (
	"context"
	"fmt"
	"strconv"
	"sync"
	"time"
)

// Registry maps task type names to executors. It is safe for concurrent use
// after registration (registration is typically done at startup).
type Registry struct {
	mu        sync.RWMutex
	executors map[string]Executor
}

// NewRegistry returns an empty registry.
func NewRegistry() *Registry {
	return &Registry{executors: map[string]Executor{}}
}

// Register binds a task type to an executor, replacing any previous binding.
func (r *Registry) Register(taskType string, ex Executor) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.executors[taskType] = ex
}

// Lookup returns the executor for taskType.
func (r *Registry) Lookup(taskType string) (Executor, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	ex, ok := r.executors[taskType]
	return ex, ok
}

// Has reports whether taskType is registered.
func (r *Registry) Has(taskType string) bool {
	r.mu.RLock()
	defer r.mu.RUnlock()
	_, ok := r.executors[taskType]
	return ok
}

// NewDefaultRegistry registers the built-in task types:
//
//   - noop:  succeeds immediately. Params: none.
//   - fail:  always fails. Params: message (optional error text).
//   - flaky: fails the first fail_for attempts, then succeeds.
//     Params: fail_for (int, default 1), message (optional).
//   - sleep: sleeps for duration ("100ms") but returns early on cancellation.
//     Params: duration.
func NewDefaultRegistry() *Registry {
	r := NewRegistry()
	r.Register("noop", ExecutorFunc(func(ctx context.Context, in ExecuteInput) error {
		return nil
	}))
	r.Register("fail", ExecutorFunc(func(ctx context.Context, in ExecuteInput) error {
		if msg := in.Params["message"]; msg != "" {
			return fmt.Errorf("%s", msg)
		}
		return fmt.Errorf("task %q failed intentionally", in.NodeID)
	}))
	r.Register("flaky", ExecutorFunc(func(ctx context.Context, in ExecuteInput) error {
		failFor := 1
		if s := in.Params["fail_for"]; s != "" {
			n, err := strconv.Atoi(s)
			if err != nil || n < 0 {
				return fmt.Errorf("flaky: invalid fail_for %q", s)
			}
			failFor = n
		}
		if in.Attempt <= failFor {
			if msg := in.Params["message"]; msg != "" {
				return fmt.Errorf("%s (attempt %d)", msg, in.Attempt)
			}
			return fmt.Errorf("flaky failure on attempt %d", in.Attempt)
		}
		return nil
	}))
	r.Register("sleep", ExecutorFunc(func(ctx context.Context, in ExecuteInput) error {
		d, err := time.ParseDuration(in.Params["duration"])
		if err != nil {
			return fmt.Errorf("sleep: invalid duration: %w", err)
		}
		t := time.NewTimer(d)
		defer t.Stop()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-t.C:
			return nil
		}
	}))
	return r
}
