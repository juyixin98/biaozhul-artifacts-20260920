package budget

import (
	"fmt"
	"sync"

	"tokenbudget/internal/clock"
	"tokenbudget/internal/event"
)

// Decision is the outcome of a non-blocking attempt.
type Decision struct {
	Allowed bool
	Reason  string // "insufficient_tokens" when denied
	WaitNS  int64  // max wait both layers require (0 when allowed)
	Global  Snapshot
	Tenant  Snapshot
}

// Decision reasons.
const (
	ReasonExceedsCap   = "tokens_exceed_capacity"
	ReasonInsufficient = "insufficient_tokens"
)

// Limiter enforces a global bucket and per-tenant buckets. Every attempt is
// atomic across both layers: either both deduct (in one lock-ordered critical
// section) or neither layer is mutated.
//
// Lock order everywhere: mapMu -> global.mu -> tenant.mu. Events are emitted
// only AFTER every lock is released, so sink behavior can never affect
// limiter liveness or lock order.
type Limiter struct {
	clk clock.Clock
	bus event.Bus

	mu      sync.Mutex // guards tenants map + defaultTenant
	tenants map[string]*bucket
	dflt    Config
	global  *bucket
}

// emit is one event to dispatch after unlocking.
type emit struct {
	kind event.Kind
	d    event.Detail
}

// NewLimiter builds a two-layer limiter.
func NewLimiter(clk clock.Clock, bus event.Bus, globalCfg, defaultTenantCfg Config) (*Limiter, error) {
	if err := globalCfg.Validate(); err != nil {
		return nil, fmt.Errorf("global: %w", err)
	}
	if err := defaultTenantCfg.Validate(); err != nil {
		return nil, fmt.Errorf("tenant default: %w", err)
	}
	g, err := newBucket("global", globalCfg, clk.Now())
	if err != nil {
		return nil, err
	}
	return &Limiter{
		clk:     clk,
		bus:     bus,
		tenants: make(map[string]*bucket),
		dflt:    defaultTenantCfg,
		global:  g,
	}, nil
}

// acquire locks mapMu, global.mu and the (possibly freshly created) tenant
// bucket. The returned unlock releases in reverse order; pending events are
// flushed by the caller after unlock.
func (l *Limiter) acquire(tenantID string, now clock.Instant) (t *bucket, unlock func(), evs []emit, err error) {
	l.mu.Lock()
	l.global.mu.Lock()

	t, existed := l.tenants[tenantID]
	if !existed {
		t, err = newBucket("tenant:"+tenantID, l.dflt, now)
		if err != nil {
			l.global.mu.Unlock()
			l.mu.Unlock()
			return nil, nil, nil, err
		}
		l.tenants[tenantID] = t
	}
	t.mu.Lock()

	unlock = func() {
		t.mu.Unlock()
		l.global.mu.Unlock()
		l.mu.Unlock()
	}
	if !existed {
		evs = append(evs, emit{event.KindTenantCreated, event.Detail{
			Scope: event.ScopeTenant, Tenant: tenantID, Name: "tenant:" + tenantID,
			After: summaryOf(t.snapshotLocked(now)),
		}})
	}
	return t, unlock, evs, nil
}

func (l *Limiter) flush(evs []emit) {
	if l.bus == nil {
		return
	}
	for _, e := range evs {
		l.bus.Emit(e.kind, e.d)
	}
}

// TryTake attempts an immediate two-layer deduction. On denial neither layer
// is mutated: settle() is a pure time projection that never consumes.
func (l *Limiter) TryTake(tenant string, tokens int64) Decision {
	now := l.clk.Now()
	if tenant == "" {
		return Decision{Reason: ReasonExceedsCap}
	}
	if tokens <= 0 {
		return Decision{Reason: ReasonExceedsCap}
	}

	t, unlock, newEvs, err := l.acquire(tenant, now)
	if err != nil {
		return Decision{Reason: ReasonExceedsCap}
	}

	var evs []emit
	evs = append(evs, newEvs...)

	// Project both layers to now (read-only), then decide.
	l.global.settleLocked(now)
	t.settleLocked(now)
	capExceed := tokens > l.global.capacity || tokens > t.capacity

	switch {
	case capExceed:
		g := l.global.snapshotLocked(now)
		tn := t.snapshotLocked(now)
		unlock()
		evs = append(evs,
			emit{event.KindDenied, denyDetail(event.ScopeGlobal, tenant, tokens, ReasonExceedsCap, 0, g.Available)},
			emit{event.KindDenied, denyDetail(event.ScopeTenant, tenant, tokens, ReasonExceedsCap, 0, tn.Available)},
		)
		l.flush(evs)
		return Decision{Allowed: false, Reason: ReasonExceedsCap, Global: g, Tenant: tn}
	case l.global.avail < tokens || t.avail < tokens:
		// Compute a best-effort wait hint per layer. A stopped layer that can
		// never be refilled reports an error: contribute no finite hint rather
		// than a negative/zero value that misleads.
		var wait int64 = -1
		if r, e := l.global.readyLocked(tokens, now); e == nil {
			if w := int64(r) - int64(now); w > wait {
				wait = w
			}
		}
		if r, e := t.readyLocked(tokens, now); e == nil {
			if w := int64(r) - int64(now); w > wait {
				wait = w
			}
		}
		if wait < 0 {
			wait = 0 // never affordable (at least one layer is stopped)
		}
		g := l.global.snapshotLocked(now)
		tn := t.snapshotLocked(now)
		unlock()
		evs = append(evs,
			emit{event.KindDenied, denyDetail(event.ScopeGlobal, tenant, tokens, ReasonInsufficient, wait, g.Available)},
			emit{event.KindDenied, denyDetail(event.ScopeTenant, tenant, tokens, ReasonInsufficient, wait, tn.Available)},
		)
		l.flush(evs)
		return Decision{Allowed: false, Reason: ReasonInsufficient, WaitNS: wait, Global: g, Tenant: tn}
	}

	// Both layers can pay RIGHT NOW: deduct both atomically under all locks.
	gAfter := l.global.avail - tokens
	tAfter := t.avail - tokens
	l.global.avail = gAfter
	t.avail = tAfter
	gSnap := l.global.snapshotLocked(now)
	tSnap := t.snapshotLocked(now)
	unlock()
	evs = append(evs,
		emit{event.KindGranted, grantDetail(event.ScopeGlobal, tenant, tokens, gAfter, 0)},
		emit{event.KindGranted, grantDetail(event.ScopeTenant, tenant, tokens, tAfter, 0)},
	)
	l.flush(evs)
	return Decision{Allowed: true, Global: gSnap, Tenant: tSnap}
}

func denyDetail(scope event.Scope, tenant string, tokens int64, reason string, wait, remaining int64) event.Detail {
	return event.Detail{
		Scope: scope, Tenant: tenant, Tokens: tokens, Reason: reason,
		WaitNS: wait, Remaining: remaining,
	}
}

func grantDetail(scope event.Scope, tenant string, tokens, remaining, wait int64) event.Detail {
	return event.Detail{
		Scope: scope, Tenant: tenant, Tokens: tokens, Remaining: remaining, WaitNS: wait,
	}
}

// Reserve atomically commits both layers for service at a future instant.
// Under a single lock acquisition it computes each layer's earliest ready
// instant (accounting for earlier firm reservations via future anchors),
// takes the later of the two, checks the horizon, and then commits BOTH layers
// at that instant. Because readyness and commit happen while all locks are
// held, the decision can never become stale and a partial two-layer commit is
// impossible.
func (l *Limiter) Reserve(tenant string, tokens int64, horizonNS int64) (readyAt clock.Instant, waitNS int64, d Decision, err error) {
	if tenant == "" {
		return 0, 0, Decision{}, fmt.Errorf("budget: empty tenant id")
	}
	if tokens <= 0 {
		return 0, 0, Decision{}, ErrZeroTokens
	}

	now := l.clk.Now()
	t, unlock, newEvs, aerr := l.acquire(tenant, now)
	if aerr != nil {
		return 0, 0, Decision{}, aerr
	}
	evs := append([]emit{}, newEvs...)

	if tokens > l.global.capacity || tokens > t.capacity {
		g := l.global.snapshotLocked(now)
		tn := t.snapshotLocked(now)
		unlock()
		evs = append(evs,
			emit{event.KindDenied, denyDetail(event.ScopeGlobal, tenant, tokens, ReasonExceedsCap, 0, g.Available)},
			emit{event.KindDenied, denyDetail(event.ScopeTenant, tenant, tokens, ReasonExceedsCap, 0, tn.Available)},
		)
		l.flush(evs)
		return 0, 0, Decision{Allowed: false, Reason: ReasonExceedsCap, Global: g, Tenant: tn}, ErrTokensExceedCap
	}

	gReady, gerr := l.global.readyLocked(tokens, now)
	tReady, terr := t.readyLocked(tokens, now)
	if gerr != nil || terr != nil {
		unlock()
		l.flush(evs)
		return 0, 0, Decision{}, ErrWaitTooLong
	}
	ready := gReady
	if tReady > ready {
		ready = tReady
	}
	wait := int64(ready) - int64(now)
	if horizonNS > 0 && wait > horizonNS {
		g := l.global.snapshotLocked(now)
		tn := t.snapshotLocked(now)
		unlock()
		evs = append(evs,
			emit{event.KindDenied, denyDetail(event.ScopeGlobal, tenant, tokens, ReasonInsufficient, wait, g.Available)},
			emit{event.KindDenied, denyDetail(event.ScopeTenant, tenant, tokens, ReasonInsufficient, wait, tn.Available)},
		)
		l.flush(evs)
		return 0, wait, Decision{Allowed: false, Reason: ReasonInsufficient, WaitNS: wait, Global: g, Tenant: tn}, ErrWaitTooLong
	}

	// Commit both layers at the joint ready instant (still all locks held).
	gAfter := l.global.reserveLocked(tokens, ready)
	tAfter := t.reserveLocked(tokens, ready)
	gSnap := l.global.snapshotLocked(ready)
	tSnap := t.snapshotLocked(ready)
	unlock()
	evs = append(evs,
		emit{event.KindReserved, grantDetail(event.ScopeGlobal, tenant, tokens, gAfter, wait)},
		emit{event.KindReserved, grantDetail(event.ScopeTenant, tenant, tokens, tAfter, wait)},
	)
	l.flush(evs)
	return ready, wait, Decision{Allowed: true, Global: gSnap, Tenant: tSnap}, nil
}

// UpdateGlobalConfig atomically replaces the global rate/capacity.
func (l *Limiter) UpdateGlobalConfig(cfg Config) error {
	if err := cfg.Validate(); err != nil {
		return err
	}
	now := l.clk.Now()
	l.global.mu.Lock()
	before := l.global.snapshotLocked(now)
	if err := l.global.applyConfigLocked(cfg, now); err != nil {
		l.global.mu.Unlock()
		return err
	}
	after := l.global.snapshotLocked(now)
	l.global.mu.Unlock()
	if l.bus != nil {
		l.bus.Emit(event.KindConfigChanged, event.Detail{
			Scope: event.ScopeGlobal, Name: "global",
			Before: summaryOf(before), After: summaryOf(after),
		})
	}
	return nil
}

// UpdateTenantConfig replaces one tenant's rate/capacity, creating the tenant
// if it does not exist yet.
func (l *Limiter) UpdateTenantConfig(id string, cfg Config) error {
	if id == "" {
		return fmt.Errorf("budget: empty tenant id")
	}
	if err := cfg.Validate(); err != nil {
		return err
	}
	now := l.clk.Now()

	l.mu.Lock()
	l.global.mu.Lock()
	t, existed := l.tenants[id]
	var evs []emit
	if !existed {
		var err error
		t, err = newBucket("tenant:"+id, cfg, now)
		if err != nil {
			l.global.mu.Unlock()
			l.mu.Unlock()
			return err
		}
		l.tenants[id] = t
		t.mu.Lock()
		evs = append(evs, emit{event.KindTenantCreated, event.Detail{
			Scope: event.ScopeTenant, Tenant: id, Name: "tenant:" + id,
			After: summaryOf(t.snapshotLocked(now)),
		}})
		t.mu.Unlock()
	} else {
		t.mu.Lock()
		before := t.snapshotLocked(now)
		if err := t.applyConfigLocked(cfg, now); err != nil {
			t.mu.Unlock()
			l.global.mu.Unlock()
			l.mu.Unlock()
			return err
		}
		after := t.snapshotLocked(now)
		t.mu.Unlock()
		evs = append(evs, emit{event.KindConfigChanged, event.Detail{
			Scope: event.ScopeTenant, Tenant: id, Name: "tenant:" + id,
			Before: summaryOf(before), After: summaryOf(after),
		}})
	}
	l.global.mu.Unlock()
	l.mu.Unlock()
	l.flush(evs)
	return nil
}

// StateView is a point-in-time snapshot for API responses.
type StateView struct {
	AtNS    int64                 `json:"at_ns"`
	Global  BucketView            `json:"global"`
	Tenants map[string]BucketView `json:"tenants"`
}

// BucketView serializes one Snapshot. When anchor_ns > at_ns the bucket holds
// firm reservations: available is promised stock at anchor_ns, not spendable
// immediately.
type BucketView struct {
	RateNum      int64 `json:"rate_num"`
	RateDenNS    int64 `json:"rate_den_ns"`
	Capacity     int64 `json:"capacity"`
	Available    int64 `json:"available"`
	FracScaledNS int64 `json:"frac_scaled_ns"`
	AnchorNS     int64 `json:"anchor_ns"`
	Committed    bool  `json:"committed"`
}

func viewOf(s Snapshot) BucketView {
	return BucketView{
		RateNum: s.Rate.Num, RateDenNS: s.Rate.Den, Capacity: s.Capacity,
		Available: s.Available, FracScaledNS: s.FracScaled,
		AnchorNS: s.AnchorNS, Committed: s.Committed(),
	}
}

func summaryOf(s Snapshot) *event.Summary {
	return &event.Summary{
		RateNum: s.Rate.Num, RateDen: s.Rate.Den, Capacity: s.Capacity,
		Available: s.Available, FracNS: s.FracScaled,
	}
}

// Snapshot returns the whole limiter state settled at the current instant.
// It takes a short global lock to read the tenant list, then reads each
// bucket independently (each snapshot is individually consistent).
func (l *Limiter) Snapshot() StateView {
	now := l.clk.Now()
	out := StateView{AtNS: int64(now), Tenants: make(map[string]BucketView)}

	l.global.mu.Lock()
	out.Global = viewOf(l.global.snapshotLocked(now))
	l.global.mu.Unlock()

	l.mu.Lock()
	tenants := make(map[string]*bucket, len(l.tenants))
	for id, t := range l.tenants {
		tenants[id] = t
	}
	l.mu.Unlock()

	for id, t := range tenants {
		t.mu.Lock()
		out.Tenants[id] = viewOf(t.snapshotLocked(now))
		t.mu.Unlock()
	}
	return out
}
