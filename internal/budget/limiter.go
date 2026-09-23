package budget

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"tokenbudget/internal/clock"
	"tokenbudget/internal/rational"
)

// ErrInvalidCost is returned when a requested cost is zero or negative.
var ErrInvalidCost = errors.New("budget: cost must be positive")

// ErrNoProgress is returned by Acquire when the rate is zero and the request
// can never be granted, or when computed wait overflows.
var ErrNoProgress = errors.New("budget: request can never be satisfied at current rate")

// Decision is the result of a non-blocking TryAcquire.
type Decision struct {
	Allowed    bool            `json:"allowed"`
	Reason     DecisionResult  `json:"reason,omitempty"`
	Cost       string          `json:"cost"`
	Tenant     string          `json:"tenant"`
	At         time.Time       `json:"at"`
	Global     *BucketSnapshot `json:"global,omitempty"`
	TenantSnap *BucketSnapshot `json:"tenant_snapshot,omitempty"`
	RetryAfter time.Duration   `json:"retry_after,omitempty"`
	RequestID  string          `json:"request_id,omitempty"`
}

// Limiter is the layered global/tenant token-budget scheduler.
//
// One mutex serializes every decision, which is what makes the two-layer
// deduction atomic: a request pays global and tenant in a single critical
// section, so a failed deduction can never leave either layer partially
// consumed. Under contention requests are ordered by lock acquisition,
// giving a fair first-come/first-served decision at one instant.
type Limiter struct {
	clk clock.Clock

	mu      sync.Mutex
	cfg     Config
	global  *bucket
	tenants map[string]*bucket
	sink    Sink
	reqSeq  uint64
}

// New constructs a limiter. A nil clock uses clock.Real; a nil sink discards
// events. Buckets start full.
func New(cfg Config, clk clock.Clock, sink Sink) (*Limiter, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	if clk == nil {
		clk = clock.Real{}
	}
	if sink == nil {
		sink = DiscardSink{}
	}
	now := clk.Now()
	ts := make(map[string]*bucket, len(cfg.Tenants))
	for t, c := range cfg.Tenants {
		ts[t] = newBucket(c, now)
	}
	return &Limiter{
		clk:     clk,
		cfg:     cfg,
		global:  newBucket(cfg.Global, now),
		tenants: ts,
		sink:    sink,
	}, nil
}

// Config returns a copy of the current configuration.
func (l *Limiter) Config() Config {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.cloneConfigLocked()
}

func (l *Limiter) cloneConfigLocked() Config {
	c := l.cfg
	if l.cfg.Tenants != nil {
		c.Tenants = make(map[string]BucketConfig, len(l.cfg.Tenants))
		for k, v := range l.cfg.Tenants {
			c.Tenants[k] = v
		}
	}
	return c
}

// UpdateConfig changes rates/bursts dynamically.
//
// Rate changes never fabricate stock: existing token balances are preserved
// as-is (a bucket refills at the new rate from that balance forward). A
// smaller burst clamps an over-full balance down; a larger burst simply
// raises the ceiling without topping anyone up.
//
// New tenant overrides create fresh full buckets; removed overrides fall back
// to the default layer (their old bucket state is discarded).
func (l *Limiter) UpdateConfig(cfg Config) error {
	if err := cfg.Validate(); err != nil {
		return err
	}
	l.mu.Lock()
	now := l.clk.Now()

	l.global.refill(now)
	applyBucketConfig(l.global, cfg.Global, now)

	// Every existing bucket survives a config change with its stock carried
	// forward; only a smaller capacity can clamp it down. A tenant keeps its
	// explicit override if one remains, otherwise it follows the default
	// tier. Nothing is dropped and recreated, so reconfiguration never mints
	// a fresh full (or empty) bucket.
	next := make(map[string]*bucket, len(l.tenants)+len(cfg.Tenants))
	for t, b := range l.tenants {
		b.refill(now)
		nc, explicit := cfg.Tenants[t]
		if !explicit {
			nc = cfg.Default
		}
		applyBucketConfig(b, nc, now)
		next[t] = b
	}
	// Explicitly configured tenants not seen yet get new full buckets.
	for t, c := range cfg.Tenants {
		if _, exists := next[t]; !exists {
			next[t] = newBucket(c, now)
		}
	}
	l.tenants = next
	l.cfg = cfg
	view := cfg.View()
	l.mu.Unlock()

	l.sink.Record(Event{Type: EventConfig, At: now, Config: view, Detail: "configuration replaced"})
	return nil
}

// applyBucketConfig mutates a bucket in place after it has been refilled to
// now. It only clamps stock down when the new capacity demands it; it never
// tops up. b.last already equals now from the refill, so it is not touched —
// resetting it would discard the time that just produced stock.
func applyBucketConfig(b *bucket, c BucketConfig, now time.Time) {
	b.rate = c.Rate
	b.capacity = c.BurstMicro
	if b.tokens > c.BurstMicro {
		b.tokens = c.BurstMicro
	}
	// Carry was computed against the old rate's denominator; discard it so a
	// rate switch cannot miscredit fragments from a different fraction. The
	// discarded amount is strictly less than one microtoken.
	b.carryNum = 0
	b.carryDen = 0
}

// tenantBucketLocked returns the bucket for a tenant, lazily creating one from
// the default config (or the explicit override). The caller holds l.mu.
func (l *Limiter) tenantBucketLocked(tenant string) *bucket {
	if b, ok := l.tenants[tenant]; ok {
		return b
	}
	c, ok := l.cfg.Tenants[tenant]
	if !ok {
		c = l.cfg.Default
	}
	b := newBucket(c, l.clk.Now())
	l.tenants[tenant] = b
	return b
}

// TryAcquire makes one non-blocking two-layer decision. costMicro is in
// microtoken units. On failure no tokens are consumed from either layer.
func (l *Limiter) TryAcquire(tenant string, costMicro int64) Decision {
	if costMicro <= 0 {
		return Decision{Allowed: false, Reason: DeniedTenant, Tenant: tenant,
			Cost: "0", At: l.clk.Now()}
	}
	l.mu.Lock()
	now := l.clk.Now()
	d := l.decideLocked(tenant, costMicro, now)
	l.mu.Unlock()

	l.sink.Record(acquireEvent(d))
	return d
}

// decideLocked performs refills, the atomic check-and-deduct, and snapshot
// capture in one critical section.
func (l *Limiter) decideLocked(tenant string, costMicro int64, now time.Time) Decision {
	l.global.refill(now)
	tb := l.tenantBucketLocked(tenant)
	tb.refill(now)

	d := Decision{
		Tenant:     tenant,
		Cost:       formatCost(costMicro),
		At:         now,
		RequestID:  l.nextRequestIDLocked(),
		Global:     l.global.snapshot(now),
		TenantSnap: tb.snapshot(now),
	}

	// Check both layers first; deduct only if BOTH can pay. Order of checks
	// never mutates state, so a failed attempt leaves balances untouched.
	if l.global.tokens < costMicro {
		d.Allowed = false
		d.Reason = DeniedGlobal
		d.RetryAfter = l.global.timeUntil(now, costMicro)
		return d
	}
	if tb.tokens < costMicro {
		d.Allowed = false
		d.Reason = DeniedTenant
		d.RetryAfter = tb.timeUntil(now, costMicro)
		return d
	}

	l.global.tokens -= costMicro
	tb.tokens -= costMicro
	d.Allowed = true
	d.Reason = Allowed
	d.Global = l.global.snapshot(now)
	d.TenantSnap = tb.snapshot(now)
	return d
}

func (l *Limiter) nextRequestIDLocked() string {
	l.reqSeq++
	return fmt.Sprintf("req-%d", l.reqSeq)
}

// Acquire blocks (using the clock's Sleep) until cost tokens can be deducted
// from both layers, ctx expires, or the wait is impossible.
//
// Each iteration is itself an atomic two-layer decision; if it loses, the
// goroutine sleeps only for the smaller of the two layers' retry hints and
// retries. It retries rather than "reserving" because reservations would
// themselves be partial consumption on a path that later times out.
func (l *Limiter) Acquire(ctx context.Context, tenant string, costMicro int64) (Decision, error) {
	if costMicro <= 0 {
		return Decision{}, ErrInvalidCost
	}
	tc, canSleep := l.clk.(clock.TimerClock)
	for {
		d := l.TryAcquire(tenant, costMicro)
		if d.Allowed {
			return d, nil
		}
		if err := ctx.Err(); err != nil {
			return d, err
		}
		if !canSleep || d.RetryAfter >= 1<<62 {
			return d, ErrNoProgress
		}
		// Sleep until the nearer layer should be ready.
		if err := tc.Sleep(ctx, d.RetryAfter); err != nil {
			return l.TryAcquire(tenant, costMicro), err
		}
	}
}

// State returns refilled snapshots without consuming anything.
func (l *Limiter) State(tenant string) (global, tenantSnap *BucketSnapshot, at time.Time) {
	l.mu.Lock()
	now := l.clk.Now()
	l.global.refill(now)
	tb := l.tenantBucketLocked(tenant)
	tb.refill(now)
	g, ts := l.global.snapshot(now), tb.snapshot(now)
	l.mu.Unlock()
	return g, ts, now
}

func acquireEvent(d Decision) Event {
	return Event{
		Type:       EventAcquire,
		At:         d.At,
		RequestID:  d.RequestID,
		Tenant:     d.Tenant,
		Result:     d.Reason,
		Cost:       d.Cost,
		Global:     d.Global,
		TenantSnap: d.TenantSnap,
		RetryAfter: d.RetryAfter,
	}
}

func formatCost(micro int64) string {
	return rational.FormatMicroTokens(micro)
}
