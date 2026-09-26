// Package mesh wires the three-tier in-process HTTP mesh used by the demo and
// the acceptance fixtures:
//
//	client (tier 1, root budget)
//	  -> edge (tier 2, local retry cap)
//	     -> mid  (tier 3, local retry cap)
//	        -> leaf (fake external dependency, scripted faults)
//
// Every hop is real loopback HTTP; every "external" system is an in-process
// scripted fake. One clock and one recorder are shared by injection.
package mesh

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"time"

	"retrybudget/internal/clock"
	"retrybudget/internal/fault"
	"retrybudget/internal/propagation"
	"retrybudget/internal/report"
	"retrybudget/internal/retry"
)

// Tier names used in reports.
const (
	TierClient = "client"
	TierEdge   = "edge"
	TierMid    = "mid"
	TierLeaf   = "leaf"
)

// Config configures a mesh instance.
type Config struct {
	Clock       clock.Clock
	Script      *fault.Script
	Recorder    *report.Collector
	EdgeLocal   int
	MidLocal    int
	ClientLocal int
	Retry       retry.Config
}

// Mesh is a running three-tier mesh; servers are live loopback listeners.
type Mesh struct {
	cfg  Config
	Leaf *httptest.Server
	Mid  *httptest.Server
	Edge *httptest.Server
	HTTP *http.Client
}

// New starts the leaf, mid and edge loopback servers.
func New(cfg Config) *Mesh {
	if cfg.Clock == nil {
		cfg.Clock = clock.Real{}
	}
	if cfg.Recorder == nil {
		cfg.Recorder = report.NewCollector()
	}
	if cfg.Script == nil {
		cfg.Script = fault.NewScript(fault.OK())
	}

	m := &Mesh{cfg: cfg, HTTP: &http.Client{}}

	m.Leaf = httptest.NewServer(NewLeafServer(cfg.Script, cfg.Recorder, cfg.Clock))
	m.Mid = httptest.NewServer(NewLayerHandler(LayerConfig{
		Name:     TierMid,
		NextURL:  m.Leaf.URL,
		Client:   m.HTTP,
		Clock:    cfg.Clock,
		Recorder: cfg.Recorder,
		Retry:    cfg.Retry,
		LocalMax: cfg.MidLocal,
	}))
	m.Edge = httptest.NewServer(NewLayerHandler(LayerConfig{
		Name:     TierEdge,
		NextURL:  m.Mid.URL,
		Client:   m.HTTP,
		Clock:    cfg.Clock,
		Recorder: cfg.Recorder,
		Retry:    cfg.Retry,
		LocalMax: cfg.EdgeLocal,
	}))
	return m
}

// Close stops all servers.
func (m *Mesh) Close() {
	m.Edge.Close()
	m.Mid.Close()
	m.Leaf.Close()
}

// Recorder exposes the shared event collector.
func (m *Mesh) Recorder() *report.Collector { return m.cfg.Recorder }

// Script exposes the leaf fault script.
func (m *Mesh) Script() *fault.Script { return m.cfg.Script }

// CallResult is what the tier-1 client observed.
type CallResult struct {
	StatusCode int
	Verdict    string
	Used       int
	Body       string
	Result     retry.Result
}

// ClientCall runs the tier-1 retry loop against the edge with a fresh root
// budget. It is the only place a root budget is created; every downstream
// tier reconstructs its view from propagated headers.
func ClientCall(ctx context.Context, m *Mesh, reqID string, maxAttempts int, deadline time.Time) CallResult {
	root := retry.NewRootBudget(m.cfg.Clock, maxAttempts, deadline)
	budget := root.Child(m.cfg.ClientLocal)

	var (
		status  int
		verdict string
		body    []byte
	)
	op := func(ctx context.Context, permit retry.Permit) error {
		m.cfg.Recorder.Add(m.cfg.Clock.Now(), reqID, TierClient, report.EvAttemptStart, permit.Attempt,
			"call edge (used="+strconv.Itoa(root.Used())+"/"+strconv.Itoa(root.Max())+")")
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, m.Edge.URL, nil)
		if err != nil {
			return retry.NonRetryable("client", err)
		}
		propagation.Inject(req.Header, reqID, deadline, root.Max(), root.Used())

		resp, err := m.HTTP.Do(req)
		if err != nil {
			m.cfg.Recorder.Add(m.cfg.Clock.Now(), reqID, TierClient, report.EvAttemptEnd, permit.Attempt, "transport: "+err.Error())
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return retry.NonRetryable("client", err)
		}
		defer resp.Body.Close()
		body = readBody(resp)
		status = resp.StatusCode
		verdict = resp.Header.Get(headerVerdict)
		// Reconcile cumulative downstream usage before deciding.
		root.AckUsed(parseUsed(resp.Header))
		m.cfg.Recorder.Add(m.cfg.Clock.Now(), reqID, TierClient, report.EvAttemptEnd, permit.Attempt,
			"status="+strconv.Itoa(status)+" verdict="+verdict+" used="+strconv.Itoa(root.Used()))

		switch verdict {
		case VerdictOK:
			return nil
		case VerdictRetryable:
			return retry.Retryable(TierClient, errFrom(body))
		case VerdictLocalExhausted:
			// Downstream tier burned its own cap; root may still allow us
			// to start a whole fresh chain.
			return retry.Retryable(TierClient, &retry.LocalExhausted{Op: TierEdge, Cause: errFrom(body)})
		case VerdictBudgetExhausted:
			return &retry.Exhausted{Op: TierClient, Cause: errFrom(body)}
		case VerdictDeadline:
			return &retry.DeadlineExceeded{Op: TierClient, Cause: errFrom(body)}
		case VerdictCanceled:
			return context.Canceled
		default:
			return retry.NonRetryable(TierClient, errFrom(body))
		}
	}

	res := retry.Do(ctx, budget, m.cfg.Retry, op)
	return CallResult{
		StatusCode: status,
		Verdict:    verdict,
		Used:       root.Used(),
		Body:       string(body),
		Result:     res,
	}
}

func readBody(resp *http.Response) []byte {
	b, _ := io.ReadAll(resp.Body)
	return b
}

func errFrom(body []byte) error {
	s := string(body)
	if s == "" {
		return errors.New("downstream failure")
	}
	return errors.New(s)
}

func parseUsed(h http.Header) int {
	n, err := strconv.Atoi(h.Get(propagation.HeaderUsed))
	if err != nil || n < 0 {
		return 0
	}
	return n
}
