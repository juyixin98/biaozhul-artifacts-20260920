// Package scenario runs the three-layer fault fixture end to end and
// produces structured (JSON-serializable) results.
package scenario

import (
	"context"
	"fmt"
	"math/rand"
	"net/http"
	"time"

	"github.com/example/retrybudget/internal/chain"
	"github.com/example/retrybudget/internal/clock"
	"github.com/example/retrybudget/internal/fakesvc"
	"github.com/example/retrybudget/internal/retry"
)

// Check is one assertion inside a scenario result.
type Check struct {
	Name   string `json:"name"`
	Passed bool   `json:"passed"`
	Detail string `json:"detail,omitempty"`
}

// Result is the structured outcome of one scenario run.
type Result struct {
	Scenario      string         `json:"scenario"`
	Passed        bool           `json:"passed"`
	RootBudget    int64          `json:"root_budget"`
	Attempts      map[string]int `json:"attempts"`
	TotalAttempts int            `json:"total_attempts"`
	HTTPStatus    int            `json:"http_status"`
	Outcome       string         `json:"outcome"`
	SleepsMs      []int64        `json:"sleeps_ms,omitempty"`
	Checks        []Check        `json:"checks"`
}

// fixture is a three-layer chain: client -> layer1 -> layer2 -> fake svc.
type fixture struct {
	recorder *chain.Recorder
	fake     *fakesvc.Service
	store    *retry.BudgetStore
	entry    http.Handler // layer1
	caller   retry.Caller
}

func newFixture(clk clock.Clock, rootBudget int64) *fixture {
	rec := chain.NewRecorder()
	fake := fakesvc.New()
	store := retry.NewBudgetStore()
	caller := retry.Caller{
		Clock: clk,
		Backoff: retry.Backoff{
			Base:       2 * time.Millisecond,
			Max:        20 * time.Millisecond,
			Multiplier: 2,
			Rand:       rand.New(rand.NewSource(42)),
		},
	}
	layer2 := &chain.Layer{
		Name:          "layer2",
		DefaultBudget: rootBudget,
		Caller:        caller,
		Next:          chain.HandlerTransport{Name: "layer2->fake", Handler: fake, Recorder: rec},
		Store:         store,
	}
	layer1 := &chain.Layer{
		Name:          "layer1",
		DefaultBudget: rootBudget,
		Caller:        caller,
		Next:          chain.HandlerTransport{Name: "layer1->layer2", Handler: layer2, Recorder: rec},
		Store:         store,
	}
	return &fixture{recorder: rec, fake: fake, store: store, entry: layer1, caller: caller}
}

// call runs one root request through the chain, owning the root budget.
func (f *fixture) call(ctx context.Context, rootBudget int64, idempotent bool, deadline time.Time, faultHeaders map[string]string) Result {
	budget := retry.NewBudget(rootBudget)
	budgetID := retry.NewBudgetID()
	f.store.Register(budgetID, budget)
	defer f.store.Release(budgetID)
	ctx = retry.WithBudget(ctx, budget)
	ctx = retry.WithIdempotent(ctx, idempotent)
	if !deadline.IsZero() {
		var cancel context.CancelFunc
		ctx, cancel = context.WithDeadline(ctx, deadline)
		defer cancel()
	}

	var res Result
	res = Result{Attempts: map[string]int{}}
	out := f.caller.Do(ctx, func(attempt int) (*http.Request, error) {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, "/call", nil)
		if err != nil {
			return nil, err
		}
		req.Header.Set(retry.HeaderBudgetID, budgetID)
		retry.SetBudgetHeader(req.Header, budget.Remaining())
		if dl, ok := ctx.Deadline(); ok {
			retry.SetDeadlineHeader(req.Header, dl)
		}
		retry.SetIdempotentHeader(req.Header, idempotent)
		for k, v := range faultHeaders {
			req.Header.Set(k, v)
		}
		return req, nil
	}, chain.HandlerTransport{Name: "client->layer1", Handler: f.entry, Recorder: f.recorder})

	for k, v := range f.recorder.Counts {
		res.Attempts[k] = v
	}
	res.TotalAttempts = f.recorder.Total()
	for _, s := range out.Sleeps {
		res.SleepsMs = append(res.SleepsMs, s.Milliseconds())
	}
	if out.Err != nil {
		res.Outcome = out.Err.Error()
	}
	if out.Resp != nil {
		res.HTTPStatus = out.Resp.StatusCode
		out.Resp.Body.Close()
		if res.Outcome == "" {
			res.Outcome = "http_" + http.StatusText(out.Resp.StatusCode)
		}
	}
	// A cancelled root context is the authoritative outcome, even if a
	// downstream response raced back after cancellation.
	if ctx.Err() != nil {
		res.Outcome = ctx.Err().Error()
	}
	return res
}

func (r *Result) finalize(checks ...Check) {
	r.Checks = checks
	r.Passed = true
	for _, c := range checks {
		if !c.Passed {
			r.Passed = false
		}
	}
}

func check(name string, passed bool, detail string) Check {
	return Check{Name: name, Passed: passed, Detail: detail}
}

// RunAll executes every acceptance scenario and returns structured results.
func RunAll() []Result {
	return []Result{
		happyPath(),
		transientFailure(),
		retryAfter(),
		budgetExhaustion(),
		cancellation(),
		nonIdempotent(),
	}
}

// happyPath: no faults; exactly one attempt per hop, success.
func happyPath() Result {
	const budget = 8
	fx := newFixture(clock.Real{}, budget)
	res := fx.call(context.Background(), budget, true, time.Time{}, nil)
	res.Scenario = "happy_path"
	res.RootBudget = budget
	res.finalize(
		check("status_200", res.HTTPStatus == 200, fmt.Sprintf("got %d", res.HTTPStatus)),
		check("total_attempts_eq_3", res.TotalAttempts == 3, fmt.Sprintf("got %d", res.TotalAttempts)),
	)
	return res
}

// transientFailure: fake fails twice then succeeds; retries recover within budget.
func transientFailure() Result {
	const budget = 8
	fx := newFixture(clock.Real{}, budget)
	res := fx.call(context.Background(), budget, true, time.Time{}, map[string]string{
		fakesvc.HeaderFaultID:        "transient",
		fakesvc.HeaderFaultFailTimes: "2",
	})
	res.Scenario = "transient_failure"
	res.RootBudget = budget
	res.finalize(
		check("status_200", res.HTTPStatus == 200, fmt.Sprintf("got %d", res.HTTPStatus)),
		check("fake_called_3_times", fx.fake.Calls("transient") == 3, fmt.Sprintf("got %d", fx.fake.Calls("transient"))),
		check("total_within_budget", res.TotalAttempts <= budget, fmt.Sprintf("total=%d budget=%d", res.TotalAttempts, budget)),
	)
	return res
}

// retryAfter: server dictates the wait via Retry-After; the client honors it.
func retryAfter() Result {
	const budget = 8
	fx := newFixture(clock.Real{}, budget)
	res := fx.call(context.Background(), budget, true, time.Time{}, map[string]string{
		fakesvc.HeaderFaultID:         "retry-after",
		fakesvc.HeaderFaultFailTimes:  "1",
		fakesvc.HeaderFaultRetryAfter: "0",
	})
	res.Scenario = "retry_after"
	res.RootBudget = budget
	res.finalize(
		check("status_200", res.HTTPStatus == 200, fmt.Sprintf("got %d", res.HTTPStatus)),
		check("fake_called_2_times", fx.fake.Calls("retry-after") == 2, fmt.Sprintf("got %d", fx.fake.Calls("retry-after"))),
	)
	return res
}

// budgetExhaustion: persistent 503 with root budget 5; total attempts across
// all three layers must be exactly the budget, then budget_exhausted.
func budgetExhaustion() Result {
	const budget = 5
	fx := newFixture(clock.Real{}, budget)
	res := fx.call(context.Background(), budget, true, time.Time{}, map[string]string{
		fakesvc.HeaderFaultID:        "exhaustion",
		fakesvc.HeaderFaultFailTimes: "100",
	})
	res.Scenario = "budget_exhaustion"
	res.RootBudget = budget
	res.finalize(
		check("outcome_budget_exhausted", res.Outcome == retry.ErrBudgetExhausted.Error(), res.Outcome),
		check("total_attempts_eq_budget", res.TotalAttempts == budget, fmt.Sprintf("total=%d budget=%d", res.TotalAttempts, budget)),
	)
	return res
}

// cancellation: the fake hangs; the caller cancels; no further attempts.
func cancellation() Result {
	const budget = 10
	fx := newFixture(clock.Real{}, budget)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan Result, 1)
	go func() {
		done <- fx.call(ctx, budget, true, time.Time{}, map[string]string{
			fakesvc.HeaderFaultID:     "cancel",
			fakesvc.HeaderFaultHangMs: "5000",
		})
	}()
	time.Sleep(50 * time.Millisecond)
	cancel()
	res := <-done
	res.Scenario = "cancellation"
	res.RootBudget = budget
	res.finalize(
		check("outcome_canceled", res.Outcome == context.Canceled.Error(), res.Outcome),
		check("no_retries_after_cancel", res.TotalAttempts == 3, fmt.Sprintf("got %d", res.TotalAttempts)),
	)
	return res
}

// nonIdempotent: operation not marked safe to retry; a 503 is final.
func nonIdempotent() Result {
	const budget = 8
	fx := newFixture(clock.Real{}, budget)
	res := fx.call(context.Background(), budget, false, time.Time{}, map[string]string{
		fakesvc.HeaderFaultID:        "nonidem",
		fakesvc.HeaderFaultFailTimes: "100",
	})
	res.Scenario = "non_idempotent_no_retry"
	res.RootBudget = budget
	res.finalize(
		check("status_503", res.HTTPStatus == 503, fmt.Sprintf("got %d", res.HTTPStatus)),
		check("total_attempts_eq_3_no_retries", res.TotalAttempts == 3, fmt.Sprintf("got %d", res.TotalAttempts)),
	)
	return res
}
