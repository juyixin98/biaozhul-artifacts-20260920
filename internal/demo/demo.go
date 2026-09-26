// Package demo assembles the built-in acceptance scenario suite and renders
// its structured results. It contains no production logic; it is the
// executable specification used by the demo server.
package demo

import (
	"time"

	"retrybudget/internal/fault"
	"retrybudget/internal/harness"
	"retrybudget/internal/retry"
)

// Case pairs a scenario with its acceptance assertion.
type Case struct {
	Scenario harness.Scenario
	// Expect is the required terminal status.
	Expect string
	// ExtraAssert optionally applies additional checks.
	ExtraAssert func(o *harness.Outcome) (string, bool)
}

// Suite returns the built-in acceptance cases.
func Suite() []Case {
	cfg := retry.Config{
		BaseDelay:  5 * time.Millisecond,
		MaxDelay:   50 * time.Millisecond,
		Multiplier: 2,
		Jitter:     0,
	}
	const locals = 3
	return []Case{
		{
			Scenario: harness.Scenario{
				Name:        "root-budget-cap",
				Description: "Always-failing leaf, every layer willing to retry; total attempts must equal the root cap (8).",
				Script:      []fault.Outcome{fault.Unavailable(0)},
				RootMax:     8,
				Retry:       cfg,
				EdgeLocal:   locals, MidLocal: locals, ClientLocal: locals,
			},
			Expect: "budget-exhausted",
			ExtraAssert: func(o *harness.Outcome) (string, bool) {
				if o.TotalUsed != 8 {
					return "total used must equal root cap 8", false
				}
				return "", true
			},
		},
		{
			Scenario: harness.Scenario{
				Name:        "local-cap-upstream-retry",
				Description: "Mid burns its local cap; edge starts fresh chains until the root is spent.",
				Script:      []fault.Outcome{fault.Unavailable(0)},
				RootMax:     12,
				Retry:       cfg,
				EdgeLocal:   2, MidLocal: 2, ClientLocal: 2,
			},
			Expect: "budget-exhausted",
			ExtraAssert: func(o *harness.Outcome) (string, bool) {
				if o.Tiers.EdgeAttempts < 2 || o.Tiers.ClientAttempts < 2 {
					return "edge and client must each retry with fresh chains", false
				}
				return "", true
			},
		},
		{
			Scenario: harness.Scenario{
				Name:        "server-retry-after-recovery",
				Description: "Leaf returns 429 with Retry-After=1s once, then OK; the hint is honored and the call succeeds.",
				Script: []fault.Outcome{
					fault.TooManyRequests(time.Second),
					fault.OK(),
				},
				RootMax: 10, Retry: cfg,
				EdgeLocal: locals, MidLocal: locals, ClientLocal: locals,
			},
			Expect: "success",
			ExtraAssert: func(o *harness.Outcome) (string, bool) {
				if o.Tiers.LeafHits != 2 {
					return "leaf should be hit exactly twice", false
				}
				if o.Elapsed < time.Second {
					return "virtual elapsed time must include the 1s Retry-After wait", false
				}
				return "", true
			},
		},
		{
			Scenario: harness.Scenario{
				Name:        "retry-after-vs-deadline",
				Description: "A 10s Retry-After against a 250ms propagated deadline ends in deadline-exceeded.",
				Script:      []fault.Outcome{fault.TooManyRequests(10 * time.Second)},
				RootMax:     20, Deadline: 250 * time.Millisecond, Retry: cfg,
				EdgeLocal: 5, MidLocal: 5, ClientLocal: 5,
			},
			Expect: "deadline-exceeded",
		},
		{
			Scenario: harness.Scenario{
				Name:        "client-cancellation",
				Description: "Client cancels after 40ms while tiers are in backoff; the call unwinds as canceled.",
				Script:      []fault.Outcome{fault.Unavailable(0)},
				RootMax:     100,
				CancelAfter: 40 * time.Millisecond,
				Retry:       cfg,
				EdgeLocal:   10, MidLocal: 10, ClientLocal: 10,
				UseRealClock: true,
			},
			Expect: "canceled",
			ExtraAssert: func(o *harness.Outcome) (string, bool) {
				if o.TotalUsed >= 100 {
					return "cancellation must end the call before the budget is spent", false
				}
				return "", true
			},
		},
		{
			Scenario: harness.Scenario{
				Name:        "non-retryable-400",
				Description: "A deterministic 400 is unsafe to retry; exactly one chain runs.",
				Script:      []fault.Outcome{fault.BadRequest()},
				RootMax:     10, Retry: cfg,
				EdgeLocal: locals, MidLocal: locals, ClientLocal: locals,
			},
			Expect: "non-retryable",
			ExtraAssert: func(o *harness.Outcome) (string, bool) {
				if o.Tiers.ClientAttempts != 1 || o.Tiers.EdgeAttempts != 1 ||
					o.Tiers.MidAttempts != 1 || o.Tiers.LeafHits != 1 {
					return "exactly one attempt per layer expected", false
				}
				return "", true
			},
		},
		{
			Scenario: harness.Scenario{
				Name:        "immediate-success",
				Description: "Leaf is healthy; a single chain of four attempts succeeds.",
				Script:      []fault.Outcome{fault.OK()},
				RootMax:     10, Retry: cfg,
				EdgeLocal: locals, MidLocal: locals, ClientLocal: locals,
			},
			Expect: "success",
			ExtraAssert: func(o *harness.Outcome) (string, bool) {
				if o.TotalUsed != 4 {
					return "one attempt per layer = 4 total", false
				}
				return "", true
			},
		},
	}
}

// CaseResult is a scenario outcome plus its assertion verdict.
type CaseResult struct {
	Case    string          `json:"case"`
	Passed  bool            `json:"passed"`
	Reasons []string        `json:"reasons,omitempty"`
	Outcome harness.Outcome `json:"outcome"`
}

// RunSuite executes every case and evaluates its assertions.
func RunSuite() []CaseResult {
	results := make([]CaseResult, 0, len(Suite()))
	for _, c := range Suite() {
		o := harness.Run(c.Scenario)
		reasons := []string{}
		if o.Status != c.Expect {
			reasons = append(reasons, "status="+o.Status+", want "+c.Expect)
		}
		if c.ExtraAssert != nil {
			if msg, ok := c.ExtraAssert(&o); !ok {
				reasons = append(reasons, msg)
			}
		}
		results = append(results, CaseResult{
			Case:    c.Scenario.Name,
			Passed:  len(reasons) == 0,
			Reasons: reasons,
			Outcome: o,
		})
	}
	return results
}
