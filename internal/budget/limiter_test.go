package budget

import (
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"tokenbudget/internal/clock"
	"tokenbudget/internal/event"
)

func gCfg(num, den, cap int64) Config {
	return Config{Rate: Rate{Num: num, Den: den}, Capacity: cap}
}

// A request that fits tenant stock but not global stock consumes neither.
func TestTwoLayerGlobalBlocksNoPartial(t *testing.T) {
	fc := clock.NewFakeClock(0)
	mem := &event.MemorySink{}
	bus := event.NewBus(fc, mem)
	l, err := NewLimiter(fc, bus,
		gCfg(1, sec, 1),     // global: 1 burst
		gCfg(100, sec, 100)) // tenant: plenty
	if err != nil {
		t.Fatal(err)
	}

	first := l.TryTake("acme", 1)
	if !first.Allowed {
		t.Fatalf("first request should pass: %+v", first)
	}
	before := l.Snapshot()
	if before.Global.Available != 0 || before.Tenants["acme"].Available != 99 {
		t.Fatalf("post-first state wrong: %+v", before)
	}

	second := l.TryTake("acme", 1)
	if second.Allowed {
		t.Fatalf("second request must be denied by global layer")
	}
	// No partial consumption: tenant stock unchanged from after first request.
	after := l.Snapshot()
	if after.Tenants["acme"].Available != 99 {
		t.Fatalf("denied request consumed tenant tokens: %d -> %d",
			before.Tenants["acme"].Available, after.Tenants["acme"].Available)
	}
	if after.Global.Available != 0 {
		t.Fatalf("denied request changed global stock: %d", after.Global.Available)
	}
}

// Tenant-layer shortage likewise consumes nothing from the global layer.
func TestTwoLayerTenantBlocksNoPartial(t *testing.T) {
	fc := clock.NewFakeClock(0)
	l, _ := NewLimiter(fc, nil,
		gCfg(100, sec, 100),
		gCfg(1, sec, 1)) // tenant: 1 burst

	ok := l.TryTake("acme", 1)
	if !ok.Allowed {
		t.Fatalf("first: %+v", ok)
	}
	denied := l.TryTake("acme", 1)
	if denied.Allowed {
		t.Fatal("expected tenant-layer denial")
	}
	st := l.Snapshot()
	if st.Global.Available != 99 {
		t.Fatalf("global stock changed on tenant denial: %d want 99", st.Global.Available)
	}
	if denied.WaitNS != sec {
		t.Fatalf("wait=%d want %d", denied.WaitNS, sec)
	}
}

// Exceeding capacity is rejected without consuming anything.
func TestTwoLayerExceedCapNoConsumption(t *testing.T) {
	fc := clock.NewFakeClock(0)
	mem := &event.MemorySink{}
	bus := event.NewBus(fc, mem)
	l, _ := NewLimiter(fc, bus, gCfg(1, sec, 10), gCfg(1, sec, 3))

	d := l.TryTake("acme", 5)
	if d.Allowed || d.Reason != ReasonExceedsCap {
		t.Fatalf("want cap rejection, got %+v", d)
	}
	st := l.Snapshot()
	if st.Global.Available != 10 || st.Tenants["acme"].Available != 3 {
		t.Fatalf("cap-rejected request changed stock: %+v", st)
	}

	// Exactly 2 deny events (one per layer), zero grants.
	var grants, denies, capDenies int
	for _, e := range mem.Events() {
		switch e.Kind {
		case event.KindGranted:
			grants++
		case event.KindDenied:
			denies++
			if e.Detail.Reason == ReasonExceedsCap {
				capDenies++
			}
		}
	}
	if grants != 0 || denies != 2 || capDenies != 2 {
		t.Fatalf("grants=%d denies=%d capDenies=%d", grants, denies, capDenies)
	}
}

// Concurrent contenders: with N goroutines and stock S, exactly S requests
// pass across both layers, and per-layer balances remain consistent.
func TestTwoLayerConcurrent(t *testing.T) {
	fc := clock.NewFakeClock(0)
	l, _ := NewLimiter(fc, nil, gCfg(0, 0, 7), gCfg(0, 0, 3)) // no refill: fixed stock

	const n = 200
	var wg sync.WaitGroup
	var allowed int64
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			if d := l.TryTake("acme", 1); d.Allowed {
				atomic.AddInt64(&allowed, 1)
			}
		}()
	}
	close(start)
	wg.Wait()

	if allowed != 3 {
		t.Fatalf("allowed=%d want 3 (tenant cap)", allowed)
	}
	st := l.Snapshot()
	if st.Tenants["acme"].Available != 0 {
		t.Fatalf("tenant remaining=%d want 0", st.Tenants["acme"].Available)
	}
	if st.Global.Available != 4 {
		t.Fatalf("global remaining=%d want 4 (7-3)", st.Global.Available)
	}
}

// Many tenants contend on the global layer: total grants never exceed the
// global burst; all denials leave both ledgers consistent.
func TestTwoLayerConcurrentManyTenants(t *testing.T) {
	fc := clock.NewFakeClock(0)
	l, _ := NewLimiter(fc, nil, gCfg(0, 0, 50), gCfg(0, 0, 100))

	const tenants = 20
	const each = 5
	var wg sync.WaitGroup
	var allowed int64
	start := make(chan struct{})
	for i := 0; i < tenants; i++ {
		id := "t" + itoaTest(int64(i))
		for j := 0; j < each; j++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				if d := l.TryTake(id, 1); d.Allowed {
					atomic.AddInt64(&allowed, 1)
				}
			}()
		}
	}
	close(start)
	wg.Wait()

	if allowed != 50 {
		t.Fatalf("total allowed=%d want 50", allowed)
	}
	st := l.Snapshot()
	var tenantConsumed int64
	for _, v := range st.Tenants {
		tenantConsumed += 100 - v.Available
	}
	if tenantConsumed != 50 {
		t.Fatalf("tenant-side consumed=%d want 50", tenantConsumed)
	}
	if st.Global.Available != 0 {
		t.Fatalf("global remaining=%d want 0", st.Global.Available)
	}
}

// When one layer is stopped and empty, the deny wait hint must be 0 (never
// affordable), never negative.
func TestTwoLayerStoppedLayerWaitHintNotNegative(t *testing.T) {
	fc := clock.NewFakeClock(0)
	empty := ptrInt64(0)
	l, _ := NewLimiter(fc, nil,
		Config{Rate: Rate{Num: 0, Den: 0}, Capacity: 3, InitialTokens: empty}, // global stopped+empty
		gCfg(1, sec, 3))
	d := l.TryTake("acme", 1)
	if d.Allowed {
		t.Fatal("expected denial")
	}
	if d.WaitNS != 0 {
		t.Fatalf("wait_ns=%d want 0 (stopped global never refills)", d.WaitNS)
	}
}

// Grant and deny events carry per-layer remaining balances.
func TestTwoLayerEventAudit(t *testing.T) {
	fc := clock.NewFakeClock(0)
	mem := &event.MemorySink{}
	bus := event.NewBus(fc, mem)
	l, _ := NewLimiter(fc, bus, gCfg(1, sec, 2), gCfg(1, sec, 2))

	l.TryTake("acme", 1)
	l.TryTake("acme", 1)
	l.TryTake("acme", 1) // denied by both

	evs := mem.Events()
	var g, dn int
	var lastG, lastT event.Event
	for _, e := range evs {
		switch e.Kind {
		case event.KindGranted:
			g++
			if e.Detail.Scope == event.ScopeGlobal {
				lastG = e
			} else {
				lastT = e
			}
		case event.KindDenied:
			dn++
		}
	}
	if g != 4 || dn != 2 {
		t.Fatalf("grants=%d denials=%d want 4/2", g, dn)
	}
	if lastG.Detail.Remaining != 0 || lastT.Detail.Remaining != 0 {
		t.Fatalf("remaining after 2 grants: g=%d t=%d want 0/0",
			lastG.Detail.Remaining, lastT.Detail.Remaining)
	}

	// Sequences are monotonic and timestamps are the clock's.
	for i := 1; i < len(evs); i++ {
		if evs[i].Seq <= evs[i-1].Seq {
			t.Fatalf("non-monotonic seq at %d", i)
		}
	}
}

// Config updates emitted via the limiter show before/after snapshots.
func TestTwoLayerConfigChangeEvent(t *testing.T) {
	fc := clock.NewFakeClock(0)
	mem := &event.MemorySink{}
	bus := event.NewBus(fc, mem)
	l, _ := NewLimiter(fc, bus, gCfg(1, sec, 10), gCfg(1, sec, 10))

	if err := l.UpdateGlobalConfig(gCfg(5, sec, 8)); err != nil {
		t.Fatal(err)
	}
	var found bool
	for _, e := range mem.Events() {
		if e.Kind == event.KindConfigChanged && e.Detail.Scope == event.ScopeGlobal {
			found = true
			if e.Detail.Before.Capacity != 10 || e.Detail.After.Capacity != 8 {
				t.Fatalf("bad before/after: %+v / %+v", e.Detail.Before, e.Detail.After)
			}
			if e.Detail.After.Available != 8 {
				t.Fatalf("shrink clamp in event: avail=%d want 8", e.Detail.After.Available)
			}
		}
	}
	if !found {
		t.Fatal("no global config_changed event")
	}
}

func itoaTest(v int64) string {
	if v == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for v > 0 {
		i--
		b[i] = byte('0' + v%10)
		v /= 10
	}
	return string(b[i:])
}

// Avoid "imported and not used" if time is only used indirectly.
var _ = time.Second
