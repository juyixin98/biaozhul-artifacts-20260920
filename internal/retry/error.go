package retry

import (
	"context"
	"errors"
	"fmt"
	"time"
)

// Kind classifies the terminal nature of an operation error.
type Kind int

const (
	KindUnknown Kind = iota
	KindRetryable
	KindNonRetryable
	KindCanceled
	KindBudgetExhausted
	KindDeadlineExceeded
	KindLocalExhausted
)

// Retryable reports whether the retry loop may schedule another attempt.
func (k Kind) Retryable() bool { return k == KindRetryable }

func (k Kind) String() string {
	switch k {
	case KindRetryable:
		return "retryable"
	case KindNonRetryable:
		return "non-retryable"
	case KindCanceled:
		return "canceled"
	case KindBudgetExhausted:
		return "budget-exhausted"
	case KindDeadlineExceeded:
		return "deadline-exceeded"
	case KindLocalExhausted:
		return "local-exhausted"
	default:
		return "unknown"
	}
}

// ClassifiedError is the explicit, operation-scoped error type used to mark
// failures as safe or unsafe to retry. Only KindRetryable is ever retried.
type ClassifiedError struct {
	Op            string
	Kind          Kind
	Err           error
	RetryAfter    time.Duration
	HasRetryAfter bool
}

func (e *ClassifiedError) Error() string {
	s := fmt.Sprintf("%s: %s", e.Op, e.Kind)
	if e.Err != nil {
		s += ": " + e.Err.Error()
	}
	if e.HasRetryAfter {
		s += fmt.Sprintf(" (retry-after %s)", e.RetryAfter)
	}
	return s
}

func (e *ClassifiedError) Unwrap() error { return e.Err }

// Retryable marks err as safe to retry for the named operation.
func Retryable(op string, err error) error {
	return &ClassifiedError{Op: op, Kind: KindRetryable, Err: err}
}

// RetryableAfter marks err retryable and carries a server-supplied wait hint
// (parsed from a Retry-After header, for example).
func RetryableAfter(op string, err error, after time.Duration) error {
	return &ClassifiedError{Op: op, Kind: KindRetryable, Err: err, RetryAfter: after, HasRetryAfter: true}
}

// NonRetryable marks err as unsafe to retry (the failure is deterministic).
func NonRetryable(op string, err error) error {
	return &ClassifiedError{Op: op, Kind: KindNonRetryable, Err: err}
}

// Exhausted reports that all retry attempts were spent; Cause is the last
// observed error and is not retryable itself.
type Exhausted struct {
	Op       string
	Attempts int
	Cause    error
}

func (e *Exhausted) Error() string {
	return fmt.Sprintf("%s: retry budget exhausted after %d attempt(s): %v", e.Op, e.Attempts, e.Cause)
}

func (e *Exhausted) Unwrap() error { return e.Cause }

// DeadlineExceeded reports that the propagated deadline left no room for
// another attempt.
type DeadlineExceeded struct {
	Op       string
	Attempts int
	Cause    error
}

func (e *DeadlineExceeded) Error() string {
	return fmt.Sprintf("%s: deadline exceeded after %d attempt(s): %v", e.Op, e.Attempts, e.Cause)
}

func (e *DeadlineExceeded) Unwrap() error { return e.Cause }

// LocalExhausted reports that this layer's own retry cap was reached while
// the shared root budget still permits work. The calling layer may start a
// fresh attempt; this verdict must not propagate as a root-level failure.
type LocalExhausted struct {
	Op       string
	Attempts int
	Cause    error
}

func (e *LocalExhausted) Error() string {
	return fmt.Sprintf("%s: local retry cap reached after %d attempt(s): %v", e.Op, e.Attempts, e.Cause)
}

func (e *LocalExhausted) Unwrap() error { return e.Cause }

// Classify unwraps to the operator's classification. Context cancellation and
// deadline expiration are never retryable; unclassified errors default to
// non-retryable (fail safe: unknown errors must not be blindly replayed).
func Classify(err error) Kind {
	if err == nil {
		return KindUnknown
	}
	// The operator's explicit, local decision wins over anything wrapped
	// inside: a layer may legitimately re-mark a downstream local-cap
	// verdict as Retryable for its own loop. Check ClassifiedError first.
	var ce *ClassifiedError
	if errors.As(err, &ce) {
		return ce.Kind
	}
	// Unwrapped terminal budget verdicts are authoritative.
	var ex *Exhausted
	if errors.As(err, &ex) {
		return KindBudgetExhausted
	}
	var de *DeadlineExceeded
	if errors.As(err, &de) {
		return KindDeadlineExceeded
	}
	var le *LocalExhausted
	if errors.As(err, &le) {
		return KindLocalExhausted
	}
	switch {
	case errors.Is(err, context.Canceled):
		return KindCanceled
	case errors.Is(err, context.DeadlineExceeded):
		return KindDeadlineExceeded
	default:
		return KindNonRetryable
	}
}
