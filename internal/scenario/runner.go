// Package scenario runs scripted, fully deterministic acceptance scenarios
// against breaker + fault-injecting client + fake upstream, all driven by a
// virtual clock. Each run produces a structured Report that records every
// clock-advanced step, call result and breaker snapshot, plus pass/fail
// expectation checks.
package scenario

import (
	"context"
	"fmt"
	"reflect"
	"sync"
	"time"

	"breakerhalfopen/internal/appclient"
	"breakerhalfopen/internal/breaker"
	"breakerhalfopen/internal/clock"
	"breakerhalfopen/internal/upstream"
)

// StepEntry is one recorded step in a scenario report.
type StepEntry struct {
	N        int                `json:"n"`
	Time     time.Time          `json:"time"`
	Kind     string             `json:"kind"` // call | await | advance | expect | note
	Label    string             `json:"label"`
	Attempt  *appclient.Attempt `json:"attempt,omitempty"`
	Snapshot *breaker.Snapshot  `json:"snapshot,omitempty"`
	Detail   string             `json:"detail,omitempty"`
	OK       bool               `json:"ok"`
}

// Report is the structured result of one scenario.
type Report struct {
	Name        string                `json:"name"`
	Description string                `json:"description"`
	StartTime   time.Time             `json:"start_time"`
	Config      breaker.Config        `json:"config"`
	Steps       []StepEntry           `json:"steps"`
	Final       breaker.Snapshot      `json:"final_snapshot"`
	Upstream    []upstream.CallRecord `json:"upstream_records"`
	History     []appclient.Attempt   `json:"client_history"`
	Pass        bool                  `json:"pass"`
	Failures    []string              `json:"failures"`
}

// Runner is the scripting surface handed to a scenario.
type Runner struct {
	name   string
	desc   string
	cfg    breaker.Config
	clk    *clock.Virtual
	up     *upstream.Upstream
	brk    *breaker.Breaker
	client *appclient.Client

	mu       sync.Mutex
	steps    []StepEntry
	failures []string
	n        int
}

// Env bundles construction parameters for a scenario.
type Env struct {
	Config      breaker.Config
	CallTimeout time.Duration
	FallbackOK  bool
	Script      []upstream.Directive
}

func newRunner(name, desc string, env Env) *Runner {
	clk := clock.NewVirtual()
	fallback := upstream.Directive{Fail: !env.FallbackOK}
	up := upstream.New(clk, fallback, env.Script...)
	brk := breaker.New(env.Config, clk)
	cl := appclient.New(brk, up, clk, env.CallTimeout)
	return &Runner{name: name, desc: desc, cfg: env.Config, clk: clk, up: up, brk: brk, client: cl}
}

// Clock exposes the virtual clock for scenarios that need raw access.
func (r *Runner) Clock() *clock.Virtual { return r.clk }

// Upstream exposes the fake (e.g. to release stalls).
func (r *Runner) Upstream() *upstream.Upstream { return r.up }

// CallHandle is an in-flight asynchronous call.
type CallHandle struct {
	ch chan appclient.Attempt
	a  appclient.Attempt
	ok bool
}

// GoCall starts a call on another goroutine.
func (r *Runner) GoCall(label string, ctx context.Context) *CallHandle {
	h := &CallHandle{ch: make(chan appclient.Attempt, 1)}
	go func() { h.ch <- r.client.Call(ctx) }()
	r.record(StepEntry{Kind: "call", Label: label, Detail: "started asynchronously", OK: true})
	return h
}

// Await blocks until the async call finishes and records its result.
func (r *Runner) Await(label string, h *CallHandle) appclient.Attempt {
	a := <-h.ch
	h.a, h.ok = a, true
	r.record(StepEntry{Kind: "await", Label: label, Attempt: &a, Snapshot: r.snap(), OK: true})
	return a
}

// AwaitAny blocks until one of the not-yet-consumed handles finishes, records
// it and returns its index. Already consumed handles are skipped.
func (r *Runner) AwaitAny(label string, hs ...*CallHandle) (int, appclient.Attempt) {
	var pending []*CallHandle
	for _, h := range hs {
		if !h.ok {
			pending = append(pending, h)
		}
	}
	if len(pending) == 0 {
		panic("AwaitAny: no pending handles")
	}
	cases := make([]reflect.SelectCase, len(pending))
	for i, h := range pending {
		cases[i] = reflect.SelectCase{Dir: reflect.SelectRecv, Chan: reflect.ValueOf(h.ch)}
	}
	idx, v, _ := reflect.Select(cases)
	a := v.Interface().(appclient.Attempt)
	pending[idx].a, pending[idx].ok = a, true
	// Map back to the caller's index.
	for i, h := range hs {
		if h == pending[idx] {
			idx = i
			break
		}
	}
	r.record(StepEntry{Kind: "await", Label: fmt.Sprintf("%s (handle %d)", label, idx), Attempt: &a, Snapshot: r.snap(), OK: true})
	return idx, a
}

// Call performs a synchronous call and records it.
func (r *Runner) Call(label string, ctx context.Context) appclient.Attempt {
	a := r.client.Call(ctx)
	r.record(StepEntry{Kind: "call", Label: label, Attempt: &a, Snapshot: r.snap(), OK: true})
	return a
}

// Advance moves virtual time and records the step.
func (r *Runner) Advance(label string, d time.Duration) {
	r.clk.Advance(d)
	r.record(StepEntry{Kind: "advance", Label: label, Detail: d.String(), Snapshot: r.snap(), OK: true})
}

// WaitActive blocks until n fake-upstream calls are in flight (real-thread
// synchronisation; no virtual time involved).
func (r *Runner) WaitActive(n int) { r.up.WaitActiveN(n) }

// Note records a free-form checkpoint.
func (r *Runner) Note(format string, args ...any) {
	r.record(StepEntry{Kind: "note", Label: fmt.Sprintf(format, args...), Snapshot: r.snap(), OK: true})
}

// Snapshot records a labelled checkpoint.
func (r *Runner) Snapshot(label string) breaker.Snapshot {
	s := r.snap()
	r.record(StepEntry{Kind: "note", Label: label, Snapshot: s, OK: true})
	return *s
}

func (r *Runner) snap() *breaker.Snapshot {
	s := r.brk.Snapshot()
	return &s
}

// ExpectState checks the current state.
func (r *Runner) ExpectState(want breaker.State) {
	s := r.snap()
	ok := s.State == want
	detail := fmt.Sprintf("want=%s got=%s", want, s.State)
	r.check(ok, "state: "+detail)
	r.record(StepEntry{Kind: "expect", Label: "ExpectState", Detail: detail, Snapshot: s, OK: ok})
}

// ExpectGeneration checks the generation counter.
func (r *Runner) ExpectGeneration(want uint64) {
	s := r.snap()
	ok := s.Generation == want
	detail := fmt.Sprintf("want=%d got=%d", want, s.Generation)
	r.check(ok, "generation: "+detail)
	r.record(StepEntry{Kind: "expect", Label: "ExpectGeneration", Detail: detail, Snapshot: s, OK: ok})
}

// ExpectCounters checks the lifetime counters named in want. Only non-zero
// fields are compared. For counters where zero is meaningful, use
// ExpectInFlight / ExpectProbePermitsFree.
func (r *Runner) ExpectCounters(want Counters) {
	s := r.snap()
	got := Counters{
		Calls: s.TotalCalls, Successes: s.TotalSuccesses, Failures: s.TotalFailures,
		Canceled: s.TotalCanceled, Rejected: s.TotalRejected,
		WindowFailures: uint64(s.WindowFailures), ProbeSuccesses: uint64(s.ProbeSuccesses),
	}
	misses := diffCounters(want, got)
	ok := len(misses) == 0
	detail := fmt.Sprintf("want=%+v got=%+v", want, got)
	r.check(ok, joinMisses("counters", misses))
	r.record(StepEntry{Kind: "expect", Label: "ExpectCounters", Detail: detail, Snapshot: s, OK: ok})
}

// ExpectInFlight asserts the number of calls currently in flight (zero is a
// meaningful, explicitly asserted value).
func (r *Runner) ExpectInFlight(want int) {
	s := r.snap()
	ok := s.InFlight == want
	detail := fmt.Sprintf("in_flight: want=%d got=%d", want, s.InFlight)
	r.check(ok, detail)
	r.record(StepEntry{Kind: "expect", Label: "ExpectInFlight", Detail: detail, Snapshot: s, OK: ok})
}

// ExpectProbePermitsFree asserts free half-open probe permits.
func (r *Runner) ExpectProbePermitsFree(want int) {
	s := r.snap()
	ok := s.ProbePermitsFree == want
	detail := fmt.Sprintf("probe_permits_free: want=%d got=%d", want, s.ProbePermitsFree)
	r.check(ok, detail)
	r.record(StepEntry{Kind: "expect", Label: "ExpectProbePermitsFree", Detail: detail, Snapshot: s, OK: ok})
}

// ExpectResult checks a finished attempt's classification.
func (r *Runner) ExpectResult(label string, a appclient.Attempt, want appclient.Result) {
	ok := a.Result == want
	detail := fmt.Sprintf("%s: want=%s got=%s", label, want, a.Result)
	r.check(ok, detail)
	s := r.snap()
	r.record(StepEntry{Kind: "expect", Label: "ExpectResult", Detail: detail, Snapshot: s, OK: ok})
}

// ExpectUpstreamActive asserts how many fake-upstream calls are in flight.
func (r *Runner) ExpectUpstreamActive(want int, note string) {
	got := r.up.ActiveCalls()
	ok := got == want
	detail := fmt.Sprintf("%s: want_active=%d got_active=%d", note, want, got)
	r.check(ok, detail)
	r.record(StepEntry{Kind: "expect", Label: "ExpectUpstreamActive", Detail: detail, Snapshot: r.snap(), OK: ok})
}

// ExpectUpstreamFinished asserts how many calls the fake upstream has
// completed (rejected breaker attempts must never appear here).
func (r *Runner) ExpectUpstreamFinished(want int) {
	got := len(r.up.Records())
	ok := got == want
	detail := fmt.Sprintf("want_finished=%d got_finished=%d", want, got)
	r.check(ok, detail)
	r.record(StepEntry{Kind: "expect", Label: "ExpectUpstreamFinished", Detail: detail, Snapshot: r.snap(), OK: ok})
}

// Counters are the cumulative counters scenarios assert on; zero-valued
// fields are ignored. In-flight and free probe permits (where zero is
// meaningful) are asserted through dedicated methods.
type Counters struct {
	Calls          uint64
	Successes      uint64
	Failures       uint64
	Canceled       uint64
	Rejected       uint64
	WindowFailures uint64
	ProbeSuccesses uint64
}

type miss struct {
	field     string
	want, got any
}

func diffCounters(want, got Counters) []miss {
	var m []miss
	add := func(field string, w, g any, set bool) {
		if set && w != g {
			m = append(m, miss{field, w, g})
		}
	}
	add("calls", want.Calls, got.Calls, want.Calls != 0)
	add("successes", want.Successes, got.Successes, want.Successes != 0)
	add("failures", want.Failures, got.Failures, want.Failures != 0)
	add("canceled", want.Canceled, got.Canceled, want.Canceled != 0)
	add("rejected", want.Rejected, got.Rejected, want.Rejected != 0)
	add("window_failures", want.WindowFailures, got.WindowFailures, want.WindowFailures != 0)
	add("probe_successes", want.ProbeSuccesses, got.ProbeSuccesses, want.ProbeSuccesses != 0)
	return m
}

func joinMisses(prefix string, m []miss) string {
	s := prefix
	for _, x := range m {
		s += fmt.Sprintf("; %s want=%v got=%v", x.field, x.want, x.got)
	}
	return s
}

func (r *Runner) check(ok bool, msg string) {
	if !ok {
		r.mu.Lock()
		r.failures = append(r.failures, msg)
		r.mu.Unlock()
	}
}

func (r *Runner) record(e StepEntry) {
	r.mu.Lock()
	r.n++
	e.N = r.n
	if e.Time.IsZero() {
		e.Time = r.clk.Now()
	}
	r.steps = append(r.steps, e)
	r.mu.Unlock()
}

// Report finalises and returns the structured result.
func (r *Runner) Report() Report {
	r.mu.Lock()
	defer r.mu.Unlock()
	return Report{
		Name:        r.name,
		Description: r.desc,
		StartTime:   r.clk.Now(),
		Config:      r.cfg,
		Steps:       append([]StepEntry(nil), r.steps...),
		Final:       r.brk.Snapshot(),
		Upstream:    r.up.Records(),
		History:     r.client.History(),
		Pass:        len(r.failures) == 0,
		Failures:    append([]string(nil), r.failures...),
	}
}
