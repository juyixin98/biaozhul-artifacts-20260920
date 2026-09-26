package scenario

import (
	"context"
	"time"

	"breakerhalfopen/internal/appclient"
	"breakerhalfopen/internal/breaker"
	"breakerhalfopen/internal/upstream"
)

// All scenarios use: trip at 3 failures within the last 5 samples, 10s
// cool-down, 2 concurrent probes, 2 consecutive probe successes to recover.
func defaultConfig() breaker.Config {
	return breaker.Config{
		WindowSize:               5,
		FailureThreshold:         3,
		OpenCoolDown:             10 * time.Second,
		MaxProbeCalls:            2,
		HalfOpenSuccessThreshold: 2,
	}
}

// ScenarioLateFailure: a call accepted in CLOSED is still in flight when the
// breaker trips on later failures. Its failure arrives only after a new
// generation has begun (HALF_OPEN). It must not change the new generation's
// state or transition counters.
func ScenarioLateFailure() Report {
	env := Env{
		Config: defaultConfig(),
		Script: []upstream.Directive{
			{Stall: true, Fail: true}, // #1 slow old-generation call, fails late
			{Fail: true},              // #2
			{Fail: true},              // #3
			{Fail: true},              // #4 -> trip (3 failures in window)
		},
		FallbackOK: true, // probes afterwards are healthy
	}
	r := newRunner("late_failure",
		"Late failure from an old-generation CLOSED call must not alter the newer HALF_OPEN generation",
		env)
	ctx := context.Background()

	// #1 goes out while CLOSED and stalls.
	old := r.GoCall("old slow call (gen 0, closed)", ctx)
	r.WaitActive(1)
	r.ExpectState(breaker.StateClosed)
	r.ExpectInFlight(1)

	// #2..#4 fast failures trip the breaker while #1 is still in flight.
	r.ExpectResult("fail-1", r.Call("fast failure 1", ctx), appclient.ResultFailure)
	r.ExpectResult("fail-2", r.Call("fast failure 2", ctx), appclient.ResultFailure)
	r.ExpectResult("fail-3", r.Call("fast failure 3", ctx), appclient.ResultFailure)
	r.ExpectState(breaker.StateOpen)
	r.ExpectCounters(Counters{Calls: 4, Failures: 3, WindowFailures: 3})
	r.ExpectInFlight(1)
	genOpen := r.brk.Generation() // OPEN bumped to generation 1

	// Cool-down elapses: OPEN -> HALF_OPEN, a brand-new generation. The old
	// call is still stalled and now belongs to a dead generation.
	r.Advance("past open cooldown", 10*time.Second)
	r.ExpectState(breaker.StateHalfOpen)
	r.ExpectInFlight(1)
	r.ExpectProbePermitsFree(2)
	r.ExpectCounters(Counters{ProbeSuccesses: 0})

	// The old call now fails, late. State and new-generation probe counters
	// must not move; only lifetime totals observe the failure.
	r.Upstream().ReleaseStalled()
	r.ExpectResult("old call late failure", r.Await("old slow call completes (failure)", old), appclient.ResultFailure)
	r.ExpectState(breaker.StateHalfOpen)
	r.ExpectGeneration(genOpen + 1) // still the HALF_OPEN generation
	r.ExpectInFlight(0)
	r.ExpectProbePermitsFree(2)
	r.ExpectCounters(Counters{ProbeSuccesses: 0, Failures: 4})

	// The new generation still gets a clean recovery path.
	r.ExpectResult("probe-1", r.Call("probe success 1", ctx), appclient.ResultSuccess)
	r.ExpectCounters(Counters{ProbeSuccesses: 1})
	r.ExpectResult("probe-2", r.Call("probe success 2", ctx), appclient.ResultSuccess)
	r.ExpectState(breaker.StateClosed)

	return r.Report()
}

// ScenarioProbeContention: only MaxProbeCalls (2) probes may run concurrently
// in HALF_OPEN. A 3rd caller is rejected and never reaches the upstream; a
// freed slot admits a replacement probe; 2 consecutive successes close.
func ScenarioProbeContention() Report {
	env := Env{
		Config: defaultConfig(),
		Script: []upstream.Directive{
			{Fail: true}, {Fail: true}, {Fail: true}, // #1-3 trip
			{Stall: true}, {Stall: true}, // #4-5 the first two probes
			{Stall: true}, // #6 replacement probe after a slot frees
		},
		FallbackOK: true,
	}
	r := newRunner("probe_contention",
		"Half-open admits at most MaxProbeCalls concurrent probes; extras are rejected and freed slots are reused",
		env)
	ctx := context.Background()

	r.Call("failure 1", ctx)
	r.Call("failure 2", ctx)
	r.Call("failure 3", ctx)
	r.ExpectState(breaker.StateOpen)
	r.Advance("cooldown elapsed", 10*time.Second)
	r.ExpectState(breaker.StateHalfOpen)

	p1 := r.GoCall("probe 1 takes a slot", ctx)
	r.WaitActive(1)
	p2 := r.GoCall("probe 2 takes a slot", ctx)
	r.WaitActive(2)
	r.ExpectInFlight(2)
	r.ExpectProbePermitsFree(0)

	// Both slots busy: a third probe is rejected and must NOT hit upstream.
	r.ExpectResult("probe 3 rejected", r.Call("probe 3 while slots full", ctx), appclient.ResultRejected)
	r.ExpectCounters(Counters{Rejected: 1})
	r.ExpectInFlight(2)
	r.ExpectProbePermitsFree(0)
	r.ExpectUpstreamActive(2, "rejected probe must not reach the fake upstream")

	// Free exactly one slot: one probe succeeds (run = 1 of 2), its slot is
	// returned; a replacement probe is then admitted.
	r.Upstream().ReleaseOne()
	r.AwaitAny("first probe succeeds", p1, p2)
	r.ExpectCounters(Counters{ProbeSuccesses: 1})
	r.ExpectInFlight(1)
	r.ExpectProbePermitsFree(1)
	p3 := r.GoCall("replacement probe takes the freed slot", ctx)
	r.WaitActive(2)
	r.ExpectProbePermitsFree(0)
	r.ExpectResult("probe 4 rejected", r.Call("probe 4 while slots full again", ctx), appclient.ResultRejected)
	r.ExpectCounters(Counters{Rejected: 2})

	// Release the two remaining probes. The 2nd consecutive success closes
	// the breaker; the completion arriving after CLOSE is a stale-generation
	// no-op (in-flight still returns to zero).
	r.Upstream().ReleaseStalled()
	r.AwaitAny("second recovery success", p1, p2, p3)
	r.AwaitAny("post-close stale completion", p1, p2, p3)
	r.ExpectState(breaker.StateClosed)
	r.ExpectCounters(Counters{Successes: 3, Calls: 6, Rejected: 2})
	r.ExpectInFlight(0)
	// Exactly 6 calls (3 failures + 3 probes) ever reached the upstream;
	// the 2 rejected attempts never did.
	r.ExpectUpstreamFinished(6)

	return r.Report()
}

// ScenarioCancelIsNotFailure covers two requirements:
//   - caller cancellation and clock-driven deadlines are OutcomeCanceled,
//     never failures: they neither trip CLOSED nor fail a HALF_OPEN probe;
//   - a later half-open round starts clean and still recovers on successes.
func ScenarioCancelIsNotFailure() Report {
	env := Env{
		Config:      defaultConfig(),
		CallTimeout: 100 * time.Millisecond,
		Script: []upstream.Directive{
			{Stall: true},                            // #1 canceled by caller while CLOSED
			{Stall: true},                            // #2 hits the clock-driven deadline while CLOSED
			{Fail: true}, {Fail: true}, {Fail: true}, // #3-5 real failures trip
			{Stall: true}, // #6 half-open probe round 1: canceled
			{Fail: true},  // #7 half-open probe round 1: fails -> reopen
			{Stall: true}, // #8 half-open probe round 2: succeeds on release
		},
		FallbackOK: true,
	}
	r := newRunner("cancel_is_not_failure",
		"Cancels/deadlines never count as failures and cannot drive breaker transitions; recovery still works",
		env)
	ctx := context.Background()

	// Caller-driven cancel in CLOSED.
	cctx, cancel := context.WithCancel(ctx)
	h1 := r.GoCall("closed call to cancel", cctx)
	r.WaitActive(1)
	cancel()
	r.ExpectResult("canceled in closed", r.Await("canceled call returns", h1), appclient.ResultCanceled)
	r.ExpectState(breaker.StateClosed)
	r.ExpectCounters(Counters{Calls: 1, Canceled: 1, Failures: 0, WindowFailures: 0})
	r.ExpectInFlight(0)

	// Clock-driven deadline in CLOSED: advancing virtual time past 100ms
	// cancels the call — again not a failure.
	h2 := r.GoCall("closed call to time out", ctx)
	r.WaitActive(1)
	r.Advance("past the 100ms call timeout", 200*time.Millisecond)
	r.ExpectResult("deadline in closed", r.Await("deadline call returns", h2), appclient.ResultCanceled)
	r.ExpectState(breaker.StateClosed)
	r.ExpectCounters(Counters{Calls: 2, Canceled: 2, Failures: 0, WindowFailures: 0})

	// Real failures trip the breaker; the 2 canceled samples do not count.
	r.ExpectResult("f1", r.Call("failure 1", ctx), appclient.ResultFailure)
	r.ExpectResult("f2", r.Call("failure 2", ctx), appclient.ResultFailure)
	r.ExpectResult("f3", r.Call("failure 3", ctx), appclient.ResultFailure)
	r.ExpectState(breaker.StateOpen)
	r.ExpectCounters(Counters{Failures: 3, Canceled: 2})

	// Half-open round 1: a canceled probe frees its slot and is NOT a verdict.
	r.Advance("cooldown 1", 10*time.Second)
	r.ExpectState(breaker.StateHalfOpen)
	cctx2, cancel2 := context.WithCancel(ctx)
	hp := r.GoCall("probe to cancel", cctx2)
	r.WaitActive(1)
	cancel2()
	r.ExpectResult("probe canceled", r.Await("canceled probe returns", hp), appclient.ResultCanceled)
	r.ExpectState(breaker.StateHalfOpen)
	r.ExpectCounters(Counters{Canceled: 3, Failures: 3, ProbeSuccesses: 0})
	r.ExpectProbePermitsFree(2)

	// A genuinely failing probe is what reopens the breaker.
	r.ExpectResult("failing probe reopens", r.Call("failing probe", ctx), appclient.ResultFailure)
	r.ExpectState(breaker.StateOpen)
	genReopened := r.brk.Generation() // generation after reopen

	// Half-open round 2 starts clean and recovers with 2 successes.
	r.Advance("cooldown 2", 10*time.Second)
	r.ExpectState(breaker.StateHalfOpen)
	round2 := r.GoCall("round-2 stalled probe", ctx)
	r.WaitActive(1)
	r.ExpectInFlight(1)
	r.ExpectProbePermitsFree(1)
	r.Upstream().ReleaseStalled()
	r.ExpectResult("round-2 probe ok", r.Await("round-2 probe returns", round2), appclient.ResultSuccess)
	r.ExpectCounters(Counters{ProbeSuccesses: 1})
	r.ExpectProbePermitsFree(2)
	r.ExpectResult("probe success 2", r.Call("probe success 2", ctx), appclient.ResultSuccess)
	r.ExpectState(breaker.StateClosed)
	r.ExpectGeneration(genReopened + 2) // HALF_OPEN then CLOSED bumped twice

	return r.Report()
}

// All returns every built-in acceptance scenario.
func All() []func() Report {
	return []func() Report{
		ScenarioLateFailure,
		ScenarioProbeContention,
		ScenarioCancelIsNotFailure,
	}
}
