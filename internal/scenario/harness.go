// Package scenario runs deterministic, virtual-clock-driven reproductions of
// the circuit breaker edge cases and emits structured results.
//
// Every scenario is an in-process script: no network, no wall-clock sleeps.
// Hung calls run on real goroutines parked on channels; the virtual clock and
// the fake upstream's explicit release are the only things that unblock them,
// so the interleavings are reproducible run after run.
package scenario

import (
	"context"
	"fmt"
	"runtime"
	"sync"
	"time"

	"cbhalfopen/internal/breaker"
	"cbhalfopen/internal/fakeupstream"
	"cbhalfopen/internal/faultclient"
	"cbhalfopen/internal/vclock"
)

// Check is one assertion made during a scenario.
type Check struct {
	Name   string `json:"name"`
	Detail string `json:"detail"`
	Pass   bool   `json:"pass"`
}

// Step is one recorded scenario step.
type Step struct {
	Index    int               `json:"index"`
	Name     string            `json:"name"`
	Time     time.Time         `json:"virtual_time"`
	Detail   string            `json:"detail"`
	Snapshot *breaker.Snapshot `json:"snapshot,omitempty"`
	Checks   []Check           `json:"checks"`
}

// Report is the structured result of one scenario.
type Report struct {
	Name    string             `json:"name"`
	Goal    string             `json:"goal"`
	Pass    bool               `json:"pass"`
	VCStart time.Time          `json:"virtual_clock_start"`
	Config  breaker.ConfigView `json:"breaker_config"`
	Steps   []Step             `json:"steps"`
	Summary map[string]int64   `json:"summary_counters"`
}

// Harness carries the shared machinery for a scenario.
type Harness struct {
	VC      *vclock.VirtualClock
	Fake    *fakeupstream.Fake
	Client  *faultclient.Client
	Breaker *breaker.Breaker
	cfg     breaker.Config

	name string
	goal string

	mu    sync.Mutex
	steps []Step
}

// NewHarness builds the standard fixture.
func NewHarness(cfg breaker.Config, clientCfg faultclient.Config) *Harness {
	vc := vclock.NewVirtual(time.UnixMilli(0).UTC())
	fake := fakeupstream.New(vc, fakeupstream.ModeOK)
	client := faultclient.New(fake, vc, clientCfg)
	cfg.Clock = vc
	b, err := breaker.New(cfg)
	if err != nil {
		panic(fmt.Sprintf("scenario: invalid breaker config: %v", err))
	}
	return &Harness{VC: vc, Fake: fake, Client: client, Breaker: b, cfg: cfg}
}

// newReport starts building the structured result for this scenario. Data is
// captured at finalize time, when all steps exist.
func (h *Harness) newReport(name, goal string) *Report {
	h.name, h.goal = name, goal
	return &Report{Name: name, Goal: goal}
}

func (h *Harness) record(name, detail string, checks []Check) {
	h.mu.Lock()
	defer h.mu.Unlock()
	snap := h.Breaker.Snapshot()
	if checks == nil {
		checks = []Check{}
	}
	h.steps = append(h.steps, Step{
		Index:    len(h.steps) + 1,
		Name:     name,
		Time:     h.VC.Now(),
		Detail:   detail,
		Snapshot: &snap,
		Checks:   checks,
	})
}

func (h *Harness) check(name string, ok bool, detail string) Check {
	return Check{Name: name, Pass: ok, Detail: detail}
}

func (h *Harness) eq(name string, got, want any) Check {
	return h.check(name, fmt.Sprint(got) == fmt.Sprint(want),
		fmt.Sprintf("got=%v want=%v", got, want))
}

// callNow performs one immediate (non-hanging) call and returns its result.
func (h *Harness) callNow(label string) faultclient.Result {
	permit, err := h.Breaker.Allow()
	if err != nil {
		panic(fmt.Sprintf("%s: unexpected breaker rejection: %v", label, err))
	}
	return h.Client.Do(context.Background(), permit)
}

// hungCall is an in-flight call parked against the fake upstream.
type hungCall struct {
	permit *breaker.Permit
	done   chan faultclient.Result
	cancel context.CancelFunc
}

// startHung switches the fake to hang mode, starts a call and waits until the
// call is actually parked in the fake before returning.
func (h *Harness) startHung() *hungCall {
	h.Fake.SetMode(fakeupstream.ModeHang)
	ctx, cancel := context.WithCancel(context.Background())
	permit, err := h.Breaker.Allow()
	if err != nil {
		cancel()
		panic(fmt.Sprintf("startHung: breaker rejected: %v", err))
	}
	hc := &hungCall{permit: permit, done: make(chan faultclient.Result, 1), cancel: cancel}
	go func() {
		hc.done <- h.Client.Do(ctx, permit)
	}()
	h.waitPending(1)
	return hc
}

// startHungProbe starts a hung call on an already-acquired permit and blocks
// until it is registered (parked) in the fake. expectPending is the total
// number of calls that must be parked afterwards. Starting probes one at a
// time and waiting for registration makes their fake-assigned IDs identical
// to their launch order, so Release (which releases the oldest parked call)
// is deterministic.
func (h *Harness) startHungProbe(permit *breaker.Permit, expectPending int) *hungCall {
	ctx, cancel := context.WithCancel(context.Background())
	hc := &hungCall{permit: permit, done: make(chan faultclient.Result, 1), cancel: cancel}
	go func() {
		hc.done <- h.Client.Do(ctx, permit)
	}()
	h.waitPending(expectPending)
	return hc
}

// waitPending parks until at least n calls are hanging in the fake.
func (h *Harness) waitPending(n int) {
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if len(h.Fake.Pending()) >= n {
			return
		}
		runtime.Gosched()
	}
	panic(fmt.Sprintf("waitPending: only %d of %d calls parked after 5s", len(h.Fake.Pending()), n))
}

// result waits for the hung call to finish.
func (c *hungCall) result() faultclient.Result { return <-c.done }

// finalize captures steps and counters, computes the overall pass from the
// recorded checks and returns the completed report.
func (r *Report) finalizeFrom(h *Harness) *Report {
	h.mu.Lock()
	r.Steps = make([]Step, len(h.steps))
	copy(r.Steps, h.steps)
	h.mu.Unlock()

	snap := h.Breaker.Snapshot()
	r.VCStart = time.UnixMilli(0).UTC()
	r.Config = snap.Config
	r.Summary = map[string]int64{
		"allowed":         snap.Counters.Allowed,
		"rejected":        snap.Counters.Rejected,
		"successes":       snap.Counters.Successes,
		"failures":        snap.Counters.Failures,
		"canceled":        snap.Counters.Canceled,
		"probes_granted":  snap.Counters.ProbesGranted,
		"probes_rejected": snap.Counters.ProbesRejected,
		"stale_results":   snap.Counters.StaleResults,
	}

	r.Pass = true
	for _, s := range r.Steps {
		for _, c := range s.Checks {
			if !c.Pass {
				r.Pass = false
				return r
			}
		}
	}
	return r
}

// standardConfig is the configuration shared by the acceptance scenarios.
func standardConfig() breaker.Config {
	return breaker.Config{
		SlidingWindowSize: 10,
		MinRequests:       5,
		FailureThreshold:  0.5,
		OpenCooldown:      5 * time.Second,
		HalfOpenMaxProbes: 3,
		RequiredSuccesses: 2,
	}
}
