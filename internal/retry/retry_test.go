package retry

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"retrybudget/internal/clock"
)

// stepClock advances virtual time by the requested sleep duration instantly
// and records every requested delay. Cancellation is not simulated here.
type stepClock struct {
	t      time.Time
	sleeps []time.Duration
}

func (c *stepClock) Now() time.Time { return c.t }

func (c *stepClock) Sleep(ctx context.Context, d time.Duration) bool {
	if err := ctx.Err(); err != nil {
		return false
	}
	c.sleeps = append(c.sleeps, d)
	c.t = c.t.Add(d)
	return true
}

func newStepClock() *stepClock {
	return &stepClock{t: time.Unix(1_700_000_000, 0)}
}

func TestRootBudgetCapsTotalAttempts(t *testing.T) {
	clk := newStepClock()
	b := NewRootBudget(clk, 3, time.Time{})

	for i := 1; i <= 3; i++ {
		p, err := b.Reserve(context.Background())
		if err != nil {
			t.Fatalf("attempt %d: unexpected error %v", i, err)
		}
		if p.Attempt != i {
			t.Fatalf("attempt number = %d, want %d", p.Attempt, i)
		}
	}
	if _, err := b.Reserve(context.Background()); !errors.Is(err, ErrBudgetExhausted) {
		t.Fatalf("4th reserve error = %v, want ErrBudgetExhausted", err)
	}
	if b.Used() != 3 {
		t.Fatalf("Used = %d, want 3", b.Used())
	}
	if b.Remaining() != 0 {
		t.Fatalf("Remaining = %d, want 0", b.Remaining())
	}
}

func TestChildBudgetsShareRootCounter(t *testing.T) {
	clk := newStepClock()
	b := NewRootBudget(clk, 5, time.Time{})
	l1 := b.Child(5)
	l2 := l1.Child(0)

	// 3 attempts through the deepest child ...
	for i := 0; i < 3; i++ {
		if _, err := l2.Reserve(context.Background()); err != nil {
			t.Fatalf("l2 reserve: %v", err)
		}
	}
	// ... are visible at the root and every intermediate node.
	if b.Used() != 3 || l1.Used() != 3 {
		t.Fatalf("shared used = %d/%d, want 3", b.Used(), l1.Used())
	}

	// A strict local cap on a sibling refuses beyond 1 while the root still
	// permits work; that is local exhaustion, not root exhaustion.
	sibling := b.Child(1)
	if _, err := sibling.Reserve(context.Background()); err != nil {
		t.Fatalf("sibling reserve: %v", err)
	}
	if _, err := sibling.Reserve(context.Background()); !errors.Is(err, ErrLocalExhausted) {
		t.Fatalf("sibling 2nd reserve = %v, want ErrLocalExhausted", err)
	}
	if b.Used() != 4 {
		t.Fatalf("Used = %d, want 4", b.Used())
	}

	// Deep child still consumes against the same root until the root is full.
	if _, err := l2.Reserve(context.Background()); err != nil {
		t.Fatalf("l2 reserve: %v", err)
	}
	if _, err := l2.Reserve(context.Background()); !errors.Is(err, ErrBudgetExhausted) {
		t.Fatalf("root should be exhausted, got %v", err)
	}
}

func TestLocalCapCannotExceedRoot(t *testing.T) {
	clk := newStepClock()
	root := NewRootBudget(clk, 2, time.Time{})
	_ = root.Child(100) // large local cap must not bypass root
	if root.Remaining() != 2 {
		t.Fatalf("Remaining = %d, want 2", root.Remaining())
	}
}

func TestBudgetDeadline(t *testing.T) {
	start := time.Unix(1_700_000_000, 0)
	fk := clock.NewFake(start)
	b := NewRootBudget(fk, 10, start.Add(100*time.Millisecond))

	if d, ok := b.TimeLeft(fk.Now()); !ok || d != 100*time.Millisecond {
		t.Fatalf("TimeLeft = %v/%v, want 100ms", d, ok)
	}
	if _, err := b.Reserve(context.Background()); err != nil {
		t.Fatalf("first reserve: %v", err)
	}
	fk.Advance(100 * time.Millisecond)
	if _, err := b.Reserve(context.Background()); !errors.Is(err, ErrDeadlineExpired) {
		t.Fatalf("after deadline error = %v, want ErrDeadlineExpired", err)
	}
}

func TestReserveChecksContextFirst(t *testing.T) {
	clk := newStepClock()
	b := NewRootBudget(clk, 3, time.Time{})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := b.Reserve(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("error = %v, want context.Canceled", err)
	}
	if b.Used() != 0 {
		t.Fatalf("canceled reservation counted %d attempts", b.Used())
	}
}

func TestBudgetConcurrentReservations(t *testing.T) {
	clk := newStepClock()
	b := NewRootBudget(clk, 50, time.Time{})
	var wg sync.WaitGroup
	ok, fail := make(chan int, 100), make(chan int, 100)
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := b.Reserve(context.Background()); err == nil {
				ok <- 1
			} else {
				fail <- 1
			}
		}()
	}
	wg.Wait()
	if len(ok) != 50 || len(fail) != 50 {
		t.Fatalf("granted=%d refused=%d, want 50/50", len(ok), len(fail))
	}
}

func TestDoSuccessFirstAttempt(t *testing.T) {
	clk := newStepClock()
	b := NewRootBudget(clk, 3, time.Time{})
	calls := 0
	res := Do(context.Background(), b, Config{}, func(ctx context.Context, p Permit) error {
		calls++
		return nil
	})
	if res.Err != nil || calls != 1 || b.Used() != 1 {
		t.Fatalf("res=%v calls=%d used=%d", res.Err, calls, b.Used())
	}
	if len(clk.sleeps) != 0 {
		t.Fatalf("slept %v on immediate success", clk.sleeps)
	}
}

func TestDoRetriesOnlyExplicitlyRetryable(t *testing.T) {
	clk := newStepClock()
	b := NewRootBudget(clk, 5, time.Time{})
	calls := 0
	res := Do(context.Background(), b, Config{BaseDelay: time.Millisecond}, func(ctx context.Context, p Permit) error {
		calls++
		if calls < 3 {
			return Retryable("flaky", errors.New("boom"))
		}
		return nil
	})
	if res.Err != nil || calls != 3 || res.Attempts != 3 {
		t.Fatalf("res=%v calls=%d attempts=%d", res.Err, calls, res.Attempts)
	}
}

func TestDoStopsOnNonRetryable(t *testing.T) {
	clk := newStepClock()
	b := NewRootBudget(clk, 5, time.Time{})
	calls := 0
	want := errors.New("deterministic bad request")
	res := Do(context.Background(), b, Config{BaseDelay: time.Millisecond}, func(ctx context.Context, p Permit) error {
		calls++
		return NonRetryable("write", want)
	})
	if calls != 1 || b.Used() != 1 {
		t.Fatalf("calls=%d used=%d, want 1/1", calls, b.Used())
	}
	var ce *ClassifiedError
	if !errors.As(res.Err, &ce) || ce.Kind != KindNonRetryable {
		t.Fatalf("err = %v, want non-retryable ClassifiedError", res.Err)
	}
}

func TestDoUnclassifiedErrorIsNotRetried(t *testing.T) {
	clk := newStepClock()
	b := NewRootBudget(clk, 5, time.Time{})
	calls := 0
	res := Do(context.Background(), b, Config{}, func(ctx context.Context, p Permit) error {
		calls++
		return errors.New("mystery")
	})
	if calls != 1 || res.Attempts != 1 {
		t.Fatalf("calls=%d attempts=%d, unclassified errors must fail safe (no retry)", calls, res.Attempts)
	}
}

func TestDoExhaustsRootBudget(t *testing.T) {
	clk := newStepClock()
	b := NewRootBudget(clk, 6, time.Time{})
	calls := 0
	res := Do(context.Background(), b, Config{BaseDelay: time.Millisecond}, func(ctx context.Context, p Permit) error {
		calls++
		return Retryable("always-fails", errors.New("nope"))
	})
	var ex *Exhausted
	if !errors.As(res.Err, &ex) {
		t.Fatalf("err = %v, want *Exhausted", res.Err)
	}
	if calls != 6 || b.Used() != 6 || ex.Attempts != 6 {
		t.Fatalf("calls=%d used=%d exhaustedAt=%d, want 6", calls, b.Used(), ex.Attempts)
	}
}

func TestExponentialBackoffSequence(t *testing.T) {
	clk := newStepClock()
	b := NewRootBudget(clk, 5, time.Time{})
	cfg := Config{BaseDelay: 10 * time.Millisecond, Multiplier: 2, MaxDelay: time.Second, Jitter: 0}
	_ = Do(context.Background(), b, cfg, func(ctx context.Context, p Permit) error {
		return Retryable("x", errors.New("x"))
	})
	want := []time.Duration{10 * time.Millisecond, 20 * time.Millisecond, 40 * time.Millisecond, 80 * time.Millisecond} // sleeps before attempts 2..5
	if len(clk.sleeps) != len(want) {
		t.Fatalf("sleeps=%v, want %v", clk.sleeps, want)
	}
	for i, w := range want {
		if clk.sleeps[i] != w {
			t.Fatalf("sleep[%d]=%s, want %s", i, clk.sleeps[i], w)
		}
	}
}

func TestBackoffHonorsMaxDelay(t *testing.T) {
	clk := newStepClock()
	b := NewRootBudget(clk, 4, time.Time{})
	cfg := Config{BaseDelay: 100 * time.Millisecond, Multiplier: 10, MaxDelay: 250 * time.Millisecond, Jitter: 0}
	_ = Do(context.Background(), b, cfg, func(ctx context.Context, p Permit) error {
		return Retryable("x", errors.New("x"))
	})
	want := []time.Duration{100 * time.Millisecond, 250 * time.Millisecond, 250 * time.Millisecond}
	for i, w := range want {
		if clk.sleeps[i] != w {
			t.Fatalf("sleep[%d]=%s, want %s", i, clk.sleeps[i], w)
		}
	}
}

func TestInjectedRandControlsJitter(t *testing.T) {
	// rand=0 -> factor 1-J; rand=1 -> factor 1+J (with Jitter 0.5).
	for _, tc := range []struct {
		r    float64
		want time.Duration
	}{
		{0, 5 * time.Millisecond},
		{1, 15 * time.Millisecond},
		{0.5, 10 * time.Millisecond},
	} {
		c := Config{BaseDelay: 10 * time.Millisecond, Multiplier: 2, Jitter: 0.5, Rand: func() float64 { return tc.r }}
		if got := c.withDefaults().delayFor(1, Retryable("x", errors.New("x"))); got != tc.want {
			t.Fatalf("rand=%v delay=%s, want %s", tc.r, got, tc.want)
		}
	}
}

func TestRetryAfterActsAsFloor(t *testing.T) {
	clk := newStepClock()
	b := NewRootBudget(clk, 3, time.Time{})
	cfg := Config{BaseDelay: 10 * time.Millisecond, Multiplier: 2, Jitter: 0}
	calls := 0
	res := Do(context.Background(), b, cfg, func(ctx context.Context, p Permit) error {
		calls++
		if calls == 1 {
			return RetryableAfter("svc", errors.New("503"), 75*time.Millisecond)
		}
		if calls == 2 {
			return Retryable("svc", errors.New("503")) // normal 20ms backoff
		}
		return nil
	})
	if res.Err != nil {
		t.Fatalf("unexpected: %v", res.Err)
	}
	want := []time.Duration{75 * time.Millisecond, 20 * time.Millisecond}
	if len(clk.sleeps) != 2 || clk.sleeps[0] != want[0] || clk.sleeps[1] != want[1] {
		t.Fatalf("sleeps=%v, want %v", clk.sleeps, want)
	}
}

func TestRetryAfterSmallerThanBackoffUsesBackoff(t *testing.T) {
	c := Config{BaseDelay: 100 * time.Millisecond, Jitter: 0}
	err := RetryableAfter("svc", errors.New("503"), 5*time.Millisecond)
	if got := c.withDefaults().delayFor(1, err); got != 100*time.Millisecond {
		t.Fatalf("delay=%s, want 100ms (backoff must not shrink below schedule)", got)
	}
}

// blockClock sleeps until ctx is canceled, so cancellation mid-backoff can
// be exercised deterministically.
type blockClock struct{ t time.Time }

func (c *blockClock) Now() time.Time { return c.t }
func (c *blockClock) Sleep(ctx context.Context, d time.Duration) bool {
	<-ctx.Done()
	return false
}

func TestDoCanceledDuringBackoff(t *testing.T) {
	clk := &blockClock{t: time.Unix(1_700_000_000, 0)}
	b := NewRootBudget(clk, 5, time.Time{})
	ctx, cancel := context.WithCancel(context.Background())

	calls := make(chan int, 5)
	done := make(chan Result, 1)
	go func() {
		done <- Do(ctx, b, Config{BaseDelay: time.Second}, func(ctx context.Context, p Permit) error {
			calls <- p.Attempt
			return Retryable("x", errors.New("x"))
		})
	}()

	if got := <-calls; got != 1 {
		t.Fatalf("first attempt = %d, want 1", got)
	}
	cancel()
	res := <-done
	if !errors.Is(res.Err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", res.Err)
	}
	if res.Attempts != 1 || b.Used() != 1 {
		t.Fatalf("attempts=%d used=%d, canceled attempt is still consumed", res.Attempts, b.Used())
	}
}

func TestDoDeadlineEndsRetryLoop(t *testing.T) {
	clk := newStepClock()
	start := time.Unix(1_700_000_000, 0)
	b := NewRootBudget(clk, 10, start.Add(100*time.Millisecond))
	cfg := Config{BaseDelay: 60 * time.Millisecond, Multiplier: 2, Jitter: 0}
	calls := 0
	res := Do(context.Background(), b, cfg, func(ctx context.Context, p Permit) error {
		calls++
		return Retryable("slow", errors.New("slow"))
	})
	var de *DeadlineExceeded
	if !errors.As(res.Err, &de) {
		t.Fatalf("err = %v, want *DeadlineExceeded", res.Err)
	}
	// Attempt 1 at t=0, 60ms backoff, attempt 2 at t=60; the next backoff
	// (120ms) is clipped to the 40ms remaining, virtual time hits the
	// deadline exactly, and attempt 3 is refused. Reservations stop at 2.
	if calls != 2 || b.Used() != 2 {
		t.Fatalf("calls=%d used=%d, want 2/2", calls, b.Used())
	}
	if got := clk.sleeps; len(got) != 2 || got[0] != 60*time.Millisecond || got[1] != 40*time.Millisecond {
		t.Fatalf("sleeps=%v, want [60ms 40ms] (clipped to deadline)", got)
	}
}

// TestNestedLoopsShareRootBudget is the unit-level mirror of the three-layer
// acceptance fixture: every layer runs its own retry loop against a child
// view of one root budget. The leaf fails forever, and all layers would retry
// on their own, yet the total number of reservations is root-capped.
func TestNestedLoopsShareRootBudget(t *testing.T) {
	clk := newStepClock()
	root := NewRootBudget(clk, 8, time.Time{})
	cfg := Config{BaseDelay: time.Millisecond, Jitter: 0}

	var mu sync.Mutex
	counts := map[string]int{}
	ran := func(layer string) {
		mu.Lock()
		counts[layer]++
		mu.Unlock()
	}
	// propagate re-marks downstream failures as retryable for the local loop
	// only while they are not already terminal for the shared budget.
	propagate := func(layer string, err error) error {
		switch Classify(err) {
		case KindRetryable:
			return Retryable(layer, err)
		default:
			return err // exhausted / deadline / canceled / non-retryable: stop
		}
	}

	var l3, l2, l1 func(ctx context.Context) error
	leaf := func(ctx context.Context, p Permit) error {
		ran("leaf")
		return Retryable("leaf", errors.New("always down"))
	}
	l3 = func(ctx context.Context) error {
		r := Do(ctx, root.Child(0), cfg, func(ctx context.Context, p Permit) error {
			ran("l3")
			return leaf(ctx, p)
		})
		return r.Err
	}
	l2 = func(ctx context.Context) error {
		r := Do(ctx, root.Child(0), cfg, func(ctx context.Context, p Permit) error {
			ran("l2")
			return propagate("l2", l3(ctx))
		})
		return r.Err
	}
	l1 = func(ctx context.Context) error {
		r := Do(ctx, root, cfg, func(ctx context.Context, p Permit) error {
			ran("l1")
			return propagate("l1", l2(ctx))
		})
		return r.Err
	}

	err := l1(context.Background())

	var ex *Exhausted
	if !errors.As(err, &ex) {
		t.Fatalf("err = %v, want root *Exhausted", err)
	}
	if total := root.Used(); total != 8 {
		t.Fatalf("total reservations = %d, root budget must cap at 8", total)
	}
	// Reservations: l1 op once (#1), l2 op once (#2), leaf six times (#3..#8).
	if counts["l1"] != 1 || counts["l2"] != 1 || counts["leaf"] != 6 {
		t.Fatalf("per-layer runs = %v, want l1=1 l2=1 leaf=6", counts)
	}
}
