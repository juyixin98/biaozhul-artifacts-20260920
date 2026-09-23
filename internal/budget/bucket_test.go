package budget

import (
	"testing"
	"time"

	"tokenbudget/internal/clock"
)

const sec = int64(time.Second)

func newTestBucket(t *testing.T, name string, num, den, cap int64, initial *int64) (*bucket, *clock.FakeClock) {
	t.Helper()
	fc := clock.NewFakeClock(0)
	cfg := Config{Rate: Rate{Num: num, Den: den}, Capacity: cap, InitialTokens: initial}
	b, err := newBucket(name, cfg, fc.Now())
	if err != nil {
		t.Fatalf("newBucket: %v", err)
	}
	return b, fc
}

func availAt(t *testing.T, b *bucket, fc *clock.FakeClock, advance time.Duration) int64 {
	t.Helper()
	fc.Advance(advance)
	b.mu.Lock()
	defer b.mu.Unlock()
	b.settleLocked(fc.Now())
	return b.avail
}

// A fresh bucket hands out exactly `capacity` tokens in an instant burst,
// never more.
func TestBucketBurstExact(t *testing.T) {
	full := int64(5)
	b, fc := newTestBucket(t, "b", 1, sec, full, nil)

	b.mu.Lock()
	for i := int64(0); i < full; i++ {
		rem, ok := b.tryLocked(1, fc.Now())
		if !ok {
			t.Fatalf("consume %d unexpectedly denied", i+1)
		}
		if rem != full-i-1 {
			t.Fatalf("after %d consumes avail=%d, want %d", i+1, rem, full-i-1)
		}
	}
	ready, err := b.readyLocked(1, fc.Now())
	w := int64(ready) - int64(fc.Now())
	b.mu.Unlock()
	if err != nil {
		t.Fatalf("readyLocked: %v", err)
	}
	if w != sec {
		t.Fatalf("empty bucket wait=%dns, want %dns", w, sec)
	}
}

// 1 token/second: availability grows by exactly one token per virtual second.
func TestBucketRefillExactPerSecond(t *testing.T) {
	b, fc := newTestBucket(t, "b", 1, sec, 10, ptrInt64(0))

	if got := availAt(t, b, fc, 0); got != 0 {
		t.Fatalf("t=0 avail=%d want 0", got)
	}
	for want := int64(1); want <= 3; want++ {
		fc.Advance(time.Second)
		b.mu.Lock()
		b.settleLocked(fc.Now())
		got := b.avail
		b.mu.Unlock()
		if got != want {
			t.Fatalf("t=%ds avail=%d want %d", want, got, want)
		}
	}

	// 999ms more must not mint a token.
	if got := availAt(t, b, fc, 999*time.Millisecond); got != 3 {
		t.Fatalf("t=3.999s avail=%d want 3 (no early token)", got)
	}
	// 1ms more mints the fourth.
	if got := availAt(t, b, fc, time.Millisecond); got != 4 {
		t.Fatalf("t=4s avail=%d want 4", got)
	}
}

// A "1 token per 3 seconds" bucket accumulates fractional progress without
// floating-point drift: tokens appear at exactly t=3s,6s,... and the carried
// remainder survives many settle calls.
func TestBucketFractionalRateNoFloatDrift(t *testing.T) {
	b, fc := newTestBucket(t, "slow", 1, 3*sec, 100, ptrInt64(0))

	// Settle 3000 times at 1ms steps covering 3s: exactly one token.
	for i := 0; i < 3000; i++ {
		fc.Advance(time.Millisecond)
		b.mu.Lock()
		b.settleLocked(fc.Now())
		b.mu.Unlock()
	}
	b.mu.Lock()
	got := b.avail
	rem := b.rem
	b.mu.Unlock()
	if got != 1 || rem != 0 {
		t.Fatalf("after 3000x1ms (rate 1/3s): avail=%d rem=%d, want 1/0", got, rem)
	}

	// 6000 further 1ms steps: second token at exactly 6s, rem 0.
	for i := 0; i < 6000; i++ {
		fc.Advance(time.Millisecond)
		b.mu.Lock()
		b.settleLocked(fc.Now())
		b.mu.Unlock()
	}
	b.mu.Lock()
	got = b.avail
	rem = b.rem
	b.mu.Unlock()
	if got != 3 || rem != 0 {
		t.Fatalf("at t=9s avail=%d rem=%d, want 3/0", got, rem)
	}
}

// The classic 0.1 token/sec case that drifts in floating point: represented
// here as 1 token / 10s exactly. 100 ticks of 100ms must produce zero tokens;
// one more 100ms tick (10.0s exactly) produces exactly one.
func TestBucketTenthTokenPerSecondExact(t *testing.T) {
	b, fc := newTestBucket(t, "tenth", 1, 10*sec, 50, ptrInt64(0))
	for i := 0; i < 100; i++ {
		fc.Advance(100 * time.Millisecond)
		b.mu.Lock()
		b.settleLocked(fc.Now())
		b.mu.Unlock()
	}
	b.mu.Lock()
	got := b.avail
	b.mu.Unlock()
	if got != 1 {
		t.Fatalf("at exactly 10s avail=%d want 1", got)
	}
}

// Capacity is a hard ceiling: leaving a full bucket idle never exceeds cap.
func TestBucketClampedAtCapacity(t *testing.T) {
	b, fc := newTestBucket(t, "b", 10, sec, 3, ptrInt64(3))
	fc.Advance(time.Hour)
	b.mu.Lock()
	b.settleLocked(fc.Now())
	got := b.avail
	rem := b.rem
	b.mu.Unlock()
	if got != 3 || rem != 0 {
		t.Fatalf("full bucket after 1h: avail=%d rem=%d want 3/0", got, rem)
	}
}

// After clamping at the cap and consuming one, refill starts from "now"
// (no phantom hoard of past tokens).
func TestBucketNoHoardAfterClamp(t *testing.T) {
	b, fc := newTestBucket(t, "b", 1, sec, 2, ptrInt64(2))
	fc.Advance(10 * time.Second)
	b.mu.Lock()
	b.settleLocked(fc.Now())
	if _, ok := b.tryLocked(2, fc.Now()); !ok {
		t.Fatal("could not consume 2 full tokens")
	}
	b.mu.Unlock()

	// 1s later exactly one token, not a burst from the idle decade.
	if got := availAt(t, b, fc, time.Second); got != 1 {
		t.Fatalf("avail=%d want 1", got)
	}
}

// A stopped bucket (rate 0) spends existing stock but never refills.
func TestBucketStopped(t *testing.T) {
	b, fc := newTestBucket(t, "b", 0, 0, 5, ptrInt64(2))
	fc.Advance(time.Hour)
	b.mu.Lock()
	b.settleLocked(fc.Now())
	// Existing 2 tokens are still spendable.
	if b.avail != 2 {
		t.Fatalf("stopped bucket avail before consume=%d want 2", b.avail)
	}
	if _, ok := b.tryLocked(2, fc.Now()); !ok {
		t.Fatal("existing 2 tokens should be spendable")
	}
	// A 3rd token can never become available.
	if _, err := b.readyLocked(1, fc.Now()); err != ErrWaitTooLong {
		t.Fatalf("stopped readyLocked err=%v, want ErrWaitTooLong", err)
	}
	b.mu.Unlock()
}

// readyLocked reports exact readiness instants.
func TestBucketWaitExact(t *testing.T) {
	b, fc := newTestBucket(t, "b", 2, sec, 10, ptrInt64(0)) // 2 tok/s
	b.mu.Lock()
	ready, err := b.readyLocked(3, fc.Now()) // need ceil(3/2)s = 1500ms
	w := int64(ready) - int64(fc.Now())
	b.mu.Unlock()
	if err != nil || w != 1500*int64(time.Millisecond) {
		t.Fatalf("wait 3 at 2/s = %dns err=%v, want 1.5e9ns", w, err)
	}
}

func ptrInt64(v int64) *int64 { return &v }
