package budget

import (
	"testing"
	"time"
)

// Firm reservations queue on future anchors: starting empty at 1 token/s,
// reservations for 1 token land at exactly 1s, 2s, 3s.
func TestBucketReservationQueue(t *testing.T) {
	b, fc := newTestBucket(t, "b", 1, sec, 100, ptrInt64(0))

	b.mu.Lock()
	r1, err := b.readyLocked(1, fc.Now())
	if err != nil {
		t.Fatal(err)
	}
	b.reserveLocked(1, r1)
	r2, err := b.readyLocked(1, fc.Now())
	if err != nil {
		t.Fatal(err)
	}
	b.reserveLocked(1, r2)
	r3, err := b.readyLocked(1, fc.Now())
	if err != nil {
		t.Fatal(err)
	}
	b.reserveLocked(1, r3)
	b.mu.Unlock()

	if int64(r1) != sec || int64(r2) != 2*sec || int64(r3) != 3*sec {
		t.Fatalf("ready instants=%d,%d,%d want %d,%d,%d", r1, r2, r3, sec, 2*sec, 3*sec)
	}

	// A non-blocking take at t=0 must fail even though 3 tokens are promised.
	if _, ok := b.tryLocked(1, fc.Now()); ok {
		t.Fatal("immediate take must fail when stock is fully promised")
	}

	// At t=1s the first promised token exists but is already committed, so
	// available stays 0 at the anchor; a NEW reservation queues at 4s.
	fc.Advance(time.Second)
	b.mu.Lock()
	if _, ok := b.tryLocked(1, fc.Now()); ok {
		t.Fatal("promised token must not be resellable")
	}
	r4, _ := b.readyLocked(1, fc.Now())
	b.mu.Unlock()
	if int64(r4) != 4*sec {
		t.Fatalf("new reservation after 1s ready=%d want %d", r4, 4*sec)
	}
}

// A burst bucket (full at start) serves the first `cap` reservations at t=0,
// then paces subsequent ones by the refill rate.
func TestBucketReservationBurstThenPace(t *testing.T) {
	b, fc := newTestBucket(t, "b", 2, sec, 3, nil) // 3 burst, +2/s

	b.mu.Lock()
	var ready []int64
	for i := 0; i < 6; i++ {
		r, err := b.readyLocked(1, fc.Now())
		if err != nil {
			t.Fatal(err)
		}
		b.reserveLocked(1, r)
		ready = append(ready, int64(r))
	}
	b.mu.Unlock()

	want := []int64{0, 0, 0, int64(500 * time.Millisecond), sec, int64(1500 * time.Millisecond)}
	for i := range want {
		if ready[i] != want[i] {
			t.Fatalf("reservation %d ready=%d want %d (all=%v)", i, ready[i], want[i], ready)
		}
	}
}

// Reserving more than capacity is rejected without committing.
func TestBucketReservationExceedsCapacity(t *testing.T) {
	b, fc := newTestBucket(t, "b", 100, sec, 4, nil)
	b.mu.Lock()
	_, err := b.readyLocked(5, fc.Now())
	b.mu.Unlock()
	if err != ErrTokensExceedCap {
		t.Fatalf("err=%v want ErrTokensExceedCap", err)
	}
	b.mu.Lock()
	if b.avail != 4 || int64(b.last) != 0 {
		t.Fatalf("rejected reservation committed: avail=%d last=%d", b.avail, b.last)
	}
	b.mu.Unlock()
}
