package budget

import (
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"tokenbudget/internal/clock"
	"tokenbudget/internal/event"
)

// A job with insufficient stock is scheduled for the exact wait and executes
// when the fake clock reaches it; tokens are consumed at reserve time.
func TestSchedulerVirtualTimeExecution(t *testing.T) {
	fc := clock.NewFakeClock(0)
	mem := &event.MemorySink{}
	bus := event.NewBus(fc, mem)
	l, _ := NewLimiter(fc, bus,
		gCfg(1, sec, 1), // global: 1 burst, +1/s
		gCfg(1, sec, 1)) // tenant: same
	sched := NewScheduler(l, fc, bus, 0)

	var ran int32
	// First job consumes the single burst token, runs immediately.
	h1, err := sched.Submit("acme", "first", 1, func() error { atomic.AddInt32(&ran, 1); return nil })
	if err != nil {
		t.Fatalf("submit1: %v", err)
	}
	j1 := h1.Result()
	if j1.Status != JobQueued || j1.WaitNS != 0 {
		t.Fatalf("job1 status=%s wait=%d", j1.Status, j1.WaitNS)
	}
	fc.Advance(0)
	if atomic.LoadInt32(&ran) != 1 {
		t.Fatal("job1 should run at t=0")
	}
	<-h1.Done()
	if h1.Result().Status != JobSucceeded {
		t.Fatalf("job1 final=%s", h1.Result().Status)
	}

	// Second job: both layers empty at 1/s -> wait exactly 1s.
	h2, err := sched.Submit("acme", "second", 1, func() error { atomic.AddInt32(&ran, 1); return nil })
	if err != nil {
		t.Fatalf("submit2: %v", err)
	}
	j2 := h2.Result()
	if j2.WaitNS != sec || int64(j2.ReadyAt) != sec {
		t.Fatalf("job2 wait=%d readyAt=%d want %d/%d", j2.WaitNS, j2.ReadyAt, sec, sec)
	}

	fc.Advance(999 * time.Millisecond)
	if atomic.LoadInt32(&ran) != 1 {
		t.Fatal("job2 must not run before ready instant")
	}
	if p, ok := sched.Job(j2.ID); !ok || p.Status != JobQueued {
		t.Fatalf("job2 early status=%s ok=%v", p.Status, ok)
	}
	fc.Advance(time.Millisecond) // exactly t=1s
	<-h2.Done()
	if atomic.LoadInt32(&ran) != 2 || h2.Result().Status != JobSucceeded {
		t.Fatalf("ran=%d job2=%s", ran, h2.Result().Status)
	}

	// Event audit: reserved pairs + queued + executed.
	counts := map[event.Kind]int{}
	for _, e := range mem.Events() {
		counts[e.Kind]++
	}
	if counts[event.KindReserved] != 4 { // 2 layers x 2 jobs
		t.Fatalf("reserved events=%d want 4", counts[event.KindReserved])
	}
	if counts[event.KindJobQueued] != 2 || counts[event.KindJobExecuted] != 2 {
		t.Fatalf("queued=%d executed=%d want 2/2", counts[event.KindJobQueued], counts[event.KindJobExecuted])
	}
}

// Oversized jobs are rejected, recorded, and consume nothing.
func TestSchedulerRejectOversize(t *testing.T) {
	fc := clock.NewFakeClock(0)
	mem := &event.MemorySink{}
	bus := event.NewBus(fc, mem)
	l, _ := NewLimiter(fc, bus, gCfg(1, sec, 2), gCfg(1, sec, 2))
	sched := NewScheduler(l, fc, bus, 0)

	h, err := sched.Submit("acme", "big", 5, nil)
	if err == nil {
		t.Fatal("expected rejection")
	}
	if h.Result().Status != JobRejected {
		t.Fatalf("status=%s want rejected", h.Result().Status)
	}
	st := l.Snapshot()
	if st.Global.Available != 2 || st.Tenants["acme"].Available != 2 {
		t.Fatalf("rejected job consumed tokens: %+v", st)
	}
	var rejected int
	for _, e := range mem.Events() {
		if e.Kind == event.KindJobRejected {
			rejected++
		}
	}
	if rejected != 1 {
		t.Fatalf("job_rejected events=%d want 1", rejected)
	}
}

// Horizon: a wait beyond the bound is rejected without consumption.
func TestSchedulerHorizon(t *testing.T) {
	fc := clock.NewFakeClock(0)
	empty := ptrInt64(0)
	slow := Config{Rate: Rate{Num: 1, Den: 10 * sec}, Capacity: 1, InitialTokens: empty}
	l, _ := NewLimiter(fc, nil, slow, slow) // both buckets start empty
	if err := l.UpdateTenantConfig("acme", slow); err != nil {
		t.Fatal(err)
	}
	sched := NewScheduler(l, fc, nil, int64(time.Second)) // horizon 1s, need 10s

	h, err := sched.Submit("acme", "late", 1, nil)
	if err == nil {
		t.Fatal("expected horizon rejection")
	}
	if h.Result().Status != JobRejected {
		t.Fatalf("status=%s", h.Result().Status)
	}
	st := l.Snapshot()
	if st.Global.Available != 0 || st.Tenants["acme"].Available != 0 {
		t.Fatalf("horizon rejection changed stock: g=%d t=%d",
			st.Global.Available, st.Tenants["acme"].Available)
	}
	if st.Global.Committed || st.Tenants["acme"].Committed {
		t.Fatal("horizon rejection left committed reservations")
	}
}

// Concurrent reservations under a stopped bucket: reservations are firm —
// they consume budget at reserve time, so the total scheduled work never
// exceeds stock even though jobs run later.
func TestSchedulerConcurrentReservationsAtomic(t *testing.T) {
	fc := clock.NewFakeClock(0)
	l, _ := NewLimiter(fc, nil,
		gCfg(0, 0, 100),
		gCfg(0, 0, 10)) // no refill
	sched := NewScheduler(l, fc, nil, 0)

	const n = 300
	var wg sync.WaitGroup
	var accepted int64
	var rejected int64
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			h, err := sched.Submit("acme", "job", 1, nil)
			if err == nil {
				atomic.AddInt64(&accepted, 1)
				fc.Advance(0)
				<-h.Done()
			} else {
				atomic.AddInt64(&rejected, 1)
			}
		}()
	}
	close(start)
	wg.Wait()

	if accepted != 10 || rejected != n-10 {
		t.Fatalf("accepted=%d rejected=%d want 10/%d", accepted, rejected, n-10)
	}
	st := l.Snapshot()
	if st.Global.Available != 90 || st.Tenants["acme"].Available != 0 {
		t.Fatalf("final stock g=%d t=%d want 90/0", st.Global.Available, st.Tenants["acme"].Available)
	}
	succeeded := 0
	for _, j := range sched.Jobs() {
		if j.Status == JobSucceeded {
			succeeded++
		}
	}
	if succeeded != 10 {
		t.Fatalf("succeeded jobs=%d want 10", succeeded)
	}
}

// Global-layer shortage paces several tenants sharing one global rate:
// at virtual t the accepted set is exactly what both layers can afford,
// spread over successive seconds.
func TestSchedulerTwoLayerPacing(t *testing.T) {
	fc := clock.NewFakeClock(0)
	l, _ := NewLimiter(fc, nil,
		gCfg(2, sec, 2), // global: 2 burst, 2/s
		gCfg(10, sec, 10))
	sched := NewScheduler(l, fc, nil, 0)

	var done int32
	// Submit 4 jobs for tenant a: burst 2 now, then 2 more over the next 1s
	// (tenant allows all 4 instantly; global is the bottleneck at 2/s).
	var handles []*Handle
	for i := 0; i < 4; i++ {
		h, err := sched.Submit("a", "j", 1, func() error { atomic.AddInt32(&done, 1); return nil })
		if err != nil {
			t.Fatalf("submit %d: %v", i, err)
		}
		handles = append(handles, h)
	}
	waits := map[int64]int{}
	for _, h := range handles {
		waits[int64(h.Result().ReadyAt)]++
	}
	half := int64(500 * time.Millisecond)
	if waits[0] != 2 || waits[half] != 1 || waits[sec] != 1 {
		t.Fatalf("readyAt distribution=%v want {0:2, 500ms:1, 1s:1}", waits)
	}

	fc.Advance(0)
	if atomic.LoadInt32(&done) != 2 {
		t.Fatalf("at t=0 done=%d want 2", done)
	}
	fc.Advance(time.Duration(half))
	if atomic.LoadInt32(&done) != 3 {
		t.Fatalf("at t=500ms done=%d want 3", done)
	}
	fc.Advance(time.Duration(half))
	for _, h := range handles {
		<-h.Done()
	}
	if atomic.LoadInt32(&done) != 4 {
		t.Fatalf("after 1s done=%d want 4", done)
	}
}
