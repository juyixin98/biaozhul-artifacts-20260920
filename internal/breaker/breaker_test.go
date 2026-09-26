package breaker

import (
	"errors"
	"sync"
	"testing"
	"time"

	"breakerhalfopen/internal/clock"
)

func testBreaker(t *testing.T, cfg Config) (*Breaker, *clock.Virtual) {
	t.Helper()
	clk := clock.NewVirtual()
	return New(cfg, clk), clk
}

func baseCfg() Config {
	return Config{
		WindowSize:               5,
		FailureThreshold:         3,
		OpenCoolDown:             10 * time.Second,
		MaxProbeCalls:            2,
		HalfOpenSuccessThreshold: 2,
	}
}

func finish(p *Permit, o Outcome) { p.Done(o) }

// TestClosedAllowsAndTracks: CLOSED grants every call and counts outcomes.
func TestClosedAllowsAndTracks(t *testing.T) {
	b, _ := testBreaker(t, baseCfg())

	for i := 0; i < 3; i++ {
		p, err := b.Allow()
		if err != nil {
			t.Fatalf("Allow #%d: %v", i, err)
		}
		finish(p, OutcomeSuccess)
	}
	s := b.Snapshot()
	if s.State != StateClosed || s.TotalCalls != 3 || s.TotalSuccesses != 3 {
		t.Fatalf("unexpected snapshot %+v", s)
	}
	if s.InFlight != 0 {
		t.Fatalf("in-flight = %d, want 0", s.InFlight)
	}
}

// TestTripsAtThreshold: threshold failures within the window trip OPEN.
func TestTripsAtThreshold(t *testing.T) {
	b, _ := testBreaker(t, baseCfg())
	genBefore := b.Generation()

	for i := 0; i < 2; i++ {
		p, _ := b.Allow()
		finish(p, OutcomeFailure)
		if got := b.State(); got != StateClosed {
			t.Fatalf("after %d failures state=%s, want closed", i+1, got)
		}
	}
	p, _ := b.Allow()
	finish(p, OutcomeFailure)

	if got := b.State(); got != StateOpen {
		t.Fatalf("state=%s, want open", got)
	}
	if b.Generation() != genBefore+1 {
		t.Fatalf("generation=%d, want %d (trip must bump)", b.Generation(), genBefore+1)
	}
	if _, err := b.Allow(); !errors.Is(err, ErrOpen) {
		t.Fatalf("Allow while open err=%v, want ErrOpen", err)
	}
	s := b.Snapshot()
	if s.TotalRejected != 1 {
		t.Fatalf("rejected=%d, want 1", s.TotalRejected)
	}
}

// TestSlidingWindow: failures that slide out of the sample window no longer
// count toward tripping.
func TestSlidingWindow(t *testing.T) {
	cfg := baseCfg() // window 5, threshold 3
	b, _ := testBreaker(t, cfg)

	seq := []Outcome{
		OutcomeFailure, OutcomeFailure,
		OutcomeSuccess, OutcomeSuccess, OutcomeSuccess,
		// window now [f f s s s]; next failure slides oldest f out,
		// window becomes [f s s s f] = 2 failures, must NOT trip.
		OutcomeFailure,
		OutcomeSuccess, OutcomeSuccess,
		// window [s s f s s] = 1 failure.
	}
	for _, o := range seq {
		p, err := b.Allow()
		if err != nil {
			t.Fatalf("Allow: %v", err)
		}
		finish(p, o)
	}
	if got := b.State(); got != StateClosed {
		t.Fatalf("state=%s, want closed (failures should have slid out)", got)
	}
	s := b.Snapshot()
	if s.WindowFailures != 1 {
		t.Fatalf("window failures=%d, want 1", s.WindowFailures)
	}
}

// TestCoolDownEntersHalfOpenAndRecovers: OPEN -> HALF_OPEN after cool-down,
// bounded probes, threshold consecutive successes close the breaker.
func TestCoolDownEntersHalfOpenAndRecovers(t *testing.T) {
	b, clk := testBreaker(t, baseCfg())
	trip(t, b, 3)
	genOpen := b.Generation()

	clk.Advance(9 * time.Second)
	if got := b.State(); got != StateOpen {
		t.Fatalf("before cooldown state=%s, want open", got)
	}
	clk.Advance(1 * time.Second)
	if got := b.State(); got != StateHalfOpen {
		t.Fatalf("after cooldown state=%s, want half_open", got)
	}
	if b.Generation() != genOpen+1 {
		t.Fatalf("generation=%d, want %d", b.Generation(), genOpen+1)
	}

	// Two probe slots; first success does not close yet.
	p1, err := b.Allow()
	if err != nil {
		t.Fatalf("probe1: %v", err)
	}
	if !p1.IsProbe() {
		t.Fatal("permit should be a probe")
	}
	finish(p1, OutcomeSuccess)
	if got := b.State(); got != StateHalfOpen {
		t.Fatalf("after 1 probe success state=%s, want half_open", got)
	}
	s := b.Snapshot()
	if s.ProbeSuccesses != 1 {
		t.Fatalf("probe successes=%d, want 1", s.ProbeSuccesses)
	}

	p2, _ := b.Allow()
	finish(p2, OutcomeSuccess)
	if got := b.State(); got != StateClosed {
		t.Fatalf("after 2 probe successes state=%s, want closed", got)
	}
	if b.Generation() != genOpen+2 {
		t.Fatalf("generation=%d, want %d", b.Generation(), genOpen+2)
	}
	// Fresh window after recovery.
	if s := b.Snapshot(); s.WindowTotal != 0 {
		t.Fatalf("window total after recovery=%d, want 0", s.WindowTotal)
	}
}

// TestProbeSlotsBounded: HALF_OPEN admits at most MaxProbeCalls concurrent
// probes and admits again once a slot frees.
func TestProbeSlotsBounded(t *testing.T) {
	b, clk := testBreaker(t, baseCfg())
	trip(t, b, 3)
	clk.Advance(10 * time.Second)

	p1, _ := b.Allow()
	p2, _ := b.Allow()
	if s := b.Snapshot(); s.ProbePermitsFree != 0 {
		t.Fatalf("free permits=%d, want 0", s.ProbePermitsFree)
	}
	if _, err := b.Allow(); !errors.Is(err, ErrOpen) {
		t.Fatalf("third probe err=%v, want ErrOpen", err)
	}

	// Free a slot with a success (run=1/2, still half-open): new probe admitted.
	finish(p1, OutcomeSuccess)
	if s := b.Snapshot(); s.ProbePermitsFree != 1 {
		t.Fatalf("free permits=%d, want 1", s.ProbePermitsFree)
	}
	p3, err := b.Allow()
	if err != nil {
		t.Fatalf("replacement probe: %v", err)
	}
	if _, err := b.Allow(); !errors.Is(err, ErrOpen) {
		t.Fatalf("probe while full again err=%v, want ErrOpen", err)
	}
	finish(p2, OutcomeSuccess)
	finish(p3, OutcomeSuccess)
	if got := b.State(); got != StateClosed {
		t.Fatalf("state=%s, want closed", got)
	}
}

// TestFailingProbeReopens: a failed half-open probe reopens and bumps the
// generation; a required run of successes is reset.
func TestFailingProbeReopens(t *testing.T) {
	b, clk := testBreaker(t, baseCfg())
	trip(t, b, 3)
	clk.Advance(10 * time.Second)
	if got := b.State(); got != StateHalfOpen { // observe: triggers OPEN -> HALF_OPEN
		t.Fatalf("after cooldown state=%s, want half_open", got)
	}
	genHalf := b.Generation()

	p1, _ := b.Allow()
	finish(p1, OutcomeSuccess) // 1 of 2
	p2, _ := b.Allow()
	finish(p2, OutcomeFailure) // reopens

	if got := b.State(); got != StateOpen {
		t.Fatalf("state=%s, want open", got)
	}
	if b.Generation() != genHalf+1 {
		t.Fatalf("generation=%d, want %d", b.Generation(), genHalf+1)
	}
	if _, err := b.Allow(); !errors.Is(err, ErrOpen) {
		t.Fatalf("Allow err=%v, want ErrOpen", err)
	}

	clk.Advance(10 * time.Second)
	if got := b.State(); got != StateHalfOpen {
		t.Fatalf("state=%s, want half_open (new round)", got)
	}
	if s := b.Snapshot(); s.ProbeSuccesses != 0 {
		t.Fatalf("new round probe successes=%d, want 0", s.ProbeSuccesses)
	}
}

// TestCanceledNeverCountsAsFailure: cancels in CLOSED cannot trip and a
// canceled half-open probe neither closes nor reopens.
func TestCanceledNeverCountsAsFailure(t *testing.T) {
	b, clk := testBreaker(t, baseCfg())

	for i := 0; i < 10; i++ { // far above threshold if these were failures
		p, _ := b.Allow()
		finish(p, OutcomeCanceled)
	}
	if got := b.State(); got != StateClosed {
		t.Fatalf("state after cancels=%s, want closed", got)
	}
	s := b.Snapshot()
	if s.TotalCanceled != 10 || s.WindowFailures != 0 {
		t.Fatalf("snapshot=%+v", s)
	}

	// One real failure then cancels: still no trip.
	p, _ := b.Allow()
	finish(p, OutcomeFailure)
	for i := 0; i < 5; i++ {
		p, _ := b.Allow()
		finish(p, OutcomeCanceled)
	}
	if got := b.State(); got != StateClosed {
		t.Fatalf("state=%s, want closed", got)
	}

	// In half-open, canceled probe is no verdict and frees its slot.
	trip(t, b, 3) // earlier failure slid out of the 5-sample window
	clk.Advance(10 * time.Second)
	hp, _ := b.Allow()
	finish(hp, OutcomeCanceled)
	if got := b.State(); got != StateHalfOpen {
		t.Fatalf("after canceled probe state=%s, want half_open", got)
	}
	if s := b.Snapshot(); s.ProbePermitsFree != 2 {
		t.Fatalf("free permits after canceled probe=%d, want 2", s.ProbePermitsFree)
	}
}

// TestStalePermitCannotChangeNewGeneration is the core "old generation" rule:
// a permit from CLOSED completing after the breaker trips (and again after it
// reaches half-open) must not affect the new generation.
func TestStalePermitCannotChangeNewGeneration(t *testing.T) {
	b, clk := testBreaker(t, baseCfg())

	// Old call leaves CLOSED and stays in flight.
	old, _ := b.Allow()
	trip(t, b, 3) // 3 other failures trip while old is in flight
	genOpen := b.Generation()

	clk.Advance(10 * time.Second)               // half-open, another generation
	if got := b.State(); got != StateHalfOpen { // observe triggers the transition
		t.Fatalf("after cooldown state=%s, want half_open", got)
	}
	genHalf := b.Generation()

	// The stale call now succeeds, then another stale-style failure: neither
	// may close nor reopen nor touch probe counters.
	old.Done(OutcomeSuccess)
	if b.Generation() != genHalf || b.State() != StateHalfOpen {
		t.Fatalf("stale success changed state: gen=%d state=%s", b.Generation(), b.State())
	}
	s := b.Snapshot()
	if s.ProbeSuccesses != 0 || s.InFlight != 0 {
		t.Fatalf("stale success touched current counters: %+v", s)
	}
	if s.TotalSuccesses < 1 {
		t.Fatalf("lifetime successes should still record stale call: %+v", s)
	}

	// A stale failure likewise cannot reopen the half-open generation.
	stale, _ := func() (*Permit, error) {
		// Simulate a permit from the OPEN generation by hand: probes are the
		// only permits in half-open, so acquire a probe in THIS generation and
		// trip via another probe to make the first one stale.
		return b.Allow()
	}()
	if stale == nil {
		t.Fatal("expected probe permit")
	}
	// Make `stale` stale: fail a different probe to reopen. But the slot is
	// occupied by stale; with MaxProbeCalls=2 a second probe can run.
	other, err := b.Allow()
	if err != nil {
		t.Fatalf("second probe: %v", err)
	}
	other.Done(OutcomeFailure) // reopen -> stale belongs to dead half-open
	if b.State() != StateOpen || b.Generation() != genHalf+1 {
		t.Fatalf("expected reopen, got state=%s gen=%d", b.State(), b.Generation())
	}
	stale.Done(OutcomeFailure) // late failure from the dead round
	if b.State() != StateOpen {
		t.Fatalf("stale probe failure changed state to %s", b.State())
	}
	// in-flight accounting must not go negative.
	if s := b.Snapshot(); s.InFlight != 0 {
		t.Fatalf("in-flight after stale completion=%d, want 0", s.InFlight)
	}
	_ = genOpen
}

// TestDoubleDoneIgnored: completing a permit twice has no effect.
func TestDoubleDoneIgnored(t *testing.T) {
	b, _ := testBreaker(t, baseCfg())
	p, _ := b.Allow()
	p.Done(OutcomeFailure)
	p.Done(OutcomeSuccess) // must not be recorded
	s := b.Snapshot()
	if s.TotalFailures != 1 || s.TotalSuccesses != 0 || s.WindowFailures != 1 {
		t.Fatalf("double Done not ignored: %+v", s)
	}
}

// TestConcurrentProbeContention hammers Allow from many goroutines under
// -race: at most MaxProbeCalls probes ever run concurrently.
func TestConcurrentProbeContention(t *testing.T) {
	cfg := baseCfg()
	cfg.HalfOpenSuccessThreshold = 1_000_000 // keep half-open for the whole test
	b, clk := testBreaker(t, cfg)
	trip(t, b, 3)
	clk.Advance(10 * time.Second)

	const goroutines = 64
	var wg sync.WaitGroup
	var maxSeenMu sync.Mutex
	maxSeen := 0
	active := 0

	wg.Add(goroutines)
	for i := 0; i < goroutines; i++ {
		go func() {
			defer wg.Done()
			p, err := b.Allow()
			if err != nil {
				return // rejected: correct under contention
			}
			s := b.Snapshot()
			maxSeenMu.Lock()
			active = s.InFlight
			if active > maxSeen {
				maxSeen = active
			}
			maxSeenMu.Unlock()
			p.Done(OutcomeCanceled) // free slot without a verdict
		}()
	}
	wg.Wait()

	if maxSeen > cfg.MaxProbeCalls {
		t.Fatalf("concurrent probes=%d, max allowed=%d", maxSeen, cfg.MaxProbeCalls)
	}
	if got := b.State(); got != StateHalfOpen {
		t.Fatalf("state=%s, want half_open", got)
	}
}

// trip fires exactly n immediate failures from CLOSED.
func trip(t *testing.T, b *Breaker, n int) {
	t.Helper()
	for i := 0; i < n; i++ {
		p, err := b.Allow()
		if err != nil {
			t.Fatalf("trip setup Allow #%d: %v", i, err)
		}
		p.Done(OutcomeFailure)
	}
	if got := b.State(); got != StateOpen {
		t.Fatalf("trip setup: state=%s, want open", got)
	}
}
