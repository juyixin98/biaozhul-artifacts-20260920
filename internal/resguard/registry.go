// Package resguard tracks request-scoped exclusive resources (worker slots,
// locks, leases) so tests can prove cleanup always releases what a leaf
// acquired, including on the fatal-fast and client-disconnect paths.
//
// Resources are namespaced per request: two concurrent requests may each
// hold a resource called "lock"; two leaves within the SAME request holding
// the same name is a programming error and panics.
package resguard

import (
	"fmt"
	"sync"
	"sync/atomic"
)

// holdKey identifies one acquisition: the request plus the resource name.
type holdKey struct {
	reqID int64
	name  string
}

// Registry records live resource acquisitions across all requests.
type Registry struct {
	mu     sync.Mutex
	live   map[holdKey]struct{}
	active int

	acquiredTotal atomic.Int64
	releasedTotal atomic.Int64
}

// NewRegistry returns an empty registry.
func NewRegistry() *Registry {
	return &Registry{live: make(map[holdKey]struct{})}
}

// Acquire marks resource held by request reqID. It returns a release
// function that is idempotent: double-release is ignored and reported via
// the returned bool=false rather than double-counting.
//
// Acquiring the same resource twice within one request (before release)
// panics: that is a leaf-definition bug, not a cross-request conflict.
func (r *Registry) Acquire(reqID int64, resource string) func() bool {
	k := holdKey{reqID: reqID, name: resource}
	r.mu.Lock()
	if _, taken := r.live[k]; taken {
		r.mu.Unlock()
		panic(fmt.Sprintf("resguard: request %d already holds resource %q", reqID, resource))
	}
	r.live[k] = struct{}{}
	r.active++
	r.mu.Unlock()
	r.acquiredTotal.Add(1)

	released := false
	return func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		if released {
			return false
		}
		if _, ok := r.live[k]; !ok {
			return false
		}
		delete(r.live, k)
		r.active--
		released = true
		r.releasedTotal.Add(1)
		return true
	}
}

// Active is the number of currently held resources across all requests.
func (r *Registry) Active() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.active
}

// Totals reports cumulative acquire/release counts.
func (r *Registry) Totals() (acquired, released int64) {
	return r.acquiredTotal.Load(), r.releasedTotal.Load()
}
