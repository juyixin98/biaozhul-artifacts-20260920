// Package e2e contains the three-tier fault-fixture acceptance tests:
// total attempts across client/edge/mid/leaf are bounded by the single root
// retry budget, Retry-After is honored, cancellation unwinds all layers, and
// budget exhaustion / deadline outcomes are reported structurally.
package e2e

import (
	"testing"
	"time"

	"retrybudget/internal/fault"
	"retrybudget/internal/harness"
	"retrybudget/internal/retry"
)

func testRetry() retry.Config {
	return retry.Config{
		BaseDelay:  5 * time.Millisecond,
		MaxDelay:   50 * time.Millisecond,
		Multiplier: 2,
		Jitter:     0, // deterministic fixtures
	}
}

// sumIsRootAndBounded is the central anti-amplification invariant: every
// reservation at every layer is one real HTTP attempt, they all draw from the
// one root axis, and the axis never exceeds the root cap.
func assertAttemptAccounting(t *testing.T, o harness.Outcome) {
	t.Helper()
	sum := o.Tiers.ClientAttempts + o.Tiers.EdgeAttempts + o.Tiers.MidAttempts + o.Tiers.LeafHits
	if sum != o.TotalUsed {
		t.Fatalf("[%s] per-tier attempts sum=%d (c=%d e=%d m=%d leaf=%d) but root used=%d",
			o.Name, sum, o.Tiers.ClientAttempts, o.Tiers.EdgeAttempts, o.Tiers.MidAttempts,
			o.Tiers.LeafHits, o.TotalUsed)
	}
	if o.TotalUsed > o.RootMax {
		t.Fatalf("[%s] total used %d exceeds root budget %d", o.Name, o.TotalUsed, o.RootMax)
	}
}

// TestRootBudgetCapsThreeTierAmplification: the leaf never recovers and every
// layer is willing to retry on its own. The root budget still puts a hard,
// exact ceiling on total attempts across the whole stack.
func TestRootBudgetCapsThreeTierAmplification(t *testing.T) {
	for _, max := range []int{4, 8, 12} {
		o := harness.Run(harness.Scenario{
			Name:        "budget-cap-" + itoa(max),
			Description: "always-503 leaf; all layers retry; root cap must bound total attempts",
			Script:      []fault.Outcome{fault.Unavailable(0)},
			RootMax:     max,
			Retry:       testRetry(),
			EdgeLocal:   3,
			MidLocal:    3,
			ClientLocal: 3,
		})
		assertAttemptAccounting(t, o)
		if o.Status != "budget-exhausted" {
			t.Fatalf("status=%s, want budget-exhausted", o.Status)
		}
		if o.TotalUsed != max {
			t.Fatalf("totalUsed=%d, want exactly %d", o.TotalUsed, max)
		}
		// Every layer had to participate (no single tier dominates by
		// bypassing the others).
		if o.Tiers.ClientAttempts == 0 || o.Tiers.EdgeAttempts == 0 ||
			o.Tiers.MidAttempts == 0 || o.Tiers.LeafHits == 0 {
			t.Fatalf("expected every layer to attempt, got %+v", o.Tiers)
		}
		t.Logf("cap=%d -> client=%d edge=%d mid=%d leaf=%d (total=%d)",
			max, o.Tiers.ClientAttempts, o.Tiers.EdgeAttempts, o.Tiers.MidAttempts,
			o.Tiers.LeafHits, o.TotalUsed)
	}
}

// TestLocalCapsTriggerUpstreamRetries: the mid tier burns its local cap and
// hands control back to edge, which starts a fresh downstream chain — the
// "retry budget propagation" behavior across layers.
func TestLocalCapsTriggerUpstreamRetries(t *testing.T) {
	o := harness.Run(harness.Scenario{
		Name:        "local-cap-propagation",
		Description: "mid hits local cap; edge retries with a fresh chain until root is spent",
		Script:      []fault.Outcome{fault.Unavailable(0)},
		RootMax:     12,
		Retry:       testRetry(),
		EdgeLocal:   2,
		MidLocal:    2,
		ClientLocal: 2,
	})
	assertAttemptAccounting(t, o)
	if o.Status != "budget-exhausted" {
		t.Fatalf("status=%s, want budget-exhausted", o.Status)
	}
	if o.Tiers.EdgeAttempts < 2 {
		t.Fatalf("edge attempts=%d, want edge to retry with fresh chains", o.Tiers.EdgeAttempts)
	}
	if o.Tiers.ClientAttempts < 2 {
		t.Fatalf("client attempts=%d, want client retries too", o.Tiers.ClientAttempts)
	}
}

// TestServerRetryAfterRecovers: the leaf throttles once with Retry-After, then
// succeeds. The wait hint is honored (virtual clock shows the elapsed wait)
// and the call ultimately succeeds within budget.
func TestServerRetryAfterRecovers(t *testing.T) {
	o := harness.Run(harness.Scenario{
		Name:        "retry-after-recovery",
		Description: "first leaf hit is 429 with Retry-After=1s, second is OK",
		Script: []fault.Outcome{
			fault.TooManyRequests(time.Second),
			fault.OK(),
		},
		RootMax:     10,
		Retry:       testRetry(),
		EdgeLocal:   3,
		MidLocal:    3,
		ClientLocal: 3,
	})
	assertAttemptAccounting(t, o)
	if o.Status != "success" {
		t.Fatalf("status=%s, want success (body=%s)", o.Status, o.Verdict)
	}
	if o.Tiers.LeafHits != 2 {
		t.Fatalf("leaf hits=%d, want 2 (throttle then success)", o.Tiers.LeafHits)
	}
	if o.Elapsed < time.Second {
		t.Fatalf("elapsed virtual time=%s, want >= Retry-After 1s", o.Elapsed)
	}
}

// TestRetryAfterDoesNotBypassBudget: a long Retry-After plus a tight deadline
// ends in deadline-exceeded rather than infinite waiting.
func TestRetryAfterDoesNotBypassBudget(t *testing.T) {
	o := harness.Run(harness.Scenario{
		Name:        "retry-after-vs-deadline",
		Description: "Retry-After beyond propagated deadline stops the loop",
		Script:      []fault.Outcome{fault.TooManyRequests(10 * time.Second)},
		RootMax:     20,
		Deadline:    250 * time.Millisecond,
		Retry:       testRetry(),
		EdgeLocal:   5,
		MidLocal:    5,
		ClientLocal: 5,
	})
	assertAttemptAccounting(t, o)
	if o.Status != "deadline-exceeded" {
		t.Fatalf("status=%s, want deadline-exceeded", o.Status)
	}
}

// TestCancellationUnwindsAllLayers uses a real clock: cancel while layers are
// in backoff; the result must be canceled and attempt accounting intact.
func TestCancellationUnwindsAllLayers(t *testing.T) {
	o := harness.Run(harness.Scenario{
		Name:         "cancellation",
		Description:  "client cancels mid-flight while tiers back off",
		Script:       []fault.Outcome{fault.Unavailable(0)},
		RootMax:      100, // big enough that budget would not end it first
		CancelAfter:  40 * time.Millisecond,
		Retry:        testRetry(),
		EdgeLocal:    10,
		MidLocal:     10,
		ClientLocal:  10,
		UseRealClock: true,
	})
	assertAttemptAccounting(t, o)
	if o.Status != "canceled" {
		t.Fatalf("status=%s, want canceled", o.Status)
	}
	if o.TotalUsed >= 100 {
		t.Fatalf("cancellation case consumed full budget (%d)", o.TotalUsed)
	}
}

// TestNonRetryableFailureIsNeverReplayed: a deterministic 400 surfaces through
// all three layers exactly once per chain — unknown/deterministic failures
// must never be retried.
func TestNonRetryableFailureIsNeverReplayed(t *testing.T) {
	o := harness.Run(harness.Scenario{
		Name:        "non-retryable-400",
		Description: "leaf 400 is unsafe to retry; exactly one chain runs",
		Script:      []fault.Outcome{fault.BadRequest()},
		RootMax:     10,
		Retry:       testRetry(),
		EdgeLocal:   3,
		MidLocal:    3,
		ClientLocal: 3,
	})
	assertAttemptAccounting(t, o)
	if o.Status != "non-retryable" {
		t.Fatalf("status=%s, want non-retryable", o.Status)
	}
	if o.Tiers.ClientAttempts != 1 || o.Tiers.EdgeAttempts != 1 ||
		o.Tiers.MidAttempts != 1 || o.Tiers.LeafHits != 1 {
		t.Fatalf("exactly one attempt per layer expected, got %+v", o.Tiers)
	}
}

// TestImmediateSuccessCostsOneChain: happy path uses exactly one attempt at
// each layer.
func TestImmediateSuccessCostsOneChain(t *testing.T) {
	o := harness.Run(harness.Scenario{
		Name:        "immediate-success",
		Description: "leaf OK on first hit",
		Script:      []fault.Outcome{fault.OK()},
		RootMax:     10,
		Retry:       testRetry(),
		EdgeLocal:   3,
		MidLocal:    3,
		ClientLocal: 3,
	})
	assertAttemptAccounting(t, o)
	if o.Status != "success" {
		t.Fatalf("status=%s, want success", o.Status)
	}
	if o.TotalUsed != 4 {
		t.Fatalf("totalUsed=%d, want 4 (one attempt per layer)", o.TotalUsed)
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
