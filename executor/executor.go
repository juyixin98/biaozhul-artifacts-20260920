// Package executor defines the replaceable job execution backend.
package executor

import (
	"context"
	"errors"
	"fmt"

	"agingqueue/queue"
)

// Executor runs a single job attempt. Implementations must be safe for
// concurrent use: the scheduler may invoke Execute from multiple goroutines
// when Concurrency > 1.
type Executor interface {
	Execute(ctx context.Context, j *queue.Job) error
}

// FatalError is an alias for queue.FatalError: an execution error wrapped
// with it is never retried.
type FatalError = queue.FatalError

// Fatal wraps err so the scheduler does not retry the job.
func Fatal(err error) error { return queue.Fatal(err) }

// IsFatal reports whether err (or anything in its chain) is a FatalError.
func IsFatal(err error) bool { return queue.IsFatal(err) }

// ErrUnknownType is returned by Registry when no handler is registered
// for a job's Type. It is a fatal error: retrying cannot fix configuration.
var ErrUnknownType = errors.New("unknown job type")

// Handler executes one attempt of a concrete job type.
type Handler func(ctx context.Context, j *queue.Job) error

// Registry maps job types to handlers and implements Executor.
type Registry struct {
	handlers map[string]Handler
}

// NewRegistry creates an empty Registry and registers the built-in demo
// handlers (noop, echo, fail, flaky). Pass no built-ins by constructing
// &Registry{} directly and registering your own handlers.
func NewRegistry() *Registry {
	r := &Registry{handlers: make(map[string]Handler)}
	r.Register("noop", Noop)
	r.Register("echo", Echo)
	r.Register("fail", Fail)
	r.Register("flaky", Flaky)
	return r
}

// Register adds (or replaces) the handler for typ.
func (r *Registry) Register(typ string, h Handler) {
	if r.handlers == nil {
		r.handlers = make(map[string]Handler)
	}
	r.handlers[typ] = h
}

// Execute dispatches the job to its registered handler.
func (r *Registry) Execute(ctx context.Context, j *queue.Job) error {
	h, ok := r.handlers[j.Type]
	if !ok {
		return Fatal(fmt.Errorf("%w: %q", ErrUnknownType, j.Type))
	}
	return h(ctx, j)
}

// Func adapts a function to Executor.
type Func func(ctx context.Context, j *queue.Job) error

func (f Func) Execute(ctx context.Context, j *queue.Job) error { return f(ctx, j) }
