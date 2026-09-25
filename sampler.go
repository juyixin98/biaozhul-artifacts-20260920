package tailsampling

import (
	"fmt"
	"hash/fnv"
	"time"
)

// TokenBucket is a thread-unsafe refillable keep budget. All access is
// serialized by the aggregator goroutine.
type TokenBucket struct {
	capacity   float64
	tokens     float64
	refillRate float64 // tokens per second
	lastRefill time.Time
}

func NewTokenBucket(capacity, refillRate float64, now time.Time) *TokenBucket {
	return &TokenBucket{
		capacity:   capacity,
		tokens:     capacity,
		refillRate: refillRate,
		lastRefill: now,
	}
}

// refill lazily adds tokens based on elapsed wall time.
func (b *TokenBucket) refill(now time.Time) {
	elapsed := now.Sub(b.lastRefill).Seconds()
	if elapsed <= 0 {
		return
	}
	b.tokens += elapsed * b.refillRate
	if b.tokens > b.capacity {
		b.tokens = b.capacity
	}
	b.lastRefill = now
}

// tryTake attempts to reserve one token.
func (b *TokenBucket) tryTake(now time.Time) bool {
	b.refill(now)
	if b.tokens >= 1 {
		b.tokens--
		return true
	}
	return false
}

func (b *TokenBucket) view(now time.Time) BudgetView {
	b.refill(now)
	return BudgetView{
		Capacity:   b.capacity,
		Tokens:     b.tokens,
		RefillRate: b.refillRate,
		Exhausted:  b.tokens < 1,
	}
}

// traceInput is the information the policy chain evaluates.
type traceInput struct {
	traceID    string
	spanCount  int
	errorCount int
	durationMs int64
	startMs    int64
	endMs      int64
	complete   bool
	rootSpanID string
	now        time.Time
}

// policyDecision is one evaluation outcome, before budget enforcement.
type policyDecision struct {
	kept       bool
	policy     string
	reasonCode string
	reason     string
	// priority marks error-trace keeps that survive budget exhaustion
	// (errors are always kept; exhaustion is recorded as degradation in
	// stats rather than silently losing the signal).
	priority bool
}

// Sampler holds the ordered tail-sampling policy chain and the shared keep
// budget. Evaluation is single-threaded (aggregator goroutine).
type Sampler struct {
	cfg    Config
	budget *TokenBucket
}

func NewSampler(cfg Config, budget *TokenBucket) *Sampler {
	return &Sampler{cfg: cfg, budget: budget}
}

// Evaluate runs the ordered policy chain:
//  1. error span        -> KEEP (highest priority)
//  2. tail latency      -> KEEP candidate (subject to budget)
//  3. probabilistic     -> baseline KEEP (subject to budget)
//  4. default           -> DROP
//
// When the budget is exhausted, latency/probabilistic keeps are explicitly
// downgraded to DROP with Degraded=true. Error keeps always reserve a token;
// if none is available the error is STILL kept (losing error traces defeats
// the sampler's purpose) and the over-budget condition is surfaced in the
// decision reason + stats.
func (s *Sampler) Evaluate(in traceInput) (Decision, policyDecision) {
	pd := s.classify(in)
	d := Decision{
		TraceID:      in.traceID,
		Complete:     in.complete,
		RootSpanID:   in.rootSpanID,
		SpanCount:    in.spanCount,
		ErrorCount:   in.errorCount,
		DurationMs:   in.durationMs,
		Policy:       pd.policy,
		ReasonCode:   pd.reasonCode,
		Reason:       pd.reason,
		TraceStartMs: in.startMs,
		TraceEndMs:   in.endMs,
	}

	if !pd.kept {
		// Pure drops (default drop) do not consume budget.
		return d, pd
	}

	gotToken := s.budget.tryTake(in.now)
	switch {
	case gotToken:
		d.Kept = true
		d.BudgetUsed = true
	case pd.priority:
		// Budget exhausted: errors keep the highest-visibility guarantee.
		d.Kept = true
		d.BudgetUsed = false
		d.Degraded = true
		d.Reason = pd.reason + " | keep budget exhausted: kept beyond budget (degraded)"
	default:
		// Budget exhausted: explicit, explainable downgrade.
		d.Kept = false
		d.Degraded = true
		d.Policy = pd.policy + "+budget"
		d.ReasonCode = ReasonBudgetDrop
		d.Reason = pd.reason + " | keep budget exhausted: downgraded KEEP->DROP (degraded)"
	}
	return d, pd
}

func (s *Sampler) classify(in traceInput) policyDecision {
	if s.cfg.ErrorPolicy && in.errorCount > 0 {
		return policyDecision{
			kept:       true,
			priority:   true,
			policy:     "error",
			reasonCode: ReasonErrorKeep,
			reason: fmt.Sprintf("trace contains %d error span(s); error policy keeps all error traces",
				in.errorCount),
		}
	}

	if s.cfg.LatencyThresholdMs > 0 && in.durationMs >= s.cfg.LatencyThresholdMs {
		return policyDecision{
			kept:       true,
			policy:     "latency",
			reasonCode: ReasonLatencyKeep,
			reason: fmt.Sprintf("trace duration %dms meets latency threshold %dms",
				in.durationMs, s.cfg.LatencyThresholdMs),
		}
	}

	if hashTraceID(in.traceID) < s.cfg.ProbabilisticRate {
		return policyDecision{
			kept:       true,
			policy:     "probabilistic",
			reasonCode: ReasonProbabilistic,
			reason: fmt.Sprintf("trace hash fell within baseline keep rate %.2f",
				s.cfg.ProbabilisticRate),
		}
	}

	return policyDecision{
		kept:       false,
		policy:     "default",
		reasonCode: ReasonDefaultDrop,
		reason:     "no keep policy matched; default drop",
	}
}

// hashTraceID maps a trace id to a deterministic value in [0,1). Determinism
// means the same trace is always classified the same way regardless of which
// spans have arrived.
func hashTraceID(id string) float64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(id))
	return float64(h.Sum64()%10000) / 10000.0
}
