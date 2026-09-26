package scenario

import (
	"time"

	"cbhalfopen/internal/breaker"
	"cbhalfopen/internal/fakeupstream"
	"cbhalfopen/internal/faultclient"
)

// LateFailureFromOldGeneration reproduces: a request started in an old
// generation is still in flight while the breaker opens, probes and fully
// recovers. Its failure arrives afterwards and must be discarded — the new
// generation's state and counters must not move.
func LateFailureFromOldGeneration() *Report {
	h := NewHarness(standardConfig(), faultclient.Config{})
	r := h.newReport(
		"late_failure_from_old_generation",
		"An old-generation call that fails after the breaker recovered cannot change new-generation state",
	)

	// 1. A healthy call starts and hangs at the upstream (permit gen 0).
	old := h.startHung()
	h.record("old_call_in_flight", "call #1 parked at upstream in closed generation", nil)

	// 2. The upstream starts failing for everyone else. Five failures
	//    across >= MinRequests samples trip the breaker.
	h.Fake.SetMode(fakeupstream.ModeFail)
	for i := 0; i < 5; i++ {
		h.callNow("failing traffic")
	}
	snap := h.Breaker.Snapshot()
	h.record("breaker_tripped", "5 immediate failures arrived while the old call is still parked",
		[]Check{
			h.eq("state is open", snap.State, breaker.StateOpen),
			h.eq("generation advanced to 1", snap.Generation, 1),
			h.eq("failure counter is 5", snap.Counters.Failures, int64(5)),
		})

	// 3. Cooldown elapses (virtual). No half-open entry happens lazily
	//    until the next Allow.
	h.VC.Advance(5 * time.Second)
	snap = h.Breaker.Snapshot()
	h.record("cooldown_elapsed", "advanced 5 virtual seconds; state stays open until the next Allow",
		[]Check{h.eq("still open before Allow", snap.State, breaker.StateOpen)})

	// 4. Two successful probes satisfy RequiredSuccesses and close the
	//    breaker (closed is a new generation, window reset).
	h.Fake.SetMode(fakeupstream.ModeOK)
	p1 := h.callNow("probe 1")
	snap = h.Breaker.Snapshot()
	h.record("probe_1_success", "first probe after cooldown succeeds",
		[]Check{
			h.eq("probe outcome", p1.Outcome, breaker.OutcomeSuccess),
			h.eq("state half_open", snap.State, breaker.StateHalfOpen),
			h.eq("consecutive successes", snap.ConsecutiveSuccesses, 1),
			h.eq("probe generation 2", p1.Generation, 2),
		})

	p2 := h.callNow("probe 2")
	snap = h.Breaker.Snapshot()
	h.record("probe_2_success_recovers", "second probe succeeds -> breaker closes",
		[]Check{
			h.eq("probe outcome", p2.Outcome, breaker.OutcomeSuccess),
			h.eq("state closed", snap.State, breaker.StateClosed),
			h.eq("generation advanced to 3", snap.Generation, 3),
			h.eq("window was reset", snap.WindowSamples, 0),
		})

	failuresBeforeLate := snap.Counters.Failures

	// 5. The ORIGINAL call (gen 0) finally fails, three generations late.
	h.Fake.Release(fakeupstream.ModeFail)
	late := old.result()
	snap = h.Breaker.Snapshot()
	h.record("late_old_failure_arrives", "the parked gen-0 call is released as a failure after recovery",
		[]Check{
			h.eq("client saw a service failure", late.Outcome, breaker.OutcomeFailure),
			h.eq("late result permit generation", late.Generation, 0),
			h.eq("state stays closed", snap.State, breaker.StateClosed),
			h.eq("generation unchanged", snap.Generation, 3),
			h.eq("failure counter did not move", snap.Counters.Failures, failuresBeforeLate),
			h.eq("window still empty", snap.WindowSamples, 0),
			h.eq("stale result counted once", snap.Counters.StaleResults, int64(1)),
		})

	return r.finalizeFrom(h)
}

// ProbeCompetition reproduces: only HalfOpenMaxProbes calls may probe at once,
// extra callers fail fast with ErrProbesExhausted, a finished probe frees its
// slot for reuse, and probes still in flight when the breaker closes become
// stale and cannot affect the new generation.
func ProbeCompetition() *Report {
	cfg := standardConfig()
	cfg.RequiredSuccesses = 3 // keep half-open alive across two probe successes
	h := NewHarness(cfg, faultclient.Config{})
	r := h.newReport(
		"half_open_probe_competition",
		"Only 3 probes may be in flight; the 4th is rejected; freed slots are reusable; in-flight probes from a closed generation go stale",
	)

	// Trip the breaker.
	h.Fake.SetMode(fakeupstream.ModeFail)
	for i := 0; i < 5; i++ {
		h.callNow("failing traffic")
	}
	h.VC.Advance(5 * time.Second)
	h.Fake.SetMode(fakeupstream.ModeHang)

	// First Allow after cooldown enters half-open; three probes in total
	// occupy the three slots. Each probe is fully registered before the
	// next starts, fixing their fake IDs to launch order.
	var probes []*hungCall
	for i := 1; i <= 3; i++ {
		permit, err := h.Breaker.Allow()
		if err != nil {
			panic(err)
		}
		probes = append(probes, h.startHungProbe(permit, i))
	}
	snap := h.Breaker.Snapshot()
	h.record("three_probes_occupy_slots", "3 concurrent probes parked at the upstream",
		[]Check{
			h.eq("state half_open", snap.State, breaker.StateHalfOpen),
			h.eq("probes in flight", snap.ProbesInFlight, 3),
			h.eq("probes granted", snap.Counters.ProbesGranted, int64(3)),
		})

	// The 4th competitor must be rejected, not queued.
	_, err := h.Breaker.Allow()
	snap = h.Breaker.Snapshot()
	h.record("fourth_competitor_rejected", "no slot available -> fast reject",
		[]Check{
			h.eq("rejection error", err, breaker.ErrProbesExhausted),
			h.eq("probes in flight stays 3", snap.ProbesInFlight, 3),
			h.eq("probes rejected counter", snap.Counters.ProbesRejected, int64(1)),
		})

	// Probe 1 succeeds and frees a slot; the competitor retries and wins it.
	h.Fake.Release(fakeupstream.ModeOK)
	p1res := probes[0].result()
	permit, err := h.Breaker.Allow()
	snap = h.Breaker.Snapshot()
	h.record("freed_slot_reused", "after one probe succeeds the rejected competitor is granted a slot",
		[]Check{
			h.eq("probe 1 success", p1res.Outcome, breaker.OutcomeSuccess),
			h.eq("retry allowed", err == nil, true),
			h.eq("consecutive successes", snap.ConsecutiveSuccesses, 1),
		})
	probes = append(probes, h.startHungProbe(permit, 3)) // P2, P3 and the replacement parked
	snap = h.Breaker.Snapshot()
	h.record("slots_full_again", "the reused slot is occupied",
		[]Check{
			h.eq("probes in flight back to 3", snap.ProbesInFlight, 3),
			h.eq("probes granted total", snap.Counters.ProbesGranted, int64(4)),
		})

	// Probes 2 and 3 succeed -> required successes reached -> closed. The
	// replacement probe is still in flight and is now old-generation.
	h.Fake.Release(fakeupstream.ModeOK) // P2
	r2 := probes[1].result()
	h.Fake.Release(fakeupstream.ModeOK) // P3
	r3 := probes[2].result()
	snap = h.Breaker.Snapshot()
	h.record("probes_recover_breaker", "3 consecutive probe successes close the breaker while a 4th probe is still in flight",
		[]Check{
			h.eq("p2 success", r2.Outcome, breaker.OutcomeSuccess),
			h.eq("p3 success", r3.Outcome, breaker.OutcomeSuccess),
			h.eq("state closed", snap.State, breaker.StateClosed),
			h.eq("generation advanced to 3", snap.Generation, 3),
		})

	// The leftover probe completes successfully in the old half-open
	// generation: it must be discarded.
	h.Fake.Release(fakeupstream.ModeOK)
	stale := probes[3].result()
	snap = h.Breaker.Snapshot()
	h.record("leftover_probe_goes_stale", "the in-flight 4th probe completes after close and is stale",
		[]Check{
			h.eq("leftover permit generation", stale.Generation, 2),
			h.eq("state stays closed", snap.State, breaker.StateClosed),
			h.eq("stale results", snap.Counters.StaleResults, int64(1)),
			h.eq("success counter only counts 3 probes", snap.Counters.Successes, int64(3)),
		})

	return r.finalizeFrom(h)
}

// Recovery reproduces the full lifecycle: closed -> open (calls rejected) ->
// half-open (bounded probes) -> closed, with the sample window reset and
// healthy traffic flowing afterwards.
func Recovery() *Report {
	h := NewHarness(standardConfig(), faultclient.Config{})
	r := h.newReport(
		"full_recovery_lifecycle",
		"Failures open the breaker, cooldown rejects calls, two good probes close it, healthy traffic resumes",
	)

	h.Fake.SetMode(fakeupstream.ModeFail)
	for i := 0; i < 5; i++ {
		h.callNow("failing traffic")
	}
	snap := h.Breaker.Snapshot()
	h.record("tripped", "5 failures in the sliding window",
		[]Check{
			h.eq("state open", snap.State, breaker.StateOpen),
			h.eq("failures", snap.Counters.Failures, int64(5)),
		})

	// Calls while open are rejected outright.
	var rejects int
	for i := 0; i < 3; i++ {
		if _, err := h.Breaker.Allow(); err != nil {
			rejects++
		}
	}
	h.VC.Advance(4 * time.Second)
	if _, err := h.Breaker.Allow(); err != nil {
		rejects++
	}
	snap = h.Breaker.Snapshot()
	h.record("open_rejects_traffic", "3 immediate rejects plus 1 more at t=4s (cooldown is 5s)",
		[]Check{
			h.eq("rejected 4 calls", rejects, 4),
			h.eq("still open at 4s", snap.State, breaker.StateOpen),
			h.eq("rejected counter", snap.Counters.Rejected, int64(4)),
		})

	// Final cooldown second; probes now go through.
	h.VC.Advance(1 * time.Second)
	h.Fake.SetMode(fakeupstream.ModeOK)
	pr1 := h.callNow("probe 1")
	snap = h.Breaker.Snapshot()
	h.record("first_probe", "at t=5s the first call enters half-open as a probe",
		[]Check{
			h.eq("probe marked", pr1.Probe, true),
			h.eq("state half_open", snap.State, breaker.StateHalfOpen),
			h.eq("one probe in flight accounting settled", snap.ProbesInFlight, 0),
		})
	pr2 := h.callNow("probe 2")
	snap = h.Breaker.Snapshot()
	h.record("second_probe_closes", "second consecutive success closes the breaker",
		[]Check{
			h.eq("probe 2 success", pr2.Outcome, breaker.OutcomeSuccess),
			h.eq("state closed", snap.State, breaker.StateClosed),
			h.eq("generation 3", snap.Generation, 3),
		})

	// Healthy traffic after recovery fills a fresh window.
	for i := 0; i < 3; i++ {
		h.callNow("healthy traffic")
	}
	snap = h.Breaker.Snapshot()
	h.record("healthy_traffic_resumes", "new sliding window after recovery",
		[]Check{
			h.eq("state closed", snap.State, breaker.StateClosed),
			h.eq("window samples", snap.WindowSamples, 3),
			h.eq("window failures", snap.WindowFailures, 0),
			h.eq("failure ratio", snap.FailureRatio, 0.0),
			h.eq("successes total", snap.Counters.Successes, int64(5)),
			h.eq("no stale results", snap.Counters.StaleResults, int64(0)),
		})

	return r.finalizeFrom(h)
}

// CanceledNotFailure reproduces: a caller canceling a parked request must
// release the breaker permit (and a half-open probe slot) without being
// recorded as a failure, in either closed or half-open state. A failure that
// the upstream produces afterwards must not retroactively count.
func CanceledNotFailure() *Report {
	h := NewHarness(standardConfig(), faultclient.Config{})
	r := h.newReport(
		"canceled_is_not_failure",
		"Caller cancellation releases permits and probe slots but never counts as a service failure",
	)

	// --- Case A: cancellation in closed state ---------------------------
	canceled := h.startHung()
	canceled.cancel()
	res := canceled.result()
	snap := h.Breaker.Snapshot()
	h.record("closed_call_canceled", "caller gives up on a parked healthy-state call",
		[]Check{
			h.eq("outcome canceled", res.Outcome, breaker.OutcomeCanceled),
			h.eq("canceled counter", snap.Counters.Canceled, int64(1)),
			h.eq("failures counter", snap.Counters.Failures, int64(0)),
			h.eq("not recorded in window", snap.WindowSamples, 0),
			h.eq("state stays closed", snap.State, breaker.StateClosed),
		})
	// The upstream eventually fails the abandoned call: nothing may move.
	h.Fake.Release(fakeupstream.ModeFail)
	snap = h.Breaker.Snapshot()
	h.record("late_answer_after_cancel_ignored", "upstream fails the already-canceled call",
		[]Check{
			h.eq("failures still zero", snap.Counters.Failures, int64(0)),
			h.eq("state closed", snap.State, breaker.StateClosed),
			h.eq("no stale marker (permit already settled)", snap.Counters.StaleResults, int64(0)),
		})

	// --- Case B: cancellation of a half-open probe frees its slot -------
	h.Fake.SetMode(fakeupstream.ModeFail)
	for i := 0; i < 5; i++ {
		h.callNow("failing traffic")
	}
	h.VC.Advance(5 * time.Second)
	h.Fake.SetMode(fakeupstream.ModeHang)

	permit, err := h.Breaker.Allow() // enters half-open, occupies slot
	if err != nil {
		panic(err)
	}
	probe := h.startHungProbe(permit, 1)
	snap = h.Breaker.Snapshot()
	h.record("probe_in_flight", "half-open probe parked at upstream",
		[]Check{
			h.eq("state half_open", snap.State, breaker.StateHalfOpen),
			h.eq("probe slot occupied", snap.ProbesInFlight, 1),
		})

	probe.cancel()
	pres := probe.result()
	snap = h.Breaker.Snapshot()
	h.record("probe_canceled", "caller cancels the probe: slot released, success streak untouched",
		[]Check{
			h.eq("probe outcome canceled", pres.Outcome, breaker.OutcomeCanceled),
			h.eq("probe slot released", snap.ProbesInFlight, 0),
			h.eq("still half_open", snap.State, breaker.StateHalfOpen),
			h.eq("no consecutive success change", snap.ConsecutiveSuccesses, 0),
			h.eq("canceled counter", snap.Counters.Canceled, int64(2)),
			h.eq("failures still only tripping ones", snap.Counters.Failures, int64(5)),
		})

	// The freed slot accepts a fresh probe; two successes recover.
	h.Fake.SetMode(fakeupstream.ModeOK)
	ok1 := h.callNow("replacement probe 1")
	ok2 := h.callNow("replacement probe 2")
	snap = h.Breaker.Snapshot()
	h.record("fresh_probes_recover", "replacement probes use the freed slot and close the breaker",
		[]Check{
			h.eq("replacement probe 1", ok1.Outcome, breaker.OutcomeSuccess),
			h.eq("replacement probe 2", ok2.Outcome, breaker.OutcomeSuccess),
			h.eq("state closed", snap.State, breaker.StateClosed),
			h.eq("failures unchanged", snap.Counters.Failures, int64(5)),
			h.eq("canceled total", snap.Counters.Canceled, int64(2)),
		})

	// Drain the abandoned probe call (parked since its cancellation) so no
	// goroutine leaks from the scenario; its late answer changes nothing.
	h.Fake.Release(fakeupstream.ModeFail)

	return r.finalizeFrom(h)
}

// Descriptor names one acceptance scenario without running it.
type Descriptor struct {
	Name string
	Run  func() *Report
}

// All returns every acceptance scenario in display order.
func All() []Descriptor {
	return []Descriptor{
		{Name: "late_failure_from_old_generation", Run: LateFailureFromOldGeneration},
		{Name: "half_open_probe_competition", Run: ProbeCompetition},
		{Name: "full_recovery_lifecycle", Run: Recovery},
		{Name: "canceled_is_not_failure", Run: CanceledNotFailure},
	}
}
