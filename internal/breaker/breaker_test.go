package breaker

import (
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"cbhalfopen/internal/vclock"
)

func testBreaker(t *testing.T, mutate func(*Config)) (*Breaker, *vclock.VirtualClock) {
	t.Helper()
	vc := vclock.NewVirtual(time.UnixMilli(0).UTC())
	cfg := Config{
		SlidingWindowSize: 10,
		MinRequests:       5,
		FailureThreshold:  0.5,
		OpenCooldown:      5 * time.Second,
		HalfOpenMaxProbes: 3,
		RequiredSuccesses: 2,
		Clock:             vc,
	}
	if mutate != nil {
		mutate(&cfg)
	}
	b, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return b, vc
}

func TestConfigValidation(t *testing.T) {
	cases := []struct {
		name   string
		cfg    Config
		errMsg string
	}{
		{"zero config gets defaults", Config{}, ""},
		{"negative window", Config{SlidingWindowSize: -1}, "SlidingWindowSize"},
		{"negative min requests", Config{MinRequests: -1}, "MinRequests"},
		{"threshold over one", Config{FailureThreshold: 1.5}, "FailureThreshold"},
		{"negative cooldown", Config{OpenCooldown: -1}, "OpenCooldown"},
		{"negative probes", Config{HalfOpenMaxProbes: -1}, "HalfOpenMaxProbes"},
		{"negative required successes", Config{RequiredSuccesses: -1}, "RequiredSuccesses"},
		{"threshold of one is valid", Config{FailureThreshold: 1}, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := New(tc.cfg)
			if tc.errMsg == "" {
				if err != nil {
					t.Fatalf("expected valid, got %v", err)
				}
				return
			}
			if err == nil {
				t.Fatalf("expected error containing %q, got nil", tc.errMsg)
			}
			if !strings.Contains(err.Error(), tc.errMsg) {
				t.Fatalf("error %q does not contain %q", err.Error(), tc.errMsg)
			}
		})
	}
}

func TestClosedAllowsAndWindow(t *testing.T) {
	b, _ := testBreaker(t, nil)

	for i := 0; i < 3; i++ {
		p, err := b.Allow()
		if err != nil {
			t.Fatalf("Allow #%d: %v", i, err)
		}
		p.RecordSuccess()
	}
	snap := b.Snapshot()
	if snap.State != StateClosed {
		t.Fatalf("state = %s, want closed", snap.State)
	}
	if snap.WindowSamples != 3 || snap.WindowSuccesses != 3 {
		t.Fatalf("window = %+v, want 3 samples/3 successes", snap)
	}
	if snap.Counters.Allowed != 3 {
		t.Fatalf("allowed = %d, want 3", snap.Counters.Allowed)
	}
}

func TestTrippingThreshold(t *testing.T) {
	cases := []struct {
		name      string
		failures  int
		successes int
		wantOpen  bool
	}{
		{"below min requests does not trip", 2, 0, false},
		{"three of five trips", 3, 2, true},
		{"all failures trip", 5, 0, true},
		{"healthy window stays closed", 1, 9, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			b, _ := testBreaker(t, nil)
			total := tc.failures + tc.successes
			for i := 0; i < total; i++ {
				p, err := b.Allow()
				if err != nil {
					t.Fatalf("Allow: %v", err)
				}
				if i < tc.failures {
					p.RecordFailure()
				} else {
					p.RecordSuccess()
				}
			}
			snap := b.Snapshot()
			if (snap.State == StateOpen) != tc.wantOpen {
				t.Fatalf("state = %s, wantOpen = %v (ratio %.2f)",
					snap.State, tc.wantOpen, snap.FailureRatio)
			}
		})
	}
}

func TestSlidingWindowEvictsOldSamples(t *testing.T) {
	// MinRequests 10: four failures cannot trip on their own, and the ratio
	// can never reach 0.5 before 10 samples exist (4 failures max), so the
	// breaker stays closed while old failures age out.
	b, _ := testBreaker(t, func(c *Config) { c.MinRequests = 10 })

	for i := 0; i < 4; i++ {
		p, _ := b.Allow()
		p.RecordFailure()
	}
	if got := b.Snapshot().WindowFailures; got != 4 {
		t.Fatalf("failures in window = %d, want 4", got)
	}

	// Six successes complete the 10-sample window at 4/10 = 0.4 (no trip);
	// four more overflow it, sliding all failures out.
	for i := 0; i < 10; i++ {
		p, _ := b.Allow()
		p.RecordSuccess()
	}
	snap := b.Snapshot()
	if snap.WindowFailures != 0 {
		t.Fatalf("old failures not evicted: %d remain", snap.WindowFailures)
	}
	if snap.WindowSamples != 10 {
		t.Fatalf("window size = %d, want 10", snap.WindowSamples)
	}
	if snap.State != StateClosed {
		t.Fatalf("state = %s, want closed", snap.State)
	}
}

func TestOpenRejectsUntilCooldown(t *testing.T) {
	b, vc := testBreaker(t, nil)
	trip(t, b)

	for i := 0; i < 3; i++ {
		if _, err := b.Allow(); !errors.Is(err, ErrOpen) {
			t.Fatalf("Allow #%d during open: err = %v, want ErrOpen", i, err)
		}
	}

	vc.Advance(4 * time.Second)
	if _, err := b.Allow(); !errors.Is(err, ErrOpen) {
		t.Fatalf("Allow at t=4s: err = %v, want still ErrOpen", err)
	}

	vc.Advance(1 * time.Second)
	p, err := b.Allow()
	if err != nil {
		t.Fatalf("Allow after cooldown: %v", err)
	}
	if !p.IsProbe() {
		t.Fatal("the cooldown-expiring Allow must be a probe")
	}
	if got := b.Snapshot().State; got != StateHalfOpen {
		t.Fatalf("state = %s, want half_open", got)
	}
}

func TestHalfOpenProbeLimit(t *testing.T) {
	b, vc := testBreaker(t, nil)
	trip(t, b)
	vc.Advance(5 * time.Second)

	var permits []*Permit
	for i := 0; i < 3; i++ {
		p, err := b.Allow()
		if err != nil {
			t.Fatalf("probe %d: %v", i, err)
		}
		permits = append(permits, p)
	}
	if _, err := b.Allow(); !errors.Is(err, ErrProbesExhausted) {
		t.Fatalf("4th probe: err = %v, want ErrProbesExhausted", err)
	}
	if got := b.Snapshot().ProbesInFlight; got != 3 {
		t.Fatalf("in flight = %d, want 3", got)
	}

	// Completing one frees a slot.
	permits[0].RecordSuccess()
	p, err := b.Allow()
	if err != nil {
		t.Fatalf("reused slot: %v", err)
	}
	if !p.IsProbe() {
		t.Fatal("reused-slot permit must be a probe")
	}
}

func TestHalfOpenRecoveryRequiresConsecutiveSuccesses(t *testing.T) {
	b, vc := testBreaker(t, nil)
	trip(t, b)
	vc.Advance(5 * time.Second)

	p1, _ := b.Allow()
	p1.RecordSuccess()
	if got := b.Snapshot().State; got != StateHalfOpen {
		t.Fatalf("after 1 success state = %s, want half_open", got)
	}
	p2, _ := b.Allow()
	p2.RecordSuccess()
	snap := b.Snapshot()
	if snap.State != StateClosed {
		t.Fatalf("after 2 successes state = %s, want closed", snap.State)
	}
	if snap.Generation != 3 {
		t.Fatalf("generation = %d, want 3", snap.Generation)
	}
	if snap.WindowSamples != 0 {
		t.Fatalf("window not reset: %d samples", snap.WindowSamples)
	}
}

func TestHalfOpenProbeFailureReopens(t *testing.T) {
	b, vc := testBreaker(t, nil)
	trip(t, b)
	vc.Advance(5 * time.Second)

	p1, _ := b.Allow()
	p1.RecordSuccess()
	p2, _ := b.Allow()
	p2.RecordFailure()

	snap := b.Snapshot()
	if snap.State != StateOpen {
		t.Fatalf("state = %s, want open after probe failure", snap.State)
	}
	if snap.Generation != 3 {
		t.Fatalf("generation = %d, want 3", snap.Generation)
	}
	until := snap.CooldownUntil
	if !until.Equal(vc.Now().Add(5 * time.Second)) {
		t.Fatalf("cooldown until %v, want %v", until, vc.Now().Add(5*time.Second))
	}
}

func TestStaleResultCannotChangeNewGeneration(t *testing.T) {
	b, vc := testBreaker(t, nil)

	// Old permit issued while closed.
	old, _ := b.Allow()
	trip(t, b) // closed(g0) -> open(g1)
	vc.Advance(5 * time.Second)

	p1, _ := b.Allow() // open(g1) -> half_open(g2)
	p1.RecordSuccess()
	p2, _ := b.Allow()
	p2.RecordSuccess() // half_open(g2) -> closed(g3)

	snapBefore := b.Snapshot()
	old.RecordFailure() // result from generation 0 arrives three generations late
	snapAfter := b.Snapshot()

	if snapAfter.State != StateClosed || snapAfter.Generation != 3 {
		t.Fatalf("stale result moved breaker: %+v", snapAfter)
	}
	if snapAfter.Counters.Failures != snapBefore.Counters.Failures {
		t.Fatalf("stale failure counted: before=%d after=%d",
			snapBefore.Counters.Failures, snapAfter.Counters.Failures)
	}
	if snapAfter.Counters.StaleResults != 1 {
		t.Fatalf("stale results = %d, want 1", snapAfter.Counters.StaleResults)
	}
	if snapAfter.WindowSamples != snapBefore.WindowSamples {
		t.Fatal("stale result altered the sliding window")
	}
}

func TestStaleProbeAcrossReopenIsDiscarded(t *testing.T) {
	b, vc := testBreaker(t, nil)
	trip(t, b) // -> open generation 1
	vc.Advance(5 * time.Second)

	// Probe A enters half-open generation 2 and stays in flight.
	probeA, err := b.Allow()
	if err != nil {
		t.Fatalf("probe A: %v", err)
	}

	// Probe B fails immediately, reopening the breaker as generation 3.
	probeB, err := b.Allow()
	if err != nil {
		t.Fatalf("probe B: %v", err)
	}
	probeB.RecordFailure()
	snap := b.Snapshot()
	if snap.State != StateOpen || snap.Generation != 3 {
		t.Fatalf("after probe B failure: state=%s gen=%d", snap.State, snap.Generation)
	}

	// Probe A's failure arrives in the now-stale generation 2.
	probeA.RecordFailure()
	snap = b.Snapshot()
	if snap.State != StateOpen || snap.Generation != 3 {
		t.Fatalf("stale probe A changed breaker: %+v", snap)
	}
	// 5 tripping failures + probe B only; probe A is stale.
	if snap.Counters.Failures != 6 {
		t.Fatalf("failures = %d, want 6", snap.Counters.Failures)
	}
	if snap.Counters.StaleResults != 1 {
		t.Fatalf("stale results = %d, want 1", snap.Counters.StaleResults)
	}
	if snap.ProbesInFlight != 0 {
		t.Fatalf("stale probe did not settle accounting: in flight = %d", snap.ProbesInFlight)
	}
}

func TestCanceledNeverCountsAsFailure(t *testing.T) {
	t.Run("closed state", func(t *testing.T) {
		b, _ := testBreaker(t, nil)
		p, _ := b.Allow()
		p.RecordCanceled()
		snap := b.Snapshot()
		if snap.Counters.Failures != 0 || snap.Counters.Canceled != 1 {
			t.Fatalf("counters = %+v", snap.Counters)
		}
		if snap.WindowSamples != 0 {
			t.Fatalf("canceled call entered window: %d samples", snap.WindowSamples)
		}
		if snap.State != StateClosed {
			t.Fatalf("state = %s", snap.State)
		}
	})

	t.Run("does not trip even en masse", func(t *testing.T) {
		b, _ := testBreaker(t, nil)
		for i := 0; i < 20; i++ {
			p, _ := b.Allow()
			p.RecordCanceled()
		}
		if got := b.Snapshot().State; got != StateClosed {
			t.Fatalf("state = %s, want closed after 20 cancellations", got)
		}
	})

	t.Run("half open releases slot without streak change", func(t *testing.T) {
		b, vc := testBreaker(t, nil)
		trip(t, b)
		vc.Advance(5 * time.Second)

		probe, _ := b.Allow()
		if got := b.Snapshot().ProbesInFlight; got != 1 {
			t.Fatalf("in flight = %d, want 1", got)
		}
		probe.RecordCanceled()
		snap := b.Snapshot()
		if snap.ProbesInFlight != 0 {
			t.Fatalf("slot not released: in flight = %d", snap.ProbesInFlight)
		}
		if snap.State != StateHalfOpen {
			t.Fatalf("state = %s, want half_open", snap.State)
		}
		if snap.ConsecutiveSuccesses != 0 {
			t.Fatalf("streak = %d, want 0", snap.ConsecutiveSuccesses)
		}
		if snap.Counters.Failures != 5 {
			t.Fatalf("cancellation counted as failure: %d", snap.Counters.Failures)
		}

		// Freed slot is reusable and recovery still works.
		p1, _ := b.Allow()
		p1.RecordSuccess()
		p2, _ := b.Allow()
		p2.RecordSuccess()
		if got := b.Snapshot().State; got != StateClosed {
			t.Fatalf("state after recovery = %s", got)
		}
	})
}

func TestPermitDoubleCompletion(t *testing.T) {
	b, _ := testBreaker(t, nil)
	p, _ := b.Allow()
	p.RecordSuccess()
	p.RecordFailure() // must be ignored, not double-counted or panic
	snap := b.Snapshot()
	if snap.Counters.Successes != 1 || snap.Counters.Failures != 0 {
		t.Fatalf("double completion counted: %+v", snap.Counters)
	}
}

func TestConcurrentAllowsAndCompletions(t *testing.T) {
	b, vc := testBreaker(t, func(c *Config) {
		c.HalfOpenMaxProbes = 8
		c.RequiredSuccesses = 64
		c.OpenCooldown = time.Hour
	})

	var wg sync.WaitGroup
	// Closed-state hammering.
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			p, err := b.Allow()
			if err != nil {
				return
			}
			if i%3 == 0 {
				p.RecordFailure()
			} else {
				p.RecordSuccess()
			}
		}(i)
	}
	wg.Wait()

	// Whether it tripped or not, counters must be internally consistent.
	snap := b.Snapshot()
	completed := snap.Counters.Successes + snap.Counters.Failures + snap.Counters.Canceled
	if completed+snap.Counters.StaleResults != snap.Counters.Allowed {
		t.Fatalf("accounting mismatch: allowed=%d but accounted=%d (counters=%+v)",
			snap.Counters.Allowed, completed+snap.Counters.StaleResults, snap.Counters)
	}

	// Force into half-open and hammer probe slots.
	if snap.State != StateOpen {
		trip(t, b)
	}
	vc.Advance(time.Hour)

	var granted, rejected int64
	var mu sync.Mutex
	for i := 0; i < 64; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			p, err := b.Allow()
			if err != nil {
				mu.Lock()
				rejected++
				mu.Unlock()
				return
			}
			mu.Lock()
			granted++
			mu.Unlock()
			p.RecordSuccess()
		}()
	}
	wg.Wait()
	if granted+rejected == 0 {
		t.Fatal("no probe decisions made")
	}
	final := b.Snapshot()
	if final.ProbesInFlight < 0 || final.ProbesInFlight > final.HalfOpenMaxProbes {
		t.Fatalf("impossible in-flight count %d", final.ProbesInFlight)
	}
	t.Logf("half-open probes granted=%d rejected=%d final state=%s", granted, rejected, final.State)
}

func TestTransitionsRecorded(t *testing.T) {
	b, vc := testBreaker(t, nil)
	trip(t, b)
	vc.Advance(5 * time.Second)
	p1, _ := b.Allow()
	p1.RecordSuccess()
	p2, _ := b.Allow()
	p2.RecordSuccess()

	tr := b.Transitions()
	want := []struct {
		from, to State
		reason   string
	}{
		{StateClosed, StateClosed, "initialized"},
		{StateClosed, StateOpen, "failure_threshold_exceeded"},
		{StateOpen, StateHalfOpen, "cooldown_elapsed"},
		{StateHalfOpen, StateClosed, "probes_succeeded"},
	}
	if len(tr) != len(want) {
		t.Fatalf("transitions = %d, want %d: %+v", len(tr), len(want), tr)
	}
	for i, w := range want {
		if tr[i].From != w.from || tr[i].To != w.to || tr[i].Reason != w.reason {
			t.Fatalf("transition %d = %+v, want %s->%s (%s)", i, tr[i], w.from, w.to, w.reason)
		}
	}
}

func TestOnTransitionCallback(t *testing.T) {
	vc := vclock.NewVirtual(time.UnixMilli(0).UTC())
	var got []Transition
	var mu sync.Mutex
	cfg := Config{
		SlidingWindowSize: 10, MinRequests: 5, FailureThreshold: 0.5,
		OpenCooldown: 5 * time.Second, HalfOpenMaxProbes: 3, RequiredSuccesses: 2,
		Clock: vc,
		OnTransition: func(tr Transition) {
			mu.Lock()
			got = append(got, tr)
			mu.Unlock()
		},
	}
	b, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	trip(t, b)
	mu.Lock()
	defer mu.Unlock()
	if len(got) != 1 || got[0].To != StateOpen {
		t.Fatalf("callback transitions = %+v", got)
	}
}

func TestSnapshotFailureRatio(t *testing.T) {
	b, _ := testBreaker(t, nil)
	outcomes := []Outcome{OutcomeFailure, OutcomeSuccess, OutcomeFailure, OutcomeSuccess, OutcomeSuccess}
	for _, o := range outcomes {
		p, _ := b.Allow()
		switch o {
		case OutcomeFailure:
			p.RecordFailure()
		case OutcomeSuccess:
			p.RecordSuccess()
		}
	}
	snap := b.Snapshot()
	if snap.FailureRatio < 0.39 || snap.FailureRatio > 0.41 {
		t.Fatalf("ratio = %.3f, want ~0.40", snap.FailureRatio)
	}
}

func trip(t *testing.T, b *Breaker) {
	t.Helper()
	for i := 0; i < 5; i++ {
		p, err := b.Allow()
		if err != nil {
			t.Fatalf("trip Allow %d: %v", i, err)
		}
		p.RecordFailure()
	}
	if got := b.Snapshot().State; got != StateOpen {
		t.Fatalf("trip: state = %s, want open", got)
	}
}

func TestGenerationMonotonic(t *testing.T) {
	b, vc := testBreaker(t, nil)
	if g := b.Snapshot().Generation; g != 0 {
		t.Fatalf("initial generation = %d, want 0", g)
	}
	trip(t, b)
	if g := b.Snapshot().Generation; g != 1 {
		t.Fatalf("open generation = %d, want 1", g)
	}
	vc.Advance(5 * time.Second)
	p, _ := b.Allow()
	if g := p.Generation(); g != 2 {
		t.Fatalf("probe permit generation = %d, want 2", g)
	}
	p.RecordFailure()
	if g := b.Snapshot().Generation; g != 3 {
		t.Fatalf("reopen generation = %d, want 3", g)
	}
}

func ExampleBreaker() {
	vc := vclock.NewVirtual(time.UnixMilli(0).UTC())
	b, _ := New(Config{
		SlidingWindowSize: 10, MinRequests: 5, FailureThreshold: 0.5,
		OpenCooldown: 5 * time.Second, HalfOpenMaxProbes: 3, RequiredSuccesses: 2,
		Clock: vc,
	})
	for i := 0; i < 5; i++ {
		p, _ := b.Allow()
		p.RecordFailure()
	}
	fmt.Println(b.Snapshot().State)
	vc.Advance(5 * time.Second)
	probe, _ := b.Allow()
	fmt.Println(b.Snapshot().State, probe.IsProbe())
	// Output:
	// open
	// half_open true
}
