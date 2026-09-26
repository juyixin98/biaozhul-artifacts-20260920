package mesh

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	"retrybudget/internal/clock"
	"retrybudget/internal/fault"
	"retrybudget/internal/propagation"
	"retrybudget/internal/report"
	"retrybudget/internal/retry"
)

// LeafServer is the in-process fake external dependency at the bottom of the
// call tree. It serves scripted outcomes, advertises Retry-After and honors
// the propagated budget: it refuses work when no root slot remains.
type LeafServer struct {
	script *fault.Script
	rec    *report.Collector
	clk    clock.Clock
}

func NewLeafServer(script *fault.Script, rec *report.Collector, clk clock.Clock) *LeafServer {
	if clk == nil {
		clk = clock.Real{}
	}
	return &LeafServer{script: script, rec: rec, clk: clk}
}

func (s *LeafServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	v, err := propagation.Parse(r.Header)
	reqID := v.RequestID
	if err != nil {
		writeVerdict(w, http.StatusBadRequest, VerdictNonRetryable, 0, err.Error())
		return
	}

	var budget *retry.Budget
	if v.HasBudget {
		budget = retry.Restore(s.clk, v.Max, v.Used, v.Deadline)
	} else {
		budget = retry.NewRootBudget(s.clk, 1, time.Time{})
	}
	permit, perr := budget.Reserve(r.Context())
	if perr != nil {
		verdict, status := mapReserveError(perr)
		s.rec.Add(s.clk.Now(), reqID, "leaf", report.EvOutcome, permit.Attempt, "rejected: "+verdict)
		writeVerdict(w, status, verdict, budget.Used(), perr.Error())
		return
	}

	outcome, hit := s.script.Next(reqID)
	detail := fmt.Sprintf("script hit %d -> %d %s", hit, outcome.Status, outcome.Message)
	s.rec.Add(s.clk.Now(), reqID, "leaf", report.EvAttemptEnd, permit.Attempt, detail)

	h := w.Header()
	h.Set(propagation.HeaderUsed, fmt.Sprint(budget.Used()))
	h.Set(headerVerdict, verdictForStatus(outcome.Status))
	if outcome.RetryAfter > 0 {
		// Delta-seconds form (RFC 9110 §10.2.3).
		h.Set("Retry-After", fmt.Sprintf("%d", int((outcome.RetryAfter+time.Second-1)/time.Second)))
	}
	w.WriteHeader(outcome.Status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"tier":    "leaf",
		"hit":     hit,
		"status":  outcome.Status,
		"attempt": permit.Attempt,
		"message": outcome.Message,
		"used":    budget.Used(),
	})
}

func verdictForStatus(status int) string {
	switch {
	case status >= 200 && status < 300:
		return VerdictOK
	case status == http.StatusTooManyRequests || status == http.StatusServiceUnavailable:
		return VerdictRetryable
	default:
		return VerdictNonRetryable
	}
}

// LayerConfig configures one retrying intermediary tier.
type LayerConfig struct {
	Name     string
	NextURL  string // empty only for the leaf
	Client   httpDoer
	Clock    clock.Clock
	Recorder *report.Collector
	Retry    retry.Config
	LocalMax int // per-call attempts allowed at this tier alone
}

// NewLayerHandler builds an HTTP handler that restores the propagated budget,
// runs its own retry loop with a local cap, and forwards each attempt
// downstream with fresh budget headers.
func NewLayerHandler(cfg LayerConfig) http.Handler {
	if cfg.Clock == nil {
		cfg.Clock = clock.Real{}
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		v, err := propagation.Parse(r.Header)
		if err != nil || !v.HasBudget {
			writeVerdict(w, http.StatusBadRequest, VerdictNonRetryable, 0, "missing or invalid retry budget headers")
			return
		}
		reqID := v.RequestID
		root := retry.Restore(cfg.Clock, v.Max, v.Used, v.Deadline)
		budget := root.Child(cfg.LocalMax)

		// The propagated deadline is enforced by the budget (Reserve/
		// Precheck) against the injected clock, so we keep the request's
		// own context: real client cancellation still propagates through
		// the HTTP transport, while virtual deadlines stay deterministic.
		ctx := r.Context()

		var lastBody []byte
		op := func(ctx context.Context, permit retry.Permit) error {
			cfg.Recorder.Add(cfg.Clock.Now(), reqID, cfg.Name, report.EvAttemptStart, permit.Attempt,
				fmt.Sprintf("forward -> %s (used=%d/%d)", cfg.NextURL, root.Used(), root.Max()))
			resp, body, err := roundTrip(ctx, cfg.Client, http.MethodGet, cfg.NextURL, root, reqID)
			if resp != nil {
				lastBody = body
			}
			cfg.Recorder.Add(cfg.Clock.Now(), reqID, cfg.Name, report.EvAttemptEnd, permit.Attempt,
				fmt.Sprintf("used=%d/%d err=%v", root.Used(), root.Max(), err))

			// Downstream spent its own local cap: the shared budget still
			// allows this tier to launch a brand new attempt.
			var le *retry.LocalExhausted
			if errors.As(err, &le) {
				return retry.Retryable(cfg.Name, le)
			}
			// Annotate propagated terminal verdicts with this tier's view
			// of the global attempt number for clearer error chains.
			var ex *retry.Exhausted
			if errors.As(err, &ex) {
				return &retry.Exhausted{Op: cfg.Name, Attempts: permit.Attempt, Cause: err}
			}
			var de *retry.DeadlineExceeded
			if errors.As(err, &de) {
				return &retry.DeadlineExceeded{Op: cfg.Name, Attempts: permit.Attempt, Cause: err}
			}
			return err
		}

		res := retry.Do(ctx, budget, cfg.Retry, op)
		writeTerminal(w, cfg.Name, res, root.Used(), lastBody)
	})
}

func writeTerminal(w http.ResponseWriter, layer string, res retry.Result, used int, body []byte) {
	if res.Err == nil {
		h := w.Header()
		h.Set(propagation.HeaderUsed, fmt.Sprint(used))
		h.Set(headerVerdict, VerdictOK)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
		return
	}

	var (
		verdict string
		status  = http.StatusServiceUnavailable
		ex      *retry.Exhausted
		de      *retry.DeadlineExceeded
		le      *retry.LocalExhausted
	)
	switch {
	case errors.As(res.Err, &ex):
		verdict, status = VerdictBudgetExhausted, http.StatusServiceUnavailable
	case errors.As(res.Err, &de):
		verdict, status = VerdictDeadline, http.StatusGatewayTimeout
	case errors.As(res.Err, &le):
		verdict, status = VerdictLocalExhausted, http.StatusServiceUnavailable
	default:
		switch retry.Classify(res.Err) {
		case retry.KindCanceled:
			verdict, status = VerdictCanceled, http.StatusRequestTimeout
		case retry.KindNonRetryable:
			verdict, status = VerdictNonRetryable, http.StatusBadRequest
		default:
			verdict, status = VerdictNonRetryable, http.StatusInternalServerError
		}
	}
	writeVerdict(w, status, verdict, used, fmt.Sprintf("%s: %v", layer, res.Err))
}

func mapReserveError(err error) (verdict string, status int) {
	switch {
	case errors.Is(err, retry.ErrBudgetExhausted):
		return VerdictBudgetExhausted, http.StatusServiceUnavailable
	case errors.Is(err, retry.ErrDeadlineExpired):
		return VerdictDeadline, http.StatusGatewayTimeout
	case errors.Is(err, retry.ErrLocalExhausted):
		return VerdictLocalExhausted, http.StatusServiceUnavailable
	default:
		return VerdictNonRetryable, http.StatusBadRequest
	}
}

func writeVerdict(w http.ResponseWriter, status int, verdict string, used int, msg string) {
	h := w.Header()
	h.Set(headerVerdict, verdict)
	if used > 0 {
		h.Set(propagation.HeaderUsed, fmt.Sprint(used))
	}
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"verdict": verdict,
		"status":  status,
		"used":    used,
		"error":   msg,
	})
}
