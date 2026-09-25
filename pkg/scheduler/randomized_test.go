package scheduler

import (
	"math/rand"
	"testing"
	"time"
)

// syncEnv is a scheduler driven entirely by the test thread: no scheduler
// goroutine, so virtual time and scheduling never race wall-clock speed.
type syncEnv struct {
	s    *Scheduler
	clk  *FakeClock
	sink *MemorySink
}

func newSyncEnv(t *testing.T, cap Resources) *syncEnv {
	t.Helper()
	clk := NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	sink := NewMemorySink(0)
	s, err := New(Config{
		Capacity: cap, Clock: clk, Executor: NewSimExecutor(clk), Sink: sink,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return &syncEnv{s: s, clk: clk, sink: sink}
}

// pump advances virtual time by d, then runs completion+schedule passes until
// no more completions are pending at the reached instant.
func (e *syncEnv) pump(t *testing.T, d time.Duration) {
	t.Helper()
	e.clk.Advance(d)
	for i := 0; i < 100000; i++ {
		if e.s.RunOnePassSync() == 0 {
			return
		}
	}
	t.Fatal("pump did not reach fixed point")
}

func (e *syncEnv) schedule() { e.s.RunOnePassSync() }

func (e *syncEnv) submit(t *testing.T, id, tenant string, cpu, mem int64, d time.Duration) {
	t.Helper()
	if _, err := e.s.Submit(SubmitRequest{
		ID: id, TenantID: tenant, CPU: cpu, Memory: mem, Duration: d,
	}); err != nil {
		t.Fatalf("submit %s: %v", id, err)
	}
}

// TestRandomizedNoOvercommitAndDRF drives many randomly timed submissions
// under a fully synchronous fake clock and continuously asserts:
//   - cluster usage never exceeds capacity in either dimension;
//   - per-tenant FIFO: a running/terminal task never appears after a queued
//     predecessor of the same tenant.
func TestRandomizedNoOvercommitAndDRF(t *testing.T) {
	const iterations = 12
	for seed := int64(0); seed < iterations; seed++ {
		rng := rand.New(rand.NewSource(seed))
		env := newSyncEnv(t, Resources{CPU: 10000, Memory: 10000})
		tenants := []string{"t-alpha", "t-beta", "t-gamma"}
		weights := map[string]int64{"t-alpha": 1, "t-beta": 2, "t-gamma": 3}
		for _, id := range tenants {
			if err := env.s.AddTenant(id, weights[id]); err != nil {
				t.Fatal(err)
			}
		}

		taskN := 0
		submitOne := func() {
			tn := tenants[rng.Intn(len(tenants))]
			taskN++
			id := "task-" + itoa(taskN)
			cpu := int64(1000 * (1 + rng.Intn(5))) // 1000..5000
			mem := int64(1000 * (1 + rng.Intn(5)))
			dur := time.Duration(5+rng.Intn(20)) * time.Millisecond
			env.submit(t, id, tn, cpu, mem, dur)
		}

		for step := 0; step < 300; step++ {
			for i, n := 0, 1+rng.Intn(4); i < n; i++ {
				submitOne()
			}
			env.schedule() // react to arrivals at the current instant
			assertSnapshotWithinCapacity(t, env, seed, step)
			assertFIFOSync(t, env, seed, step)

			env.pump(t, time.Duration(1+rng.Intn(4))*time.Millisecond)
			assertSnapshotWithinCapacity(t, env, seed, step)
			assertFIFOSync(t, env, seed, step)
		}

		// Drain: follow-up tasks started at a deadline need another time step
		// to complete; advance until the cluster is empty.
		var running, queued, completed int
		for i := 0; i < 1000; i++ {
			env.pump(t, time.Second)
			_, _, running, queued, completed = env.s.UsedAndCounts()
			if running == 0 && queued == 0 {
				break
			}
			if i == 999 {
				t.Fatalf("seed=%d did not drain: running=%d queued=%d", seed, running, queued)
			}
		}
		used, _, _, _, _ := env.s.UsedAndCounts()
		if used != (Resources{}) {
			t.Fatalf("seed=%d final used = %v, want zero", seed, used)
		}
		if completed != taskN {
			t.Fatalf("seed=%d completed=%d tasks=%d", seed, completed, taskN)
		}
	}
}

func assertSnapshotWithinCapacity(t *testing.T, e *syncEnv, seed int64, step int) {
	t.Helper()
	used, cap, _, _, _ := e.s.UsedAndCounts()
	if !used.LessEqual(cap) {
		t.Fatalf("seed=%d step=%d overcommit used=%v cap=%v", seed, step, used, cap)
	}
}

func assertFIFOSync(t *testing.T, e *syncEnv, seed int64, step int) {
	t.Helper()
	e.s.mu.Lock()
	defer e.s.mu.Unlock()
	for _, list := range e.s.byTenant {
		seenQueued := false
		for _, task := range list {
			if task.State == StateQueued {
				seenQueued = true
				continue
			}
			if seenQueued {
				t.Fatalf("seed=%d step=%d FIFO violation: %s is %s after a QUEUED predecessor",
					seed, step, task.ID, task.State)
			}
		}
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
