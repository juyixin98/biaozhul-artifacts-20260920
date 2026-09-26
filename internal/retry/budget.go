// Package retry implements a retry budget that is propagated across service
// boundaries via HTTP headers, together with deadline and idempotency
// propagation. A single root budget bounds the total number of attempts made
// by an entire multi-layer call tree.
package retry

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

// Propagation headers shared by every layer.
const (
	// HeaderBudgetRemaining carries the remaining retry budget (in attempts)
	// for the whole call tree rooted at the original client.
	HeaderBudgetRemaining = "X-Retry-Budget-Remaining"
	// HeaderBudgetID identifies the shared budget of the call tree; layers
	// resolve it through a BudgetStore so every layer decrements one counter.
	HeaderBudgetID = "X-Retry-Budget-Id"
	// HeaderDeadlineUnixMilli carries the absolute end-to-end deadline.
	HeaderDeadlineUnixMilli = "X-Deadline-Unix-Milli"
	// HeaderIdempotent marks whether the operation is safe to retry.
	HeaderIdempotent = "X-Idempotent"
)

// Budget is a countdown of attempts still allowed for a call tree. It is
// safe for concurrent use.
type Budget struct {
	remaining atomic.Int64
}

func NewBudget(n int64) *Budget {
	b := &Budget{}
	b.remaining.Store(n)
	return b
}

// TryAcquire consumes one attempt. It returns false when the budget is
// exhausted, in which case nothing is consumed.
func (b *Budget) TryAcquire() bool {
	for {
		cur := b.remaining.Load()
		if cur <= 0 {
			return false
		}
		if b.remaining.CompareAndSwap(cur, cur-1) {
			return true
		}
	}
}

func (b *Budget) Remaining() int64 { return b.remaining.Load() }

// NewBudgetID generates a random call-tree identifier.
func NewBudgetID() string {
	var buf [8]byte
	if _, err := rand.Read(buf[:]); err != nil {
		panic(err)
	}
	return hex.EncodeToString(buf[:])
}

// BudgetStore maps call-tree IDs to their shared budget. In this project the
// "external dependency" is in-process, so the store is a local map standing
// in for a shared store (e.g. Redis) in a multi-process deployment.
type BudgetStore struct {
	mu sync.Mutex
	m  map[string]*Budget
}

func NewBudgetStore() *BudgetStore { return &BudgetStore{m: map[string]*Budget{}} }

// Get returns the budget for id, or nil when unknown.
func (s *BudgetStore) Get(id string) *Budget {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.m[id]
}

// Register associates id with b; an existing entry wins and is returned.
func (s *BudgetStore) Register(id string, b *Budget) *Budget {
	s.mu.Lock()
	defer s.mu.Unlock()
	if cur, ok := s.m[id]; ok {
		return cur
	}
	s.m[id] = b
	return b
}

// Release drops the id; called by the budget owner when the tree completes.
func (s *BudgetStore) Release(id string) {
	s.mu.Lock()
	delete(s.m, id)
	s.mu.Unlock()
}

type ctxKey int

const (
	keyBudget ctxKey = iota
	keyIdempotent
)

func WithBudget(ctx context.Context, b *Budget) context.Context {
	return context.WithValue(ctx, keyBudget, b)
}

// BudgetFrom returns the budget attached to ctx, or nil when absent ( callers
// then fall back to a local default).
func BudgetFrom(ctx context.Context) *Budget {
	b, _ := ctx.Value(keyBudget).(*Budget)
	return b
}

// WithIdempotent marks whether the operation in ctx is safe to retry.
func WithIdempotent(ctx context.Context, ok bool) context.Context {
	return context.WithValue(ctx, keyIdempotent, ok)
}

// IdempotentFrom reports whether the operation may be retried. The zero
// value (absent) is false: operations are not retried unless explicitly
// marked safe.
func IdempotentFrom(ctx context.Context) bool {
	v, _ := ctx.Value(keyIdempotent).(bool)
	return v
}

// BudgetFromHeader parses the incoming budget header. Missing or invalid
// values yield nil so the layer applies its own default.
func BudgetFromHeader(h http.Header) *Budget {
	s := h.Get(HeaderBudgetRemaining)
	if s == "" {
		return nil
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil || n < 0 {
		return nil
	}
	return NewBudget(n)
}

func SetBudgetHeader(h http.Header, remaining int64) {
	h.Set(HeaderBudgetRemaining, strconv.FormatInt(remaining, 10))
}

// DeadlineFromHeader parses the absolute deadline header.
func DeadlineFromHeader(h http.Header) (time.Time, bool) {
	s := h.Get(HeaderDeadlineUnixMilli)
	if s == "" {
		return time.Time{}, false
	}
	ms, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return time.Time{}, false
	}
	return time.UnixMilli(ms), true
}

func SetDeadlineHeader(h http.Header, t time.Time) {
	h.Set(HeaderDeadlineUnixMilli, strconv.FormatInt(t.UnixMilli(), 10))
}

func IdempotentFromHeader(h http.Header) bool {
	return h.Get(HeaderIdempotent) == "true"
}

func SetIdempotentHeader(h http.Header, ok bool) {
	if ok {
		h.Set(HeaderIdempotent, "true")
	} else {
		h.Set(HeaderIdempotent, "false")
	}
}
