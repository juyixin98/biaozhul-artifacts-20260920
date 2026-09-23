package budget

import (
	"testing"
	"time"

	"tokenbudget/internal/clock"
)

// Increasing the rate while a fraction is pending must NOT mint tokens for
// elapsed time under the new (faster) rate, nor invent stock. Existing whole
// tokens stay; only future accrual uses the new rate.
func TestConfigRateIncreaseNoRetroactiveTokens(t *testing.T) {
	fc := clock.NewFakeClock(0)
	cfg := Config{Rate: Rate{Num: 1, Den: 2 * sec}, Capacity: 100, InitialTokens: ptrInt64(0)}
	b, _ := newBucket("b", cfg, fc.Now())

	// 1s at 1 token/2s -> 0 whole tokens, fraction pending:
	// rem = num*elapsed mod den = 1e9 (i.e. half a token's worth).
	fc.Advance(time.Second)
	b.mu.Lock()
	b.settleLocked(fc.Now())
	if b.avail != 0 || b.rem != sec {
		t.Fatalf("after 1s @1/2s avail=%d rem=%d, want 0/%d", b.avail, b.rem, sec)
	}

	// Switch to 1 token/s. No stock may appear from the switch itself.
	if err := b.applyConfigLocked(Config{Rate: Rate{Num: 1, Den: sec}, Capacity: 100}, fc.Now()); err != nil {
		t.Fatal(err)
	}
	if b.avail != 0 {
		t.Fatalf("rate switch minted tokens: avail=%d want 0", b.avail)
	}
	// Fraction 1/2 of a token maps to rem_new = 0.5*1s = 5e8ns.
	if b.rem != sec/2 {
		t.Fatalf("rem after switch=%d want %d", b.rem, sec/2)
	}
	b.mu.Unlock()

	// Half a second more accrues another half token => first token at 1.5s,
	// proving the carried fraction survived rather than being reset (reset
	// would need a full second, i.e. token at 2s) and was not double counted.
	fc.Advance(500 * time.Millisecond)
	b.mu.Lock()
	b.settleLocked(fc.Now())
	got := b.avail
	b.mu.Unlock()
	if got != 1 {
		t.Fatalf("avail at 1.5s=%d want 1", got)
	}
}

// Decreasing the rate keeps whole stock and conservatively converts fraction.
func TestConfigRateDecreasePreservesWhole(t *testing.T) {
	fc := clock.NewFakeClock(0)
	cfg := Config{Rate: Rate{Num: 1, Den: sec}, Capacity: 100, InitialTokens: ptrInt64(0)}
	b, _ := newBucket("b", cfg, fc.Now())

	// 2.5s at 1/s -> 2 whole tokens, 0.5 fraction.
	fc.Advance(2500 * time.Millisecond)
	b.mu.Lock()
	b.settleLocked(fc.Now())
	if b.avail != 2 || b.rem != sec/2 {
		t.Fatalf("pre: avail=%d rem=%d want 2/%d", b.avail, b.rem, sec/2)
	}

	// Slow to 1 token/2s: fraction 0.5 maps to rem_new = 0.5*2s = 1s.
	if err := b.applyConfigLocked(Config{Rate: Rate{Num: 1, Den: 2 * sec}, Capacity: 100}, fc.Now()); err != nil {
		t.Fatal(err)
	}
	if b.avail != 2 {
		t.Fatalf("avail after slowdown=%d want 2", b.avail)
	}
	if b.rem != sec {
		t.Fatalf("rem after slowdown=%d want %d", b.rem, sec)
	}
	b.mu.Unlock()

	// 1s at the new rate: 1s/2s = 0.5 token, fraction completes => 3rd token.
	fc.Advance(time.Second)
	b.mu.Lock()
	b.settleLocked(fc.Now())
	got := b.avail
	b.mu.Unlock()
	if got != 3 {
		t.Fatalf("avail at 3.5s=%d want 3", got)
	}
}

// Growing capacity grants nothing at the moment of the switch; shrinking
// clamps existing stock and later refill is capped at the new (smaller) cap.
func TestConfigCapacityShrinkGrow(t *testing.T) {
	fc := clock.NewFakeClock(0)
	b, _ := newBucket("b", Config{Rate: Rate{Num: 1, Den: sec}, Capacity: 10}, fc.Now()) // full=10

	b.mu.Lock()
	if err := b.applyConfigLocked(Config{Rate: Rate{Num: 1, Den: sec}, Capacity: 20}, fc.Now()); err != nil {
		t.Fatal(err)
	}
	if b.avail != 10 {
		t.Fatalf("growing capacity must not grant tokens: avail=%d want 10", b.avail)
	}
	if err := b.applyConfigLocked(Config{Rate: Rate{Num: 1, Den: sec}, Capacity: 4}, fc.Now()); err != nil {
		t.Fatal(err)
	}
	if b.avail != 4 {
		t.Fatalf("after shrink avail=%d want 4", b.avail)
	}
	b.mu.Unlock()

	// After shrink, refill at 1/s must clamp at 4, not reach the old cap.
	fc.Advance(10 * time.Second)
	b.mu.Lock()
	b.settleLocked(fc.Now())
	got := b.avail
	b.mu.Unlock()
	if got != 4 {
		t.Fatalf("refill after shrink=%d want 4", got)
	}
}

// Stopping (rate 0) freezes; resuming later accrues only from resume instant.
func TestConfigStopAndResume(t *testing.T) {
	fc := clock.NewFakeClock(0)
	b, _ := newBucket("b", Config{Rate: Rate{Num: 1, Den: sec}, Capacity: 100, InitialTokens: ptrInt64(0)}, fc.Now())

	fc.Advance(2 * time.Second) // 2 tokens
	b.mu.Lock()
	b.settleLocked(fc.Now())
	if err := b.applyConfigLocked(Config{Rate: Rate{Num: 0, Den: 0}, Capacity: 100}, fc.Now()); err != nil {
		t.Fatal(err)
	}
	b.mu.Unlock()
	fc.Advance(time.Hour) // stopped: nothing accrues
	b.mu.Lock()
	b.settleLocked(fc.Now())
	if b.avail != 2 {
		t.Fatalf("stopped period changed stock: avail=%d want 2", b.avail)
	}
	if err := b.applyConfigLocked(Config{Rate: Rate{Num: 1, Den: sec}, Capacity: 100}, fc.Now()); err != nil {
		t.Fatal(err)
	}
	b.mu.Unlock()
	fc.Advance(time.Second)
	b.mu.Lock()
	b.settleLocked(fc.Now())
	got := b.avail
	b.mu.Unlock()
	if got != 3 {
		t.Fatalf("after resume avail=%d want 3 (no backfill)", got)
	}
}
