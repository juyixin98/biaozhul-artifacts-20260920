// Package harness defines declarative scenarios over the three-tier mesh and
// produces structured results: per-tier attempt counts, the shared budget
// verdict and the full event timeline.
package harness

import (
	"context"
	"errors"
	"time"

	"retrybudget/internal/clock"
	"retrybudget/internal/fault"
	"retrybudget/internal/mesh"
	"retrybudget/internal/report"
	"retrybudget/internal/retry"
)

// Scenario is one executable acceptance case.
type Scenario struct {
	Name        string
	Description string
	Script      []fault.Outcome
	RootMax     int
	Deadline    time.Duration // relative to the (virtual) start; 0 = none
	// CancelAfter, when positive, cancels the call after this much real time
	// (used only with a real clock).
	CancelAfter time.Duration
	Retry       retry.Config
	EdgeLocal   int
	MidLocal    int
	ClientLocal int
	// UseRealClock opts out of the auto-advancing virtual clock
	// (cancellation scenarios need real wall-clock).
	UseRealClock bool
}

// TierCounts breaks down reservations observed on the event timeline.
type TierCounts struct {
	ClientAttempts int `json:"client_attempts"`
	EdgeAttempts   int `json:"edge_attempts"`
	MidAttempts    int `json:"mid_attempts"`
	LeafHits       int `json:"leaf_hits"`
	// LeafRejected counts contacts turned away at the leaf door because no
	// root slot remained. These consumed a caller slot but never executed
	// the fake, so they are not part of the served-attempt sum.
	LeafRejected int `json:"leaf_rejected"`
}

// Outcome is the structured result of a scenario.
type Outcome struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	Status      string         `json:"status"`
	Verdict     string         `json:"verdict"`
	HTTPStatus  int            `json:"http_status"`
	RootMax     int            `json:"root_max"`
	TotalUsed   int            `json:"total_used"`
	Tiers       TierCounts     `json:"tiers"`
	Elapsed     time.Duration  `json:"-"`
	ElapsedMS   int64          `json:"elapsed_ms"`
	Timeline    []report.Event `json:"timeline"`
}

// Run executes the scenario against a freshly built mesh and disposes it.
func Run(s Scenario) Outcome {
	clk := clock.Clock(clock.NewAuto(time.Unix(1_700_000_000, 0)))
	if s.UseRealClock {
		clk = clock.Real{}
	}
	rec := report.NewCollector()
	script := fault.NewScript(s.Script...)
	m := mesh.New(mesh.Config{
		Clock:       clk,
		Script:      script,
		Recorder:    rec,
		EdgeLocal:   s.EdgeLocal,
		MidLocal:    s.MidLocal,
		ClientLocal: s.ClientLocal,
		Retry:       s.Retry,
	})

	reqID := "req-" + s.Name
	start := clk.Now()
	var deadlineAbs time.Time
	if s.Deadline > 0 {
		deadlineAbs = start.Add(s.Deadline)
	}

	ctx, cancel := context.WithCancel(context.Background())
	if s.CancelAfter > 0 {
		go func() {
			time.Sleep(s.CancelAfter)
			cancel()
		}()
	} else {
		defer cancel()
	}

	cr := mesh.ClientCall(ctx, m, reqID, s.RootMax, deadlineAbs)
	// Close before snapshotting so handlers unwinding after a cancellation
	// have fully recorded their events (Close waits for in-flight requests).
	m.Close()

	elapsed := clk.Now().Sub(start)
	out := Outcome{
		Name:        s.Name,
		Description: s.Description,
		Status:      classifyResult(cr.Result.Err),
		Verdict:     cr.Verdict,
		HTTPStatus:  cr.StatusCode,
		RootMax:     s.RootMax,
		TotalUsed:   cr.Used,
		Elapsed:     elapsed,
		ElapsedMS:   elapsed.Milliseconds(),
		Timeline:    rec.Timeline(reqID),
	}
	out.Tiers = countTiers(out.Timeline)
	// The client-local counter can miss reservations that were still
	// in flight when the call was canceled; the global axis recorded on
	// the shared timeline is authoritative.
	if g := globalAttempts(out.Timeline); g > out.TotalUsed {
		out.TotalUsed = g
	}
	return out
}

// globalAttempts returns the highest global attempt slot observed anywhere in
// the tree. All layers number permits on the single propagated axis.
func globalAttempts(events []report.Event) int {
	max := 0
	for _, e := range events {
		if e.Type == report.EvAttemptStart || e.Type == report.EvAttemptEnd {
			if e.Attempt > max {
				max = e.Attempt
			}
		}
	}
	return max
}

func countTiers(events []report.Event) TierCounts {
	var c TierCounts
	for _, e := range events {
		switch e.Layer {
		case mesh.TierClient:
			if e.Type == report.EvAttemptStart {
				c.ClientAttempts++
			}
		case mesh.TierEdge:
			if e.Type == report.EvAttemptStart {
				c.EdgeAttempts++
			}
		case mesh.TierMid:
			if e.Type == report.EvAttemptStart {
				c.MidAttempts++
			}
		case mesh.TierLeaf:
			switch e.Type {
			case report.EvAttemptEnd:
				c.LeafHits++
			case report.EvOutcome:
				// Turned away before serving (no slot): a rejected contact,
				// not a served attempt.
				c.LeafRejected++
			}
		}
	}
	return c
}

func classifyResult(err error) string {
	switch {
	case err == nil:
		return "success"
	case errors.As(err, new(*retry.Exhausted)):
		return "budget-exhausted"
	case errors.As(err, new(*retry.DeadlineExceeded)):
		return "deadline-exceeded"
	case errors.Is(err, context.Canceled):
		return "canceled"
	case errors.As(err, new(*retry.LocalExhausted)):
		return "local-exhausted"
	default:
		switch retry.Classify(err) {
		case retry.KindNonRetryable:
			return "non-retryable"
		default:
			return "error"
		}
	}
}
