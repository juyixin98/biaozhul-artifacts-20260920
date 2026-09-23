package executor

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"agingqueue/queue"
)

// Noop always succeeds and does nothing. Useful for scheduling tests.
func Noop(ctx context.Context, _ *queue.Job) error {
	return nil
}

// Echo succeeds after an optional delay, returning the payload in the error
// message-free form. Payload shape: {"delay":"10ms"}.
func Echo(ctx context.Context, j *queue.Job) error {
	var p struct {
		Delay queue.Duration `json:"delay"`
	}
	if len(j.Payload) > 0 {
		if err := json.Unmarshal(j.Payload, &p); err != nil {
			return Fatal(fmt.Errorf("echo: bad payload: %w", err))
		}
	}
	if p.Delay > 0 {
		select {
		case <-time.After(p.Delay.Std()):
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}

// Fail always fails with a retryable error, until Attempts reaches MaxAttempts.
// Use it to observe retries and dead-lettering.
func Fail(ctx context.Context, j *queue.Job) error {
	return fmt.Errorf("fail handler: attempt %d failed", j.Attempts)
}

// Flaky fails the first failTimes attempts of each job, then succeeds.
// Payload: {"failTimes":2} (default 1). The decision uses j.Attempts,
// so a job's retry history exercises it deterministically.
func Flaky(ctx context.Context, j *queue.Job) error {
	n := 1
	var p struct {
		FailTimes int `json:"failTimes"`
	}
	if len(j.Payload) > 0 {
		if err := json.Unmarshal(j.Payload, &p); err != nil {
			return Fatal(fmt.Errorf("flaky: bad payload: %w", err))
		}
		if p.FailTimes > 0 {
			n = p.FailTimes
		}
	}
	if j.Attempts <= n {
		return fmt.Errorf("flaky: transient failure on attempt %d", j.Attempts)
	}
	return nil
}
