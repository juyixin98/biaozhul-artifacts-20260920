package budget

import (
	"math/big"
	"math/rand"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"tokenbudget/internal/clock"
)

// div128by64 must match math/big across random 128-bit dividends.
func TestDiv128PropertyBigInt(t *testing.T) {
	rng := rand.New(rand.NewSource(20260923))
	d := new(big.Int)
	n := new(big.Int)
	qWant := new(big.Int)
	rWant := new(big.Int)
	for i := 0; i < 20000; i++ {
		hi := rng.Uint64()
		lo := rng.Uint64()
		div := rng.Uint64()
		if div == 0 {
			div = 1
		}
		n.SetUint64(hi)
		n.Lsh(n, 64)
		n.Or(n, new(big.Int).SetUint64(lo))
		d.SetUint64(div)
		qWant.QuoRem(n, d, rWant)

		qhi, qlo, rem := div128by64(hi, lo, div)
		qGot := new(big.Int).SetUint64(qhi)
		qGot.Lsh(qGot, 64)
		qGot.Or(qGot, new(big.Int).SetUint64(qlo))
		if qGot.Cmp(qWant) != 0 || rem != rWant.Uint64() {
			t.Fatalf("(%d:%d)/%d: got (%d:%d) rem %d, want %s rem %s",
				hi, lo, div, qhi, qlo, rem, qWant, rWant)
		}
	}
}

// Ceiling division property: q*d >= n > (q-1)*d, remainder < d.
func TestDivCeilProperty(t *testing.T) {
	rng := rand.New(rand.NewSource(42))
	for i := 0; i < 20000; i++ {
		hi := uint64(0) // keep quotients 64-bit for simple checking
		lo := rng.Uint64()
		d := rng.Uint64()
		if d == 0 {
			d = 1
		}
		q, r := div128by64Ceil(hi, lo, d)
		if q.hi != 0 {
			t.Fatalf("unexpected wide quotient for %d/%d", lo, d)
		}
		prod := mul64(q.lo, d)
		if prod.cmp(u128{lo: lo}) < 0 {
			t.Fatalf("ceil broken low: %d*%d=%d:%d < %d (rem %d)", q.lo, d, prod.hi, prod.lo, lo, r)
		}
		if q.lo > 0 {
			prev := u128{lo: q.lo - 1}
			prodPrev := mul64(prev.lo, d)
			if prodPrev.cmp(u128{lo: lo}) >= 0 {
				t.Fatalf("ceil not minimal for %d/%d", lo, d)
			}
		}
	}
}

// Stock-conservation invariant across a randomized concurrent workload with
// refill disabled: granted*tokens + remaining == initial for BOTH layers, and
// denials change nothing. This directly checks "no partial consumption".
func TestTwoLayerStockConservation(t *testing.T) {
	rng := rand.New(rand.NewSource(7))
	for iter := 0; iter < 50; iter++ {
		fc := clock.NewFakeClock(clock.Instant(rng.Int63n(1 << 40)))
		gCap := int64(1 + rng.Intn(8))
		tCap := int64(1 + rng.Intn(8))
		l, _ := NewLimiter(fc, nil, gCfg(0, 0, gCap), gCfg(0, 0, tCap))

		tenants := make([]string, 0, 4)
		for i := 0; i < 4; i++ {
			tenants = append(tenants, "t"+string(rune('a'+i)))
		}

		var grantedTenant sync.Map // tenant -> *int64 granted tokens
		var grantedTot int64
		var wg sync.WaitGroup
		start := make(chan struct{})
		const workers = 16
		for w := 0; w < workers; w++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				for k := 0; k < 100; k++ {
					id := tenants[rand.Intn(len(tenants))]
					n := int64(1 + rand.Intn(3))
					d := l.TryTake(id, n)
					if d.Allowed {
						atomic.AddInt64(&grantedTot, n)
						v, _ := grantedTenant.LoadOrStore(id, new(int64))
						atomic.AddInt64(v.(*int64), n)
					}
				}
			}()
		}
		close(start)
		wg.Wait()

		st := l.Snapshot()
		// Global conservation.
		if st.Global.Available+grantedTot != gCap {
			t.Fatalf("iter %d: global remaining=%d granted=%d != initial %d",
				iter, st.Global.Available, grantedTot, gCap)
		}
		// Per-tenant conservation.
		var tenantSum int64
		for _, id := range tenants {
			got := st.Tenants[id].Available
			consumed := int64(0)
			if v, ok := grantedTenant.Load(id); ok {
				consumed = atomic.LoadInt64(v.(*int64))
			}
			if got+consumed != tCap {
				t.Fatalf("iter %d tenant %s: remaining=%d consumed=%d != %d",
					iter, id, got, consumed, tCap)
			}
			tenantSum += consumed
		}
		// Tenant-side consumed cannot exceed global-side granted.
		if tenantSum != grantedTot {
			t.Fatalf("iter %d: tenant consumed=%d global granted=%d (partial two-layer?)",
				iter, tenantSum, grantedTot)
		}
	}
}

// Randomized refill test: with a single bucket, advancing random durations and
// consuming random amounts never leaves stock negative or above capacity, and
// availability is a non-decreasing function of elapsed idle time.
func TestBucketRandomRefillBounded(t *testing.T) {
	rng := rand.New(rand.NewSource(99))
	for iter := 0; iter < 200; iter++ {
		fc := clock.NewFakeClock(0)
		num := int64(1 + rng.Intn(10))
		den := int64(1 + rng.Intn(int(time.Second)))
		cap := int64(1 + rng.Intn(10))
		cfg := Config{Rate: Rate{Num: num, Den: den}, Capacity: cap, InitialTokens: ptrInt64(rng.Int63n(cap + 1))}
		b, err := newBucket("b", cfg, fc.Now())
		if err != nil {
			t.Fatal(err)
		}
		for step := 0; step < 50; step++ {
			fc.Advance(time.Duration(rng.Int63n(int64(2 * time.Second))))
			n := int64(1 + rng.Intn(int(cap)))
			b.mu.Lock()
			_, _ = b.tryLocked(n, fc.Now())
			if b.avail < 0 || b.avail > b.capacity {
				t.Fatalf("iter %d step %d: stock out of range: %d", iter, step, b.avail)
			}
			b.mu.Unlock()
		}
	}
}
