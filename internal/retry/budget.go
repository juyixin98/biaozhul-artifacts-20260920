package retry

import (
	"context"
	"errors"
	"sync"
	"time"

	"retrybudget/internal/clock"
)

// Sentinel reservation failures.
var (
	// ErrBudgetExhausted: the shared root counter reached its cap. Terminal
	// for every layer — no attempt slot exists anywhere in the call tree.
	ErrBudgetExhausted = errors.New("retry budget exhausted")
	// ErrLocalExhausted: this node's own cap was reached while the root
	// budget still has headroom. Terminal for this retry loop, but the
	// caller layer is allowed to start a fresh attempt.
	ErrLocalExhausted = errors.New("local retry budget exhausted")
	// ErrDeadlineExpired: the propagated absolute deadline passed.
	ErrDeadlineExpired = errors.New("retry budget deadline expired")
)

// Permit represents one reserved attempt slot.
type Permit struct {
	Attempt int
}

// Budget is the shared retry budget: a single attempt counter plus a deadline,
// safe for concurrent use across a whole multi-layer call tree. Child budgets
// share the root counter and may impose an additional local cap.
type Budget struct {
	clk clock.Clock

	// Shared state (same pointers for every node derived from one root).
	mu   *sync.Mutex
	used *int

	deadline time.Time // zero means no deadline

	// Chain from root to this node; every node enforces its own local cap.
	chain     []*Budget
	localMax  int // <=0 means uncapped at this node
	localUsed *int
}

// NewRootBudget creates a root budget: at most maxAttempts attempts in total,
// none permitted at/after deadline (zero deadline disables the time cap).
func NewRootBudget(clk clock.Clock, maxAttempts int, deadline time.Time) *Budget {
	if clk == nil {
		clk = clock.Real{}
	}
	mu := &sync.Mutex{}
	used := 0
	localUsed := 0
	b := &Budget{
		clk:       clk,
		mu:        mu,
		used:      &used,
		deadline:  deadline,
		localMax:  maxAttempts,
		localUsed: &localUsed,
	}
	b.chain = []*Budget{b}
	return b
}

// Restore rebuilds a budget view on the receiving side of a propagated call:
// the root cap/deadline are reconstructed and upstreamUsed slots are marked
// already consumed. Downstream consumption learned later is merged with
// AckUsed. A local policy cap is layered on via Child.
func Restore(clk clock.Clock, maxAttempts, upstreamUsed int, deadline time.Time) *Budget {
	b := NewRootBudget(clk, maxAttempts, deadline)
	b.AckUsed(upstreamUsed)
	return b
}

// Child derives a budget that shares the root counter and deadline but caps
// attempts made through it at localMax (<=0 = no extra cap). It can never
// permit more than the root budget regardless of localMax.
func (b *Budget) Child(localMax int) *Budget {
	c := &Budget{
		clk:       b.clk,
		mu:        b.mu,
		used:      b.used,
		deadline:  b.deadline,
		chain:     append(append([]*Budget(nil), b.chain...), nil),
		localMax:  localMax,
		localUsed: new(int),
	}
	c.chain[len(c.chain)-1] = c
	return c
}

// Root reports whether b is the root budget.
func (b *Budget) Root() bool { return len(b.chain) == 1 }

// Max reports the root attempt cap.
func (b *Budget) Max() int { return b.chain[0].localMax }

// Deadline returns the budget deadline (zero when uncapped).
func (b *Budget) Deadline() time.Time { return b.deadline }

// Used reports attempts consumed so far across the whole call tree, including
// consumption acknowledged from downstream layers.
func (b *Budget) Used() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return *b.used
}

// Remaining reports slots still available under the root cap.
func (b *Budget) Remaining() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	r := b.chain[0].localMax - *b.used
	if r < 0 {
		return 0
	}
	return r
}

// AckUsed merges downstream-reported cumulative consumption into the shared
// counter. The global attempt axis is monotonic, so reconciliation is a max.
func (b *Budget) AckUsed(n int) {
	if n <= 0 {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if n > *b.used {
		*b.used = n
	}
}

// Reserve claims one attempt slot. The reservation is counted immediately and
// never returned: a canceled or failed attempt still consumed real work.
func (b *Budget) Reserve(ctx context.Context) (Permit, error) {
	if err := ctx.Err(); err != nil {
		return Permit{}, err
	}
	b.mu.Lock()
	defer b.mu.Unlock()

	if !b.deadline.IsZero() && !b.clk.Now().Before(b.deadline) {
		return Permit{}, ErrDeadlineExpired
	}
	root := b.chain[0]
	if *b.used >= root.localMax {
		return Permit{}, ErrBudgetExhausted
	}
	// Non-root nodes enforce their own (possibly tighter) local caps.
	for _, n := range b.chain[1:] {
		if n.localMax > 0 && *n.localUsed >= n.localMax {
			return Permit{}, ErrLocalExhausted
		}
	}

	*b.used++
	for _, n := range b.chain {
		*n.localUsed++
	}
	return Permit{Attempt: *b.used}, nil
}

// Precheck reports whether another Reserve could succeed right now, without
// consuming a slot. Retry loops use it to avoid backing off when no attempt
// slot remains.
func (b *Budget) Precheck(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.deadline.IsZero() && !b.clk.Now().Before(b.deadline) {
		return ErrDeadlineExpired
	}
	if *b.used >= b.chain[0].localMax {
		return ErrBudgetExhausted
	}
	for _, n := range b.chain[1:] {
		if n.localMax > 0 && *n.localUsed >= n.localMax {
			return ErrLocalExhausted
		}
	}
	return nil
}

// TimeLeft reports how long until the budget deadline; ok is false when the
// budget has no deadline.
func (b *Budget) TimeLeft(now time.Time) (time.Duration, bool) {
	if b.deadline.IsZero() {
		return 0, false
	}
	d := b.deadline.Sub(now)
	if d < 0 {
		return 0, true
	}
	return d, true
}
