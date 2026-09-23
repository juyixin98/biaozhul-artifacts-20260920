package queue_test

import (
	"errors"
	"testing"
	"time"

	"agingqueue/queue"
)

// submitBlock registers the release channel first (so the job can block the
// worker the instant it starts) and then submits a "block" job.
func (h *testHarness) submitBlock(t *testing.T, id string, prio int, delay ...time.Duration) chan struct{} {
	t.Helper()
	j := queue.Submit{Type: "block", Priority: prio, ID: id}
	if len(delay) > 0 {
		j.Delay = queue.Duration(delay[0])
	}
	rel := make(chan struct{})
	h.exec.setRelease(id, rel)
	h.mustSubmit(t, j)
	return rel
}

func TestSubmitAndRun(t *testing.T) {
	h := newHarness(t, queue.Config{
		MinPriority: 0, MaxPriority: 9, AgeInterval: time.Second,
	})
	rel := h.submitBlock(t, "j1", 3)
	h.waitStatus(t, "j1", queue.Running)

	close(rel)
	h.waitStatus(t, "j1", queue.Succeeded)

	j := h.get(t, "j1")
	if j.Attempts != 1 {
		t.Fatalf("attempts = %d, want 1", j.Attempts)
	}
	if j.EffectivePrio != 3 {
		t.Fatalf("effective priority = %d, want 3", j.EffectivePrio)
	}
}

func TestHigherPriorityDispatchesFirst(t *testing.T) {
	h := newHarness(t, queue.Config{Concurrency: 1, AgeInterval: time.Hour})

	// Submit three jobs while the worker is blocked by an anchor job.
	anchor := h.submitBlock(t, "anchor", 0)
	h.waitStatus(t, "anchor", queue.Running)

	_ = h.submitBlock(t, "low", 1)
	_ = h.submitBlock(t, "high", 8)
	_ = h.submitBlock(t, "mid", 5)
	h.advance(time.Millisecond)

	// Sanity: nothing else started yet (single worker).
	if got := h.exec.startedList(); len(got) != 1 || got[0] != "anchor" {
		t.Fatalf("started = %v, want only anchor", got)
	}

	close(anchor)
	h.waitStatus(t, "anchor", queue.Succeeded)
	h.waitStatus(t, "high", queue.Running)
	close(h.exec.release("high"))
	h.waitStatus(t, "high", queue.Succeeded)
	h.waitStatus(t, "mid", queue.Running)
	close(h.exec.release("mid"))
	h.waitStatus(t, "mid", queue.Succeeded)
	h.waitStatus(t, "low", queue.Running)
	close(h.exec.release("low"))
	h.waitStatus(t, "low", queue.Succeeded)

	wantOrder := []string{"anchor", "high", "mid", "low"}
	if got := h.exec.startedList(); !equalStrings(got, wantOrder) {
		t.Fatalf("start order = %v, want %v", got, wantOrder)
	}
}

func TestFIFOWithinSamePriority(t *testing.T) {
	h := newHarness(t, queue.Config{Concurrency: 1, AgeInterval: time.Hour})
	anchor := h.submitBlock(t, "anchor", 5)
	h.waitStatus(t, "anchor", queue.Running)

	ids := []string{"a", "b", "c", "d"}
	for _, id := range ids {
		_ = h.submitBlock(t, id, 5)
	}
	h.advance(time.Millisecond)
	close(anchor)

	want := append([]string{"anchor"}, ids...)
	for _, id := range ids {
		h.waitStatus(t, id, queue.Running)
		close(h.exec.release(id))
		h.waitStatus(t, id, queue.Succeeded)
	}
	if started := h.exec.startedList(); !equalStrings(started, want) {
		t.Fatalf("FIFO start order = %v, want %v", started, want)
	}
}

func TestAgingPromotesLowPriority(t *testing.T) {
	// AgeInterval 1s, priorities 0..9. A priority-0 job reaches effective
	// priority 9 after 9s.
	h := newHarness(t, queue.Config{Concurrency: 1, AgeInterval: time.Second})

	anchor := h.submitBlock(t, "anchor", 9)
	h.waitStatus(t, "anchor", queue.Running)

	lowRel := h.submitBlock(t, "low", 0)
	// Keep injecting fresh HIGH priority jobs: the classic starvation setup.
	for _, id := range []string{"h1", "h2", "h3", "h4"} {
		_ = h.submitBlock(t, id, 9)
	}

	// Before aging crosses a boundary, low is still at 0.
	h.advance(500 * time.Millisecond)
	if got := h.get(t, "low").EffectivePrio; got != 0 {
		t.Fatalf("early effective = %d, want 0", got)
	}

	// Clock JUMP across many intervals: low cascades to effective 9.
	h.advance(9 * time.Second)
	if got := h.get(t, "low").EffectivePrio; got != 9 {
		t.Fatalf("aged effective = %d, want 9", got)
	}
	// Exactly 5 jobs queued (low + 4 highs) regardless of how many buckets
	// they span: no duplicate bucket entries from promotion.
	if st := h.s.Stats(); st.Runnable != 5 {
		t.Fatalf("runnable = %d, want exactly 5 (bucket duplicates?)", st.Runnable)
	}
	promos := countEvents(h.sink, queue.EventPromoted, "low")
	if promos < 9 {
		t.Fatalf("promoted events = %d, want >= 9", promos)
	}

	// Release the anchor; now low and the high jobs are tied at effective
	// 9, but low has the oldest birth sequence -> low goes FIRST, even
	// though fresh high-priority jobs were continuously present.
	close(anchor)
	h.waitStatus(t, "low", queue.Running)
	close(lowRel)
	h.waitStatus(t, "low", queue.Succeeded)

	started := h.exec.startedList()
	if started[0] != "anchor" || started[1] != "low" {
		t.Fatalf("aged low job should run first after anchor, got %v", started)
	}

	// Drain the high-priority jobs and verify they all still run.
	for _, id := range []string{"h1", "h2", "h3", "h4"} {
		h.waitStatus(t, id, queue.Running)
		close(h.exec.release(id))
		h.waitStatus(t, id, queue.Succeeded)
	}

	// Regression: aging moves a job between buckets; it must never leave a
	// stale duplicate in an old bucket, which would re-dispatch an already
	// finished job. Each job has exactly one Started event.
	for _, id := range []string{"anchor", "low", "h1", "h2", "h3", "h4"} {
		if n := countEvents(h.sink, queue.EventStarted, id); n != 1 {
			t.Fatalf("job %s started %d times, want exactly 1 (stale bucket duplicate)", id, n)
		}
		if n := countEvents(h.sink, queue.EventSucceeded, id); n != 1 {
			t.Fatalf("job %s succeeded %d times, want exactly 1", id, n)
		}
	}
}

func TestLowPriorityStarvesUntilAgingCondition(t *testing.T) {
	// Fresh high-priority jobs keep arriving. A low job with MaxPriority
	// capped below the high tier can never catch up and must wait.
	h := newHarness(t, queue.Config{Concurrency: 1, AgeInterval: time.Second})

	anchor := h.submitBlock(t, "anchor", 5)
	h.waitStatus(t, "anchor", queue.Running)

	// Low capped at 3, high jobs are priority 5: never catches up.
	j := queue.Submit{Type: "block", Priority: 0, MaxPriority: 3, ID: "capped"}
	h.exec.setRelease("capped", make(chan struct{}))
	h.mustSubmit(t, j)
	_ = h.submitBlock(t, "h1", 5)

	h.advance(60 * time.Second) // well past any aging boundary
	if got := h.get(t, "capped").EffectivePrio; got != 3 {
		t.Fatalf("capped effective = %d, want 3", got)
	}

	// Release anchor: high job runs, capped stays queued.
	close(anchor)
	h.waitStatus(t, "h1", queue.Running)
	if got := h.get(t, "capped").Status; got != queue.Queued {
		t.Fatalf("capped status = %s, want queued (starved by higher tier)", got)
	}
	close(h.exec.release("h1"))
	h.waitStatus(t, "h1", queue.Succeeded)

	// With no higher-tier competition left, the capped job runs.
	h.waitStatus(t, "capped", queue.Running)
	close(h.exec.release("capped"))
	h.waitStatus(t, "capped", queue.Succeeded)
}

func TestRetryDoesNotResetAge(t *testing.T) {
	// Core acceptance rule: a failed job's age and birth order survive a
	// retry. Retry must neither reset EnqueuedAt (which would let it jump
	// the queue by re-aging) nor drop accumulated waiting time. We use a
	// "younger" job submitted LATER at the SAME effective priority to prove
	// the retrying job keeps its earlier birth order (FIFO tie-break).
	h := newHarness(t, queue.Config{
		Concurrency: 1, AgeInterval: time.Second,
		Backoff:            queue.ConstantBackoff(500 * time.Millisecond),
		DefaultMaxAttempts: 3,
	})

	// anchor holds the single worker while the test jobs accumulate age.
	anchor := h.submitBlock(t, "anchor", 2)
	h.waitStatus(t, "anchor", queue.Running)

	// "old" will fail its first two attempts (fast), then succeed on #3.
	h.exec.setExtra("failx", failNTimes{n: 2})
	h.mustSubmit(t, queue.Submit{Type: "failx", Priority: 0, ID: "old", MaxAttempts: 3})
	enqueuedAt := h.get(t, "old").EnqueuedAt

	// old ages to level 2 while queued behind anchor.
	h.advance(2 * time.Second)
	if got := h.get(t, "old").EffectivePrio; got != 2 {
		t.Fatalf("aged effective = %d, want 2", got)
	}

	// A sibling submitted LATER and at a LOWER tier: it can never overtake
	// old, which aged to effective 2. Capped at 1 so aging never closes the
	// gap. It fails instantly every attempt and never blocks the worker.
	h.exec.setExtra("youngfail", alwaysFail{})
	h.mustSubmit(t, queue.Submit{
		Type: "youngfail", Priority: 0, MaxPriority: 1,
		ID: "young", MaxAttempts: 100,
	})

	// Free anchor. old (effective 2) is the highest queued job and is picked
	// before the capped young job. old fails instantly twice with a 500ms
	// backoff between attempts and succeeds on attempt 3.
	close(anchor)
	h.waitEventCount(t, "old", queue.EventStarted, 1)
	h.advance(600 * time.Millisecond) // past backoff #1
	h.waitEventCount(t, "old", queue.EventStarted, 2)
	h.advance(600 * time.Millisecond) // past backoff #2
	h.waitEventCount(t, "old", queue.EventStarted, 3)
	h.waitStatus(t, "old", queue.Succeeded)
	if j := h.get(t, "old"); j.Attempts != 3 {
		t.Fatalf("old attempts = %d, want 3", j.Attempts)
	}

	// Every retry event must carry the RETAINED age and effective priority;
	// in particular the first retry (right after a 2s wait) reports age 2s.
	// The FIRST retry event is emitted the instant attempt 1 fails, before
	// any backoff elapses, so its retained age is exactly the pre-execution
	// 2s. Later retry events legitimately show MORE age (aging continues
	// through backoff), so assert on the first one specifically.
	var firstRetry map[string]any
	for _, e := range h.sink.Events() {
		if e.Type == queue.EventRetrying && e.JobID == "old" {
			firstRetry = e.Detail
			break
		}
	}
	if age, _ := firstRetry["age"].(string); age != "2s" {
		t.Fatalf("first retry age = %q, want retained 2s", age)
	}
	if ep, _ := detailInt(firstRetry, "effectivePriority"); ep != 2 {
		t.Fatalf("first retry effectivePriority = %d, want 2 (retained)", ep)
	}
	// Retries never reset birth time.
	if got := h.get(t, "old").EnqueuedAt; !got.Equal(enqueuedAt) {
		t.Fatalf("EnqueuedAt changed across retry: %v -> %v", enqueuedAt, got)
	}

	// old (older, higher effective priority) must have started its first
	// attempt before young ever did.
	started := h.exec.startedList()
	iOld, iYoung := indexOf(started, "old"), indexOf(started, "young")
	if iOld < 0 || (iYoung >= 0 && iOld > iYoung) {
		t.Fatalf("older retrying job must be dispatched first; order=%v", started)
	}

	// young retries forever; cancel it to finish the test.
	_ = h.s.Cancel("young")
	h.waitStatus(t, "young", queue.Canceled)
}

func TestRetryBackoffAndEventOrder(t *testing.T) {
	h := newHarness(t, queue.Config{
		Concurrency:        1,
		Backoff:            queue.ConstantBackoff(100 * time.Millisecond),
		DefaultMaxAttempts: 2,
	})
	h.exec.setExtra("boom", alwaysFail{})
	id := h.mustSubmit(t, queue.Submit{Type: "boom", Priority: 1, ID: "boom", MaxAttempts: 2})

	h.waitEventCount(t, id, queue.EventRetrying, 1) // attempt 1 failed, in backoff
	h.advance(100 * time.Millisecond)
	h.waitStatus(t, id, queue.Failed) // attempts exhausted on attempt 2

	types := h.eventTypes(id)
	want := []queue.EventType{
		queue.EventSubmitted,
		queue.EventStarted,
		queue.EventRetrying,
		queue.EventStarted,
		queue.EventFailed,
	}
	if !equalEventTypes(types, want) {
		t.Fatalf("events = %v, want %v", types, want)
	}
}

func TestFatalErrorSkipsRetry(t *testing.T) {
	h := newHarness(t, queue.Config{DefaultMaxAttempts: 5})
	h.exec.extra["fatal"] = fatalExec{}
	id := h.mustSubmit(t, queue.Submit{Type: "fatal", Priority: 1, ID: "f", MaxAttempts: 5})
	h.waitStatus(t, id, queue.Failed)
	j := h.get(t, id)
	if j.Attempts != 1 {
		t.Fatalf("attempts = %d, fatal job must not retry", j.Attempts)
	}
	fail := lastDetail(h.sink, queue.EventFailed, id)
	if fail["fatal"] != true {
		t.Fatalf("failed detail fatal = %v, want true", fail["fatal"])
	}
}

func TestDuplicateCancelAndUnknownCancel(t *testing.T) {
	h := newHarness(t, queue.Config{})

	// Unknown id: rejected event.
	if err := h.s.Cancel("ghost"); err != queue.ErrNotFound {
		t.Fatalf("ghost cancel err = %v, want ErrNotFound", err)
	}
	rejected := countEvents(h.sink, queue.EventCancelRejected, "ghost")
	if rejected != 1 {
		t.Fatalf("ghost rejected events = %d, want 1", rejected)
	}

	_ = h.submitBlock(t, "c", 4)
	h.waitStatus(t, "c", queue.Running)

	if err := h.s.Cancel("c"); err != nil {
		t.Fatalf("first cancel: %v", err)
	}
	h.waitStatus(t, "c", queue.Canceled)
	if err := h.s.Cancel("c"); !errors.Is(err, queue.ErrTerminal) {
		t.Fatalf("second cancel err = %v, want ErrTerminal", err)
	}
	if err := h.s.Cancel("c"); !errors.Is(err, queue.ErrTerminal) {
		t.Fatalf("third cancel err = %v, want ErrTerminal", err)
	}

	if n := countEvents(h.sink, queue.EventCanceled, "c"); n != 1 {
		t.Fatalf("canceled events = %d, want exactly 1", n)
	}
	if n := countEvents(h.sink, queue.EventCancelRejected, "c"); n != 2 {
		t.Fatalf("cancel_rejected events = %d, want 2", n)
	}
}

func TestCancelQueuedBeforeStart(t *testing.T) {
	h := newHarness(t, queue.Config{Concurrency: 1})
	anchor := h.submitBlock(t, "anchor", 0)
	h.waitStatus(t, "anchor", queue.Running)
	victim := h.submitBlock(t, "victim", 0)
	_ = victim

	if err := h.s.Cancel("victim"); err != nil {
		t.Fatalf("cancel queued: %v", err)
	}
	h.waitStatus(t, "victim", queue.Canceled)
	close(anchor)
	h.waitStatus(t, "anchor", queue.Succeeded)

	// victim must never have started.
	for _, id := range h.exec.startedList() {
		if id == "victim" {
			t.Fatal("canceled queued job started executing")
		}
	}
}

func TestDelayedJob(t *testing.T) {
	h := newHarness(t, queue.Config{})
	late := h.submitBlock(t, "late", 1, 2*time.Second)
	h.advance(time.Second)
	if st := h.s.Stats(); st.Runnable != 0 || st.Delayed != 1 {
		t.Fatalf("stats during delay = %+v, want 1 delayed", st)
	}
	if got := h.get(t, "late").Status; got != queue.Queued {
		t.Fatalf("status during delay = %s", got)
	}
	h.advance(time.Second + time.Millisecond)
	h.waitStatus(t, "late", queue.Running)
	close(late)
	h.waitStatus(t, "late", queue.Succeeded)
}

func TestConcurrencyTwo(t *testing.T) {
	h := newHarness(t, queue.Config{Concurrency: 2})
	a := h.submitBlock(t, "a", 1)
	b := h.submitBlock(t, "b", 1)
	h.waitStatus(t, "a", queue.Running)
	h.waitStatus(t, "b", queue.Running)
	close(a)
	close(b)
	h.waitStatus(t, "a", queue.Succeeded)
	h.waitStatus(t, "b", queue.Succeeded)
}

func TestValidation(t *testing.T) {
	h := newHarness(t, queue.Config{MinPriority: 0, MaxPriority: 5})
	if _, err := h.s.Submit(queue.Submit{Type: "x", Priority: 9}); err == nil {
		t.Fatal("out-of-range priority should fail")
	}
	if _, err := h.s.Submit(queue.Submit{Priority: 1}); err != queue.ErrNoType {
		t.Fatalf("missing type err = %v, want ErrNoType", err)
	}
}

// ----------------------------------------------------------------- helpers

// detailInt reads an integer from an event Detail map, tolerating the
// numeric types Go/JSON produce (int in-memory, float64 after unmarshal).
func detailInt(d map[string]any, key string) (int, bool) {
	switch v := d[key].(type) {
	case int:
		return v, true
	case int64:
		return int(v), true
	case float64:
		return int(v), true
	default:
		return 0, false
	}
}

func countEvents(sink *queue.MemorySink, typ queue.EventType, jobID string) int {
	n := 0
	for _, e := range sink.Events() {
		if e.Type == typ && (jobID == "" || e.JobID == jobID) {
			n++
		}
	}
	return n
}

func lastDetail(sink *queue.MemorySink, typ queue.EventType, jobID string) map[string]any {
	var d map[string]any
	for _, e := range sink.Events() {
		if e.Type == typ && e.JobID == jobID {
			d = e.Detail
		}
	}
	if d == nil {
		return map[string]any{}
	}
	return d
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func equalEventTypes(a, b []queue.EventType) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func indexOf(xs []string, x string) int {
	for i, v := range xs {
		if v == x {
			return i
		}
	}
	return -1
}

func lastIndexOf(xs []string, x string) int {
	idx := -1
	for i, v := range xs {
		if v == x {
			idx = i
		}
	}
	return idx
}
