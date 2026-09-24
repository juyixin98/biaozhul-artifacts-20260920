// Package stub implements the local stub actions invoked by behavior tree
// action nodes. All "work" is simulated locally; nothing leaves the process.
//
// Stubs are split into synchronous stubs (the tick goroutine executes them
// inline and gets the result immediately) and asynchronous stubs (the result
// is delivered later — a goroutine, a gate resolved through the control API,
// or a wall-clock wait). An asynchronous result only ever mutates the
// action_calls latch row; it never walks or "resumes" the tree. The tree
// advances only on the next explicit tick.
package stub

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"time"
)

// Result is the outcome of a physical stub execution.
type Result struct {
	Status string          // "success" | "failure"
	Output json.RawMessage // optional structured output
	Err    string          // populated when Status == "failure"
}

// SyncFunc executes a synchronous stub inline within the tick. If ctx is
// canceled (tick interruption / parent cancel) it must return promptly with
// ctx.Err(); such an execution is recorded as interrupted and is allowed to
// run again later — it has produced no successful side effect.
type SyncFunc func(ctx context.Context, args json.RawMessage) Result

// AsyncFunc starts an asynchronous physical attempt. It must return
// immediately; the returned channel must deliver exactly one Result when the
// attempt finishes, or be closed (with no value) when ctx fires before
// completion. It MUST NOT touch any tree state itself.
type AsyncFunc func(ctx context.Context, args json.RawMessage) <-chan Result

// Kind classifies a stub.
type Kind int

const (
	KindSync Kind = iota
	KindAsync
)

type entry struct {
	kind Kind
	sync SyncFunc
	asyn AsyncFunc
}

// Registry holds known stubs plus the gate control bookkeeping.
type Registry struct {
	mu       sync.RWMutex
	stubs    map[string]entry
	gates    map[string]chan Result // gate token -> completion channel
	counters map[string]int         // counter name -> durable-independent demo counter
}

// NewRegistry builds a registry populated with the built-in stubs.
func NewRegistry() *Registry {
	r := &Registry{
		stubs: map[string]entry{},
		gates: map[string]chan Result{},
	}
	r.registerBuiltins()
	return r
}

// RegisterSync adds a synchronous stub.
func (r *Registry) RegisterSync(name string, f SyncFunc) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.stubs[name] = entry{kind: KindSync, sync: f}
}

// RegisterAsync adds an asynchronous stub.
func (r *Registry) RegisterAsync(name string, f AsyncFunc) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.stubs[name] = entry{kind: KindAsync, asyn: f}
}

// Lookup returns the stub kind and whether it exists.
func (r *Registry) Lookup(name string) (Kind, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	e, ok := r.stubs[name]
	if !ok {
		return 0, false
	}
	return e.kind, true
}

// RunSync executes a synchronous stub.
func (r *Registry) RunSync(ctx context.Context, name string, args json.RawMessage) Result {
	r.mu.RLock()
	e := r.stubs[name]
	r.mu.RUnlock()
	if e.kind != KindSync {
		return Result{Status: "failure", Err: "unknown sync stub: " + name}
	}
	return e.sync(ctx, args)
}

// RunAsync starts an asynchronous stub.
func (r *Registry) RunAsync(ctx context.Context, name string, args json.RawMessage) (<-chan Result, bool) {
	r.mu.RLock()
	e := r.stubs[name]
	r.mu.RUnlock()
	if e.kind != KindAsync {
		return nil, false
	}
	return e.asyn(ctx, args), true
}

// --- built-in stubs ---------------------------------------------------------

// Standard argument object for stubs that return a fixed outcome.
type fixedArgs struct {
	Outcome string          `json:"outcome"` // success (default) | failure
	Output  json.RawMessage `json:"output"`
	Reason  string          `json:"reason"`
}

func parseFixed(args json.RawMessage) fixedArgs {
	var a fixedArgs
	a.Outcome = "success"
	if len(args) > 0 {
		_ = json.Unmarshal(args, &a)
	}
	if a.Outcome == "" {
		a.Outcome = "success"
	}
	return a
}

// waitArgs controls the "wait" async stub.
type waitArgs struct {
	MS      int    `json:"ms"`
	Outcome string `json:"outcome"` // success (default) | failure
}

// gateArgs binds a gate stub to a control token.
type gateArgs struct {
	Token   string `json:"token"`
	Outcome string `json:"outcome"` // result when resolved: success (default) | failure
}

func (r *Registry) registerBuiltins() {
	// succeed / fail: instant synchronous outcomes.
	r.RegisterSync("succeed", func(_ context.Context, args json.RawMessage) Result {
		a := parseFixed(args)
		return Result{Status: "success", Output: a.Output}
	})
	r.RegisterSync("fail", func(_ context.Context, args json.RawMessage) Result {
		a := parseFixed(args)
		if a.Reason == "" {
			a.Reason = "stub failure"
		}
		return Result{Status: "failure", Err: a.Reason, Output: a.Output}
	})

	// block: synchronous, blocks until canceled or released by a timeout
	// given via args.release_ms (default 0 = block forever until canceled).
	type blockEx struct {
		ReleaseMS int    `json:"release_ms"`
		Outcome   string `json:"outcome"`
	}
	r.RegisterSync("block", func(ctx context.Context, args json.RawMessage) Result {
		var a blockEx
		if len(args) > 0 {
			_ = json.Unmarshal(args, &a)
		}
		var ch <-chan time.Time
		if a.ReleaseMS > 0 {
			t := time.NewTimer(time.Duration(a.ReleaseMS) * time.Millisecond)
			defer t.Stop()
			ch = t.C
		} else {
			ch = nil
		}
		if ch == nil {
			<-ctx.Done()
			return Result{Status: "failure", Err: ctxErr(ctx)}
		}
		select {
		case <-ctx.Done():
			return Result{Status: "failure", Err: ctxErr(ctx)}
		case <-ch:
			outcome := a.Outcome
			if outcome == "" {
				outcome = "success"
			}
			return Result{Status: outcome}
		}
	})

	// wait: asynchronous, resolves after ms (or when canceled).
	r.RegisterAsync("wait", func(ctx context.Context, args json.RawMessage) <-chan Result {
		var a waitArgs
		if len(args) > 0 {
			_ = json.Unmarshal(args, &a)
		}
		out := make(chan Result, 1)
		go func() {
			if a.MS <= 0 {
				a.MS = 1
			}
			t := time.NewTimer(time.Duration(a.MS) * time.Millisecond)
			defer t.Stop()
			select {
			case <-ctx.Done():
				close(out) // fenced by the engine; nothing is delivered
			case <-t.C:
				outcome := a.Outcome
				if outcome == "" {
					outcome = "success"
				}
				out <- Result{Status: outcome}
			}
		}()
		return out
	})

	// gate: asynchronous, resolved out-of-band via POST /internal/gates/:token
	r.RegisterAsync("gate", func(ctx context.Context, args json.RawMessage) <-chan Result {
		var a gateArgs
		if len(args) > 0 {
			_ = json.Unmarshal(args, &a)
		}
		if a.Token == "" {
			ch := make(chan Result, 1)
			ch <- Result{Status: "failure", Err: "gate stub requires args.token"}
			return ch
		}
		ch := r.attachGate(a.Token)
		out := make(chan Result, 1)
		go func() {
			select {
			case <-ctx.Done():
				r.detachGate(a.Token, ch)
				close(out)
			case res, ok := <-ch:
				if !ok {
					close(out)
					return
				}
				if res.Status == "" {
					res.Status = "success"
				}
				if a.Outcome != "" {
					res.Status = a.Outcome
				}
				out <- res
			}
		}()
		return out
	})

	// echo: synchronous, returns its arguments as output (useful to inspect
	// what was actually invoked).
	r.RegisterSync("echo", func(_ context.Context, args json.RawMessage) Result {
		return Result{Status: "success", Output: args}
	})

	// crash: synchronous failure with a fixed reason, for fallback branches.
	r.RegisterSync("crash", func(_ context.Context, args json.RawMessage) Result {
		var a struct {
			Reason string `json:"reason"`
		}
		_ = json.Unmarshal(args, &a)
		if a.Reason == "" {
			a.Reason = "crash"
		}
		return Result{Status: "failure", Err: a.Reason}
	})
}

func ctxErr(ctx context.Context) string {
	if err := ctx.Err(); err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			return "timeout"
		}
		return "canceled"
	}
	return "canceled"
}

// --- gate control ------------------------------------------------------------

// ErrGateBusy is returned when attaching a gate whose token is already held.
var ErrGateBusy = errors.New("gate token already held by another running action")

func (r *Registry) attachGate(token string) chan Result {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.gates[token]; ok {
		// Existing waiter exists; return a closed error channel to the newcomer.
		errCh := make(chan Result, 1)
		errCh <- Result{Status: "failure", Err: ErrGateBusy.Error()}
		return errCh
	}
	ch := make(chan Result, 1)
	r.gates[token] = ch
	return ch
}

func (r *Registry) detachGate(token string, ch chan Result) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if cur, ok := r.gates[token]; ok && cur == ch {
		delete(r.gates, token)
	}
}

// GateState describes one held gate.
type GateState struct {
	Token string `json:"token"`
}

// ResolveGate delivers a result to the gate waiter if present. Returns false
// when no running action holds the token (the result is rejected rather than
// stored: late resolutions must never resurrect anything).
func (r *Registry) ResolveGate(token string, res Result) bool {
	r.mu.Lock()
	ch, ok := r.gates[token]
	if ok {
		delete(r.gates, token)
	}
	r.mu.Unlock()
	if !ok {
		return false
	}
	ch <- res
	return true
}

// ListGates returns currently held gate tokens.
func (r *Registry) ListGates() []GateState {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]GateState, 0, len(r.gates))
	for t := range r.gates {
		out = append(out, GateState{Token: t})
	}
	return out
}

// ensure fmt is referenced even if stubs shrink.
