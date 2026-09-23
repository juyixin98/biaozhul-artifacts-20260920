package budget

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"tokenbudget/internal/clock"
	"tokenbudget/internal/rational"
)

const micro = MicroPerToken

func testLimiter(t *testing.T, clk clock.Clock, sink Sink, cfg Config) *Limiter {
	t.Helper()
	l, err := New(cfg, clk, sink)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return l
}

func baseConfig(globalRate, globalBurst, tenantRate, tenantBurst int64) Config {
	return Config{
		Global:  BucketConfig{Rate: rational.PerSecond(globalRate), BurstMicro: globalBurst * micro},
		Default: BucketConfig{Rate: rational.PerSecond(tenantRate), BurstMicro: tenantBurst * micro},
	}
}

func thirdConfig() Config {
	r := rational.Rate{Num: 1, Den: 3}
	return Config{
		Global:  BucketConfig{Rate: r, BurstMicro: 3 * micro},
		Default: BucketConfig{Rate: r, BurstMicro: 3 * micro},
	}
}

// TestVirtualBurst verifies a full bucket admits exactly burst requests at t0
// and the next one is denied, then refill admits precisely the elapsed quota.
func TestVirtualBurst(t *testing.T) {
	v := clock.NewVirtual(time.Unix(0, 0))
	sink := NewMemorySink(0)
	// global 10/s burst 10; tenant default 2/s burst 5. Tenant layer binds.
	l := testLimiter(t, v, sink, baseConfig(10, 10, 2, 5))

	for i := 0; i < 5; i++ {
		d := l.TryAcquire("a", 1*micro)
		if !d.Allowed {
			t.Fatalf("burst request %d denied: %s retry=%v", i+1, d.Reason, d.RetryAfter)
		}
	}
	d := l.TryAcquire("a", 1*micro)
	if d.Allowed || d.Reason != DeniedTenant {
		t.Fatalf("6th request should be denied by tenant, got allowed=%v reason=%s", d.Allowed, d.Reason)
	}

	// 1 second at 2/s -> exactly 2 more.
	v.Advance(1 * time.Second)
	for i := 0; i < 2; i++ {
		d := l.TryAcquire("a", 1*micro)
		if !d.Allowed {
			t.Fatalf("post-refill request %d denied: %s", i+1, d.Reason)
		}
	}
	d = l.TryAcquire("a", 1*micro)
	if d.Allowed {
		t.Fatal("third request after only 2/s refill must be denied")
	}

	// Advance 500ms -> 1 token refilled.
	v.Advance(500 * time.Millisecond)
	d = l.TryAcquire("a", 1*micro)
	if !d.Allowed {
		t.Fatalf("request after 500ms at 2/s denied: %s", d.Reason)
	}
}

// TestGlobalLayerBinds verifies the global layer is independently enforced.
func TestGlobalLayerBinds(t *testing.T) {
	v := clock.NewVirtual(time.Unix(0, 0))
	l := testLimiter(t, v, nil, baseConfig(2, 2, 10, 10))

	for i := 0; i < 2; i++ {
		if d := l.TryAcquire("a", 1*micro); !d.Allowed {
			t.Fatalf("global burst req %d denied: %s", i+1, d.Reason)
		}
	}
	// New tenant with a full own bucket still can't pay the global layer.
	d := l.TryAcquire("b", 1*micro)
	if d.Allowed || d.Reason != DeniedGlobal {
		t.Fatalf("fresh tenant should be denied globally, got allowed=%v reason=%s", d.Allowed, d.Reason)
	}
}

// TestDenialConsumesNothing is the atomicity acceptance check: a request that
// cannot be afforded by one layer must consume zero from BOTH layers, and a
// request affordable by tenant but not global must not debit the tenant.
func TestDenialConsumesNothing(t *testing.T) {
	v := clock.NewVirtual(time.Unix(0, 0))
	// tenant burst 5, global burst 2.
	l := testLimiter(t, v, nil, baseConfig(2, 2, 5, 5))

	consume := func(tenant string, n int) {
		for i := 0; i < n; i++ {
			d := l.TryAcquire(tenant, 1*micro)
			if !d.Allowed {
				t.Fatalf("setup consume denied: %s", d.Reason)
			}
		}
	}
	consume("a", 2) // drains global to 0 and tenant a to 3

	// Tenant a has 3, global has 0: denial must leave a at 3 and global at 0.
	d := l.TryAcquire("a", 1*micro)
	if d.Allowed || d.Reason != DeniedGlobal {
		t.Fatalf("want global denial, got %v %s", d.Allowed, d.Reason)
	}
	g, ts, _ := l.State("a")
	if g.Available != "0" {
		t.Fatalf("global changed after denial: %s", g.Available)
	}
	if ts.Available != "3" {
		t.Fatalf("PARTIAL CONSUMPTION: tenant a=%s want 3", ts.Available)
	}

	// A fresh tenant b has 5 tenant tokens but global is 0: its balance must
	// stay at the full burst even after a failed attempt.
	d = l.TryAcquire("b", 1*micro)
	if d.Allowed || d.Reason != DeniedGlobal {
		t.Fatalf("want global denial for b, got %v %s", d.Allowed, d.Reason)
	}
	_, tsB, _ := l.State("b")
	if tsB.Available != "5" {
		t.Fatalf("PARTIAL CONSUMPTION on fresh tenant: b=%s want 5", tsB.Available)
	}

	// Cost larger than either single balance: nothing moves.
	v.Advance(10 * time.Second) // refill everything toward cap
	d = l.TryAcquire("a", 100*micro)
	if d.Allowed {
		t.Fatal("oversized request must be denied")
	}
	g2, ts2, _ := l.State("a")
	if g2.Available != "2" {
		t.Fatalf("global=%s want 2 (cap)", g2.Available)
	}
	if ts2.Available != "5" {
		t.Fatalf("tenant=%s want 5 (cap)", ts2.Available)
	}
}

// TestCostTwoAtomic verifies a cost-2 request pays 2 from both or none.
func TestCostTwoAtomic(t *testing.T) {
	v := clock.NewVirtual(time.Unix(0, 0))
	l := testLimiter(t, v, nil, baseConfig(3, 3, 3, 3))

	if d := l.TryAcquire("a", 2*micro); !d.Allowed {
		t.Fatalf("cost-2 denied: %s", d.Reason)
	}
	g, ts, _ := l.State("a")
	if g.Available != "1" || ts.Available != "1" {
		t.Fatalf("after cost-2: global=%s tenant=%s want 1/1", g.Available, ts.Available)
	}
	// Only one token in each: cost-2 must fail and leave both at 1.
	if d := l.TryAcquire("a", 2*micro); d.Allowed {
		t.Fatal("cost-2 with one token each must be denied")
	}
	g, ts, _ = l.State("a")
	if g.Available != "1" || ts.Available != "1" {
		t.Fatalf("after failed cost-2: global=%s tenant=%s, want 1/1 (no partial consumption)", g.Available, ts.Available)
	}
}

// TestThirdRateNoDrift is the exact-rational acceptance test: with rate
// 1 token / 3 seconds, refill over 300 seconds is exactly 100 tokens with no
// floating point drift and no stock appearing at capacity.
func TestThirdRateNoDrift(t *testing.T) {
	v := clock.NewVirtual(time.Unix(0, 0))
	l := testLimiter(t, v, nil, thirdConfig())

	// Consume initial burst of 3 (both layers share 3).
	for i := 0; i < 3; i++ {
		if d := l.TryAcquire("a", 1*micro); !d.Allowed {
			t.Fatalf("burst %d: %s", i+1, d.Reason)
		}
	}
	// Advance in 1s steps; after each 3s window exactly one token must be
	// available, never more, never accumulating fractional drift.
	granted := 0
	for step := 1; step <= 300; step++ {
		v.Advance(1 * time.Second)
		d := l.TryAcquire("a", 1*micro)
		if d.Allowed {
			granted++
			// A second token must never be available prematurely.
			if d2 := l.TryAcquire("a", 1*micro); d2.Allowed {
				t.Fatalf("step %d: second token appeared early (drift)", step)
			}
		}
	}
	if granted != 100 {
		t.Fatalf("granted %d over 300s at 1/3 per s, want exactly 100", granted)
	}
	g, ts, _ := l.State("a")
	if g.Available != "0" || ts.Available != "0" {
		t.Fatalf("leftover stock global=%s tenant=%s want 0", g.Available, ts.Available)
	}
}

// TestIdleAtCapacityMintsNothing verifies a saturated bucket does not accrue
// tokens while full ("no stock out of thin air").
func TestIdleAtCapacityMintsNothing(t *testing.T) {
	v := clock.NewVirtual(time.Unix(0, 0))
	l := testLimiter(t, v, nil, baseConfig(5, 5, 5, 5))

	v.Advance(1 * time.Hour)
	g, ts, _ := l.State("a")
	if g.Available != "5" || ts.Available != "5" {
		t.Fatalf("full bucket gained tokens while idle: global=%s tenant=%s", g.Available, ts.Available)
	}
	// Consume all 5, wait one second, get exactly 5? rate 5/s -> 5 but cap 5.
	for i := 0; i < 5; i++ {
		if d := l.TryAcquire("a", 1*micro); !d.Allowed {
			t.Fatalf("drain: %s", d.Reason)
		}
	}
	v.Advance(1 * time.Second)
	g, ts, _ = l.State("a")
	if g.Available != "5" || ts.Available != "5" {
		t.Fatalf("after refill to cap: global=%s tenant=%s want 5", g.Available, ts.Available)
	}
	v.Advance(1 * time.Hour)
	g, ts, _ = l.State("a")
	if g.Available != "5" || ts.Available != "5" {
		t.Fatalf("saturated again grew: global=%s tenant=%s", g.Available, ts.Available)
	}
}

// TestRateSwitchPreservesStock verifies dynamic reconfiguration: switching
// rates never tops up stock; smaller burst clamps; larger burst doesn't fill.
func TestRateSwitchPreservesStock(t *testing.T) {
	v := clock.NewVirtual(time.Unix(0, 0))
	l := testLimiter(t, v, nil, baseConfig(10, 10, 10, 10))

	// Spend tenant a down to 4 (global also at 4).
	for i := 0; i < 6; i++ {
		if d := l.TryAcquire("a", 1*micro); !d.Allowed {
			t.Fatalf("drain: %s", d.Reason)
		}
	}
	// Raise rate and burst: existing balance must remain exactly 4, NOT be
	// refilled to the new burst of 20.
	if err := l.UpdateConfig(baseConfig(100, 20, 100, 20)); err != nil {
		t.Fatalf("update: %v", err)
	}
	g, ts, _ := l.State("a")
	if g.Available != "4" || ts.Available != "4" {
		t.Fatalf("config switch minted stock: global=%s tenant=%s want 4", g.Available, ts.Available)
	}
	// Shrink burst below balance: clamped down to 2, never above.
	if err := l.UpdateConfig(baseConfig(100, 2, 100, 2)); err != nil {
		t.Fatalf("shrink: %v", err)
	}
	g, ts, _ = l.State("a")
	if g.Available != "2" || ts.Available != "2" {
		t.Fatalf("shrink clamp: global=%s tenant=%s want 2", g.Available, ts.Available)
	}
	// Bucket is exactly at the new cap (2 tokens), so refill during a
	// saturated interval adds nothing.
	v.Advance(10 * time.Millisecond)
	g, ts, _ = l.State("a")
	if g.Available != "2" || ts.Available != "2" {
		t.Fatalf("saturated post-switch: global=%s tenant=%s want 2", g.Available, ts.Available)
	}
	// Consume one (down to 1 on both), then 10ms at 100/s yields exactly one
	// more on each layer.
	if d := l.TryAcquire("a", 1*micro); !d.Allowed {
		t.Fatalf("consume after shrink: %s", d.Reason)
	}
	v.Advance(10 * time.Millisecond)
	g, ts, _ = l.State("a")
	if g.Available != "2" || ts.Available != "2" {
		t.Fatalf("post-switch refill: global=%s tenant=%s want 2 (1 left + 1 refilled)", g.Available, ts.Available)
	}
}

// TestTenantOverride verifies per-tenant configs and global/tenant interaction.
func TestTenantOverride(t *testing.T) {
	v := clock.NewVirtual(time.Unix(0, 0))
	cfg := baseConfig(100, 100, 1, 1)
	cfg.Tenants = map[string]BucketConfig{
		"vip": {Rate: rational.PerSecond(10), BurstMicro: 10 * micro},
	}
	l := testLimiter(t, v, nil, cfg)

	// default tenant: 1.
	if d := l.TryAcquire("normal", 1*micro); !d.Allowed {
		t.Fatalf("normal burst: %s", d.Reason)
	}
	if d := l.TryAcquire("normal", 1*micro); d.Allowed {
		t.Fatal("normal second token must deny")
	}
	// vip: 10.
	for i := 0; i < 10; i++ {
		if d := l.TryAcquire("vip", 1*micro); !d.Allowed {
			t.Fatalf("vip burst %d: %s", i+1, d.Reason)
		}
	}
	if d := l.TryAcquire("vip", 1*micro); d.Allowed {
		t.Fatal("vip 11th must deny")
	}
}

// TestSimultaneousRequests is the concurrency acceptance check under virtual
// time: N goroutines released at the same instant, exactly burst succeed and
// the rest fail with zero over-commitment. Run with -race.
func TestSimultaneousRequests(t *testing.T) {
	v := clock.NewVirtual(time.Unix(0, 0))
	l := testLimiter(t, v, nil, baseConfig(50, 50, 50, 50))

	const N = 200
	var wg sync.WaitGroup
	start := make(chan struct{})
	results := make([]bool, N)
	wg.Add(N)
	for i := 0; i < N; i++ {
		i := i
		go func() {
			defer wg.Done()
			<-start
			d := l.TryAcquire(fmt.Sprintf("shared"), 1*micro)
			results[i] = d.Allowed
		}()
	}
	close(start)
	wg.Wait()

	allowed := 0
	for _, a := range results {
		if a {
			allowed++
		}
	}
	if allowed != 50 {
		t.Fatalf("simultaneous: %d allowed, want exactly 50", allowed)
	}
	g, ts, _ := l.State("shared")
	if g.Available != "0" || ts.Available != "0" {
		t.Fatalf("post-burst balances: global=%s tenant=%s want 0", g.Available, ts.Available)
	}
}

// TestConcurrentAcrossTenantsGlobalBound: many tenants, global caps total.
func TestConcurrentAcrossTenantsGlobalBound(t *testing.T) {
	v := clock.NewVirtual(time.Unix(0, 0))
	l := testLimiter(t, v, nil, baseConfig(10, 10, 100, 100))

	const tenants = 100
	var wg sync.WaitGroup
	start := make(chan struct{})
	var mu sync.Mutex
	allowed := 0
	wg.Add(tenants)
	for i := 0; i < tenants; i++ {
		tenant := fmt.Sprintf("t%d", i)
		go func() {
			defer wg.Done()
			<-start
			d := l.TryAcquire(tenant, 1*micro)
			if d.Allowed {
				mu.Lock()
				allowed++
				mu.Unlock()
			}
		}()
	}
	close(start)
	wg.Wait()
	if allowed != 10 {
		t.Fatalf("global admitted %d across tenants, want 10", allowed)
	}
}

// TestBlockingAcquireVirtual verifies Acquire waits under virtual time and is
// granted exactly when refill covers the cost, and times out under a deadline.
func TestBlockingAcquireVirtual(t *testing.T) {
	v := clock.NewVirtual(time.Unix(0, 0))
	l := testLimiter(t, v, nil, baseConfig(2, 2, 2, 2))

	for i := 0; i < 2; i++ {
		if _, err := l.Acquire(context.Background(), "a", 1*micro); err != nil {
			t.Fatalf("acquire: %v", err)
		}
	}

	got := make(chan error, 1)
	go func() {
		_, err := l.Acquire(context.Background(), "a", 1*micro)
		got <- err
	}()
	v.WaitForWaiters(1)
	v.Advance(500 * time.Millisecond) // exactly 1 token at 2/s
	select {
	case err := <-got:
		if err != nil {
			t.Fatalf("blocking acquire returned: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("blocking acquire not woken after virtual refill")
	}

	// Timeout path.
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	_, err := l.Acquire(ctx, "a", 1*micro)
	if err == nil {
		t.Fatal("expected deadline error")
	}
}

// TestEventsRecorded verifies structured events for allow/deny/config.
func TestEventsRecorded(t *testing.T) {
	v := clock.NewVirtual(time.Unix(0, 0))
	sink := NewMemorySink(0)
	l := testLimiter(t, v, sink, baseConfig(1, 1, 1, 1))

	l.TryAcquire("a", 1*micro)
	l.TryAcquire("a", 1*micro) // denied: both layers are empty after the first
	if err := l.UpdateConfig(baseConfig(2, 2, 2, 2)); err != nil {
		t.Fatal(err)
	}
	evs := sink.Events("", 0)
	if len(evs) != 3 {
		t.Fatalf("events=%d want 3", len(evs))
	}
	if evs[0].Result != Allowed || evs[1].Result != DeniedGlobal {
		t.Fatalf("event results wrong: %s %s", evs[0].Result, evs[1].Result)
	}
	if evs[1].TenantSnap.Available != "0" {
		t.Fatalf("denied event tenant snapshot=%s want 0", evs[1].TenantSnap.Available)
	}
	if evs[2].Type != EventConfig || evs[2].Config == nil {
		t.Fatal("config event missing payload")
	}
}

// TestMicrotokenCost verifies fractional cost handling end-to-end.
func TestMicrotokenCost(t *testing.T) {
	v := clock.NewVirtual(time.Unix(0, 0))
	// 1 token burst per layer.
	l := testLimiter(t, v, nil, baseConfig(1, 1, 1, 1))

	quarter, err := rational.ParseDecimalTokens("0.25")
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 4; i++ {
		if d := l.TryAcquire("a", quarter); !d.Allowed {
			t.Fatalf("quarter %d denied: %s", i+1, d.Reason)
		}
	}
	if d := l.TryAcquire("a", quarter); d.Allowed {
		t.Fatal("fifth quarter must deny (exact exhaustion)")
	}
	g, ts, _ := l.State("a")
	if g.Available != "0" || ts.Available != "0" {
		t.Fatalf("fractional balances: global=%s tenant=%s want 0", g.Available, ts.Available)
	}
}

// TestZeroRateNoProgress verifies a zero-rate bucket denies forever cleanly.
func TestZeroRateNoProgress(t *testing.T) {
	v := clock.NewVirtual(time.Unix(0, 0))
	cfg := Config{
		Global:  BucketConfig{Rate: rational.PerSecond(0), BurstMicro: 1 * micro},
		Default: BucketConfig{Rate: rational.PerSecond(10), BurstMicro: 10 * micro},
	}
	l := testLimiter(t, v, nil, cfg)
	if d := l.TryAcquire("a", 1*micro); !d.Allowed {
		t.Fatalf("initial burst: %s", d.Reason)
	}
	d := l.TryAcquire("a", 1*micro)
	if d.Allowed || d.Reason != DeniedGlobal {
		t.Fatalf("zero-rate deny: %v %s", d.Allowed, d.Reason)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	if _, err := l.Acquire(ctx, "a", 1*micro); err == nil {
		t.Fatal("zero-rate Acquire must fail")
	}
}
