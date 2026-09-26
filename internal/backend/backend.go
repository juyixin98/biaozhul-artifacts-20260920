// Package backend is the in-process fake of the external compute
// service. It stands in for the real dependency so nothing here talks
// to production systems, and it exposes fault-injection knobs
// (injected latency, forced failures) driven by the fault client.
package backend

import (
	"context"
	"errors"
	"sync"
	"time"
)

// ErrInjected is returned when the fault injector forces a failure.
var ErrInjected = errors.New("injected backend failure")

// Faults describes the active fault-injection settings.
type Faults struct {
	// Latency is added to every Compute call.
	Latency time.Duration `json:"latency"`
	// FailNext forces the next N Compute calls to fail with ErrInjected.
	FailNext int `json:"fail_next"`
}

// Fake is the in-process fake compute backend.
type Fake struct {
	mu     sync.Mutex
	faults Faults
	// Calls counts completed Compute invocations (test/diagnostic use).
	Calls int
}

// New returns a fake backend with no faults configured.
func New() *Fake { return &Fake{} }

// SetFaults replaces the fault-injection settings.
func (f *Fake) SetFaults(fl Faults) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.faults = fl
}

// GetFaults returns the current fault-injection settings.
func (f *Fake) GetFaults() Faults {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.faults
}

// Compute simulates a CPU-bound computation: it derives a deterministic
// digest from the payload by iterating a mixing function iterations
// times. Injected latency and failures apply first.
func (f *Fake) Compute(ctx context.Context, payload string, iterations int) ([]byte, error) {
	f.mu.Lock()
	latency := f.faults.Latency
	if f.faults.FailNext > 0 {
		f.faults.FailNext--
		f.mu.Unlock()
		return nil, ErrInjected
	}
	f.Calls++
	f.mu.Unlock()

	if latency > 0 {
		t := time.NewTimer(latency)
		defer t.Stop()
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-t.C:
		}
	}

	// Deterministic mixing loop: stands in for real CPU work.
	var acc uint64 = 1469598103934665603
	for i := 0; i < iterations; i++ {
		for j := 0; j < len(payload); j++ {
			acc ^= uint64(payload[j]) + uint64(i)
			acc *= 1099511628211
		}
	}
	out := make([]byte, 8)
	for i := range out {
		out[i] = byte(acc >> (8 * i))
	}
	return out, nil
}
