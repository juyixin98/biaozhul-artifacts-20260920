package retry

import (
	"context"
	"errors"
	"fmt"
	"math"
	"math/rand"
	"strconv"
	"time"
)

// Operation is one attempt. permit.Attempt is the 1-based global attempt
// number within the whole call tree (shared budget).
type Operation func(ctx context.Context, permit Permit) error

// Config configures exponential backoff.
type Config struct {
	// BaseDelay is the wait before the second attempt.
	BaseDelay time.Duration
	// MaxDelay caps the computed exponential delay (server Retry-After
	// hints are honored in full and are not capped).
	MaxDelay time.Duration
	// Multiplier grows the delay per attempt; 2.0 when zero.
	Multiplier float64
	// Jitter in [0,1] randomizes each delay symmetrically by that fraction;
	// 0 disables jitter (fully deterministic).
	Jitter float64
	// Rand returns values in [0,1). Defaults to a process-global rand when
	// nil; tests inject a deterministic source.
	Rand func() float64
}

func (c Config) withDefaults() Config {
	if c.Multiplier <= 0 {
		c.Multiplier = 2
	}
	if c.BaseDelay <= 0 {
		c.BaseDelay = 50 * time.Millisecond
	}
	if c.MaxDelay <= 0 {
		c.MaxDelay = 5 * time.Second
	}
	if c.Rand == nil {
		c.Rand = rand.Float64
	}
	if c.Jitter < 0 {
		c.Jitter = 0
	}
	if c.Jitter > 1 {
		c.Jitter = 1
	}
	return c
}

// Result reports how the retry loop terminated.
type Result struct {
	Attempts int // global (shared-budget) attempt number of the last try
	Err      error
}

// Do executes op under budget until it succeeds, returns a non-retryable
// error, the shared budget runs out, its deadline passes, or ctx is canceled.
// Only errors explicitly marked Retryable (optionally carrying Retry-After)
// trigger another attempt.
func Do(ctx context.Context, b *Budget, cfg Config, op Operation) Result {
	cfg = cfg.withDefaults()

	var lastErr error
	var attempts int

	for {
		permit, err := b.Reserve(ctx)
		if err != nil {
			return Result{Attempts: attempts, Err: terminate(attempts, lastErr, err)}
		}
		attempts = permit.Attempt

		if err := op(ctx, permit); err != nil {
			lastErr = err
			if Classify(err) != KindRetryable {
				return Result{Attempts: attempts, Err: err}
			}
			// If no further attempt can possibly be reserved, stop now
			// instead of burning a backoff interval nobody waits to use.
			if perr := b.Precheck(ctx); perr != nil {
				return Result{Attempts: attempts, Err: terminate(attempts, lastErr, perr)}
			}
			delay := cfg.delayFor(permit.Attempt, err)

			// Waiting longer than the remaining deadline is pointless;
			// the next Reserve classifies it as deadline-exceeded.
			if d, ok := b.TimeLeft(b.clk.Now()); ok && delay >= d {
				delay = d
			}
			if delay > 0 {
				if !b.clk.Sleep(ctx, delay) {
					return Result{Attempts: attempts, Err: interrupted(ctx, attempts, lastErr)}
				}
			}
			continue
		}
		return Result{Attempts: attempts, Err: nil}
	}
}

func terminate(attempts int, lastErr, reserveErr error) error {
	cause := lastErr
	if cause == nil {
		cause = reserveErr
	}
	switch {
	case errors.Is(reserveErr, ErrDeadlineExpired):
		return &DeadlineExceeded{Op: "retry", Attempts: attempts, Cause: cause}
	case errors.Is(reserveErr, ErrLocalExhausted):
		return &LocalExhausted{Op: "retry", Attempts: attempts, Cause: cause}
	case errors.Is(reserveErr, ErrBudgetExhausted):
		return &Exhausted{Op: "retry", Attempts: attempts, Cause: cause}
	default:
		// Context canceled before a slot could be reserved.
		return fmt.Errorf("retry stopped before attempt %s: %w", strconv.Itoa(attempts+1), reserveErr)
	}
}

// interrupted builds the terminal error when waiting for the next attempt is
// aborted by ctx.
func interrupted(ctx context.Context, attempts int, lastErr error) error {
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return &DeadlineExceeded{Op: "retry", Attempts: attempts, Cause: lastErr}
	}
	return fmt.Errorf("retry canceled after %d attempt(s): %w", attempts, context.Canceled)
}

// delayFor computes the wait after the attempt numbered attemptNo. A server
// Retry-After hint acts as a floor: the caller waits at least that long.
func (c Config) delayFor(attemptNo int, err error) time.Duration {
	exp := float64(c.BaseDelay) * math.Pow(c.Multiplier, float64(attemptNo-1))
	if exp > float64(c.MaxDelay) || math.IsInf(exp, 1) {
		exp = float64(c.MaxDelay)
	}
	d := exp
	if c.Jitter > 0 {
		d *= 1 + c.Jitter*(2*c.Rand()-1)
	}
	if d < 0 {
		d = 0
	}
	delay := time.Duration(d)

	var ce *ClassifiedError
	if errors.As(err, &ce) && ce.HasRetryAfter && ce.RetryAfter > delay {
		delay = ce.RetryAfter
	}
	return delay
}
