package engine

import (
	"context"
	"encoding/json"
	"log"
	"sync"

	"github.com/google/uuid"

	"resumable-bt/internal/store"
	"resumable-bt/internal/stub"
)

// dispatchKey identifies one in-flight in-process worker goroutine.
type dispatchKey struct {
	exec uuid.UUID
	key  string
}

// runner owns the goroutines that actually execute local stubs after the
// store has granted a claim (attempt epoch). Completion is persisted with the
// optimistic attempt barrier; cancellation interrupts the worker context.
type runner struct {
	st   *store.Store
	reg  *stub.Registry
	wake func(exec uuid.UUID) // request an auto tick when results land

	mu      sync.Mutex
	workers map[dispatchKey]context.CancelFunc
	wg      sync.WaitGroup
}

func newRunner(st *store.Store, reg *stub.Registry, wake func(uuid.UUID)) *runner {
	return &runner{st: st, reg: reg, wake: wake, workers: map[dispatchKey]context.CancelFunc{}}
}

// dispatch claims the invocation in the database (cross-process gate) and, if
// the claim succeeds, runs the stub in a goroutine. Safe to call repeatedly:
// an already-running local worker is not started twice.
func (r *runner) dispatch(parent context.Context, execID uuid.UUID, row store.InvocationRow) {
	dk := dispatchKey{exec: execID, key: row.StableKey}

	r.mu.Lock()
	if _, busy := r.workers[dk]; busy {
		r.mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(parent)
	r.workers[dk] = cancel
	r.mu.Unlock()

	r.wg.Add(1)
	go func() {
		defer r.wg.Done()
		defer func() {
			r.mu.Lock()
			delete(r.workers, dk)
			r.mu.Unlock()
		}()

		claimed, attempt, err := r.st.ClaimInvocation(ctx, execID, row.StableKey)
		if err != nil {
			// Already terminal/canceled, or shutdown: nothing to run.
			return
		}

		var params map[string]any
		if len(claimed.Params) > 0 {
			_ = json.Unmarshal(claimed.Params, &params)
		}
		out, runErr := r.reg.Run(ctx, claimed.Action, params)

		if runErr != nil {
			if stub.IsCanceled(runErr) || ctx.Err() != nil {
				// Our context was canceled. Do NOT persist failure: the
				// cancellation owner (timeout, parallel winner, execution
				// cancel) has already flipped the row to 'canceled' and any
				// result we could report would be rejected by the attempt
				// barrier anyway.
				return
			}
			accepted, err := r.st.CompleteInvocation(context.Background(), execID,
				claimed.StableKey, attempt, false, nil, runErr.Error())
			if err != nil {
				log.Printf("complete invocation failure %s/%s: %v", execID, claimed.StableKey, err)
				return
			}
			if accepted {
				r.wake(execID)
			}
			return
		}

		raw, _ := json.Marshal(out)
		accepted, err := r.st.CompleteInvocation(context.Background(), execID,
			claimed.StableKey, attempt, true, raw, "")
		if err != nil {
			log.Printf("complete invocation success %s/%s: %v", execID, claimed.StableKey, err)
			return
		}
		if accepted {
			r.wake(execID)
		}
	}()
}

// cancelActive cancels local workers for the given stable keys. Database rows
// are flipped to 'canceled' inside the tick transaction; here we only
// interrupt their contexts so sleeping stubs return immediately.
func (r *runner) cancelActive(execID uuid.UUID, keys []string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, k := range keys {
		if cancel, ok := r.workers[dispatchKey{exec: execID, key: k}]; ok {
			cancel()
		}
	}
}

// cancelAll cancels every in-flight worker (execution cancel / shutdown).
func (r *runner) cancelAll(execID uuid.UUID) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for dk, cancel := range r.workers {
		if dk.exec == execID {
			cancel()
		}
	}
}

// shutdown cancels all workers and waits for them to observe cancellation.
func (r *runner) shutdown() {
	r.mu.Lock()
	for _, cancel := range r.workers {
		cancel()
	}
	r.mu.Unlock()
	r.wg.Wait()
}
