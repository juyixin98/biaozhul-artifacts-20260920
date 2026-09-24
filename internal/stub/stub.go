// Package stub implements the local in-process action catalog. Actions never
// leave the process: their side effects are sleeps, counters and real
// cryptographic computations. Each action honors ctx cancellation, so a
// canceled dispatch returns promptly with ctx.Err() and never reports a
// terminal result.
package stub

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

// Result is what an action returns when it finishes.
type Result struct {
	Output   any   `json:"output"`
	Executed int64 `json:"executed_count"` // how many times this stub ran in this process
}

// Action executes against parameters. Implementations MUST return
// ctx.Err() (wrapped) when ctx is canceled; the runner converts that into
// invocation cancellation rather than success/failure.
type Action func(ctx context.Context, params map[string]any) (any, error)

// ErrCanceled marks a run that stopped because its context was canceled.
var ErrCanceled = errors.New("stub action canceled")

// IsCanceled reports whether err is a cancellation error.
func IsCanceled(err error) bool {
	return errors.Is(err, ErrCanceled) || errors.Is(err, context.Canceled)
}

// Registry holds the local actions and process-visible execution counters.
type Registry struct {
	mu       sync.RWMutex
	actions  map[string]entry
	counters sync.Map // name -> *int64
}

type entry struct {
	idempotent bool
	fn         Action
}

// NewRegistry builds a registry preloaded with the built-in stubs.
func NewRegistry() *Registry {
	r := &Registry{actions: map[string]entry{}}
	r.Register("stub", true, Generic)
	r.Register("stub.nonidempotent", false, Generic)
	r.Register("hash.sha256", true, HashSHA256)
	return r
}

// Known reports whether name is registered.
func (r *Registry) Known(name string) bool {
	r.mu.RLock()
	defer r.mu.RUnlock()
	_, ok := r.actions[name]
	return ok
}

// Idempotent reports the action's idempotency declaration.
func (r *Registry) Idempotent(name string) bool {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.actions[name].idempotent
}

// Register adds an action (test helper for custom stubs).
func (r *Registry) Register(name string, idempotent bool, fn Action) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.actions[name] = entry{idempotent: idempotent, fn: fn}
}

// Run executes the named action and bumps its process-local counter exactly
// once per real invocation (a canceled start that never ran the body is not
// counted). The counter is what lets tests prove a succeeded non-idempotent
// action was never executed twice even across an engine restart.
func (r *Registry) Run(ctx context.Context, name string, params map[string]any) (any, error) {
	r.mu.RLock()
	e, ok := r.actions[name]
	r.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("unknown action %q", name)
	}
	v, _ := r.counters.LoadOrStore(name, new(int64))
	ctr := v.(*int64)
	out, err := e.fn(ctx, params)
	if err != nil {
		return nil, err
	}
	n := atomic.AddInt64(ctr, 1)
	if m, ok := out.(map[string]any); ok {
		m["executed_count"] = n
		return m, nil
	}
	return Result{Output: out, Executed: n}, nil
}

// Count returns how many times the named stub body completed in this process.
func (r *Registry) Count(name string) int64 {
	if v, ok := r.counters.Load(name); ok {
		return atomic.LoadInt64(v.(*int64))
	}
	return 0
}

// sleepMS extracts delay_ms / delay (milliseconds) from params, defaulting 0.
func sleepMS(params map[string]any) (time.Duration, error) {
	for _, key := range []string{"delay_ms", "delay"} {
		if v, ok := params[key]; ok {
			switch t := v.(type) {
			case float64:
				return time.Duration(t) * time.Millisecond, nil
			case int:
				return time.Duration(t) * time.Millisecond, nil
			case int64:
				return time.Duration(t) * time.Millisecond, nil
			case string:
				n, err := strconv.ParseInt(t, 10, 64)
				if err != nil {
					return 0, fmt.Errorf("%s must be an integer number of milliseconds", key)
				}
				return time.Duration(n) * time.Millisecond, nil
			default:
				return 0, fmt.Errorf("%s must be a number", key)
			}
		}
	}
	return 0, nil
}

// Generic is the scripted workhorse used by both "stub" (idempotent) and
// "stub.nonidempotent". Params:
//
//	delay_ms   wait before finishing (context-aware sleep)
//	result     "success" (default) | "failure"
//	message    echoed back in the output
//	echo       any value echoed back (proves JSONB parameter round-trip)
func Generic(ctx context.Context, params map[string]any) (any, error) {
	d, err := sleepMS(params)
	if err != nil {
		return nil, err
	}
	if d > 0 {
		t := time.NewTimer(d)
		select {
		case <-ctx.Done():
			t.Stop()
			return nil, fmt.Errorf("%w during %s sleep", ErrCanceled, d)
		case <-t.C:
		}
	}
	out := map[string]any{"message": params["message"]}
	if echo, ok := params["echo"]; ok {
		out["echo"] = echo
	}
	res, _ := params["result"].(string)
	if res == "failure" {
		return out, fmt.Errorf("stub failure: %v", params["message"])
	}
	return out, nil
}

// HashSHA256 really computes SHA-256 over params.input and returns its hex
// digest — demonstrates that cryptographic operations are executed for real.
func HashSHA256(ctx context.Context, params map[string]any) (any, error) {
	var input string
	switch v := params["input"].(type) {
	case string:
		input = v
	case nil:
		input = ""
	default:
		return nil, fmt.Errorf("input must be a string")
	}
	d, err := sleepMS(params)
	if err != nil {
		return nil, err
	}
	if d > 0 {
		t := time.NewTimer(d)
		select {
		case <-ctx.Done():
			t.Stop()
			return nil, fmt.Errorf("%w during %s sleep", ErrCanceled, d)
		case <-t.C:
		}
	}
	sum := sha256.Sum256([]byte(input))
	return map[string]any{
		"input":      input,
		"sha256_hex": hex.EncodeToString(sum[:]),
	}, nil
}
