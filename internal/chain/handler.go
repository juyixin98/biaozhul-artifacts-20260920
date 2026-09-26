// Package chain wires HTTP service layers that propagate retry budget,
// deadline and idempotency to the next hop.
package chain

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"time"

	"github.com/example/retrybudget/internal/retry"
)

// Recorder counts outbound attempts per layer for structured reporting.
type Recorder struct {
	mu     sync.Mutex
	Counts map[string]int
}

func NewRecorder() *Recorder { return &Recorder{Counts: map[string]int{}} }

func (r *Recorder) Add(layer string) {
	r.mu.Lock()
	r.Counts[layer]++
	r.mu.Unlock()
}

func (r *Recorder) Total() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	t := 0
	for _, n := range r.Counts {
		t += n
	}
	return t
}

// HandlerTransport adapts an http.Handler into a RoundTripper so layers can
// be chained fully in-process (the "fake external service" lives in this
// process) while still exercising real header propagation.
type HandlerTransport struct {
	Name     string
	Handler  http.Handler
	Recorder *Recorder
}

func (t HandlerTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	if t.Recorder != nil {
		t.Recorder.Add(t.Name)
	}
	if err := req.Context().Err(); err != nil {
		return nil, err
	}
	rec := httptest.NewRecorder()
	t.Handler.ServeHTTP(rec, req)
	return rec.Result(), nil
}

// Layer is one service tier: it accepts a request, resolves the shared retry
// budget (via the budget-ID header and the store, falling back to the
// remaining-attempts header or a local default at the root), and calls the
// next tier through a retry.Caller.
type Layer struct {
	Name string
	// DefaultBudget is used when the incoming request carries no budget
	// header (i.e. this layer is the root of the call tree).
	DefaultBudget int64
	// MaxDuration caps the local request lifetime; the effective deadline is
	// the earlier of the propagated deadline and now+MaxDuration.
	MaxDuration time.Duration
	Caller      retry.Caller
	Next        retry.RoundTripper
	// Store resolves budget IDs to the shared counter. Required for correct
	// cross-layer accounting; without it each layer would count its own
	// retries independently and the tree could amplify attempts.
	Store *retry.BudgetStore
}

type errorBody struct {
	Error   string `json:"error"`
	Layer   string `json:"layer"`
	Details string `json:"details,omitempty"`
}

func writeError(w http.ResponseWriter, status int, layer, code, details string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(errorBody{Error: code, Layer: layer, Details: details})
}

func (l *Layer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	budget, budgetID, owned := l.resolveBudget(r)
	if owned {
		defer l.Store.Release(budgetID)
	}
	ctx = retry.WithBudget(ctx, budget)
	ctx = retry.WithIdempotent(ctx, retry.IdempotentFromHeader(r.Header))

	if dl, ok := retry.DeadlineFromHeader(r.Header); ok {
		var cancel context.CancelFunc
		ctx, cancel = context.WithDeadline(ctx, dl)
		defer cancel()
	}
	if l.MaxDuration > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, l.MaxDuration)
		defer cancel()
	}

	var body []byte
	if r.Body != nil {
		var err error
		body, err = io.ReadAll(r.Body)
		if err != nil {
			writeError(w, http.StatusBadRequest, l.Name, "read_body", err.Error())
			return
		}
	}

	res := l.Caller.Do(ctx, func(attempt int) (*http.Request, error) {
		req, err := http.NewRequestWithContext(ctx, r.Method, r.URL.Path, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header = r.Header.Clone()
		req.Header.Set(retry.HeaderBudgetID, budgetID)
		retry.SetBudgetHeader(req.Header, budget.Remaining())
		if dl, ok := ctx.Deadline(); ok {
			retry.SetDeadlineHeader(req.Header, dl)
		}
		retry.SetIdempotentHeader(req.Header, retry.IdempotentFrom(ctx))
		return req, nil
	}, l.Next)

	if res.Err != nil {
		status := http.StatusServiceUnavailable
		code := "upstream_error"
		switch {
		case res.Err == retry.ErrBudgetExhausted:
			status, code = http.StatusTooManyRequests, "budget_exhausted"
		case res.Err == retry.ErrDeadlineExceeded:
			status, code = http.StatusGatewayTimeout, "deadline_exceeded"
		case res.Err == context.Canceled:
			status, code = 499, "canceled"
		case res.Err == context.DeadlineExceeded:
			status, code = http.StatusGatewayTimeout, "deadline_exceeded"
		}
		writeError(w, status, l.Name, code, res.Err.Error())
		return
	}
	defer res.Resp.Body.Close()
	for k, vs := range res.Resp.Header {
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(res.Resp.StatusCode)
	io.Copy(w, res.Resp.Body)
}

// resolveBudget finds the shared budget for this call tree. The returned
// owned flag is true when this layer created the tree's budget (root) and
// must release it from the store.
func (l *Layer) resolveBudget(r *http.Request) (budget *retry.Budget, id string, owned bool) {
	id = r.Header.Get(retry.HeaderBudgetID)
	if id != "" && l.Store != nil {
		if b := l.Store.Get(id); b != nil {
			return b, id, false
		}
	}
	budget = retry.BudgetFromHeader(r.Header)
	if budget == nil {
		budget = retry.NewBudget(l.DefaultBudget)
	}
	if id == "" {
		id = retry.NewBudgetID()
		owned = true
	}
	if l.Store != nil {
		l.Store.Register(id, budget)
	}
	return budget, id, owned
}
