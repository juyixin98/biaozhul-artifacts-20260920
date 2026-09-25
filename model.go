package tailsampling

// Span is the minimal OpenTelemetry-like span model used by the sampler.
// Times are expressed as Unix epoch milliseconds.
type Span struct {
	TraceID      string `json:"trace_id"`
	SpanID       string `json:"span_id"`
	ParentSpanID string `json:"parent_span_id,omitempty"`
	Name         string `json:"name"`
	Service      string `json:"service,omitempty"`
	// Status is "OK" (default) or "ERROR".
	Status string `json:"status,omitempty"`
	// ErrorEvent carries an error description when Status == "ERROR".
	ErrorEvent  string            `json:"error_event,omitempty"`
	StartTimeMs int64             `json:"start_time_ms"`
	DurationMs  int64             `json:"duration_ms"`
	Attributes  map[string]string `json:"attributes,omitempty"`
}

func (s *Span) endMs() int64 { return s.StartTimeMs + s.DurationMs }

// IsRoot reports whether the span is a trace root (no parent).
func (s *Span) IsRoot() bool { return s.ParentSpanID == "" }

func (s *Span) isError() bool { return s.Status == StatusError }

// LateEvent records a span that arrived after the trace decision was finalized.
// The decision is immutable; the late span is recorded for observability only.
type LateEvent struct {
	TraceID     string `json:"trace_id"`
	SpanID      string `json:"span_id"`
	Name        string `json:"name"`
	Status      string `json:"status,omitempty"`
	ArrivedAtMs int64  `json:"arrived_at_ms"`
	DecidedAtMs int64  `json:"decided_at_ms"`
	Note        string `json:"note"`
}

// Reason codes returned in Decision.ReasonCode.
const (
	ReasonErrorKeep        = "ERROR_POLICY"
	ReasonLatencyKeep      = "LATENCY_POLICY"
	ReasonProbabilistic    = "PROBABILISTIC_KEEP"
	ReasonBudgetDrop       = "BUDGET_DROP"
	ReasonDefaultDrop      = "DEFAULT_DROP"
	ReasonForcedIncomplete = "FORCED_INCOMPLETE"
)

// Decision is the immutable final sampling decision for one trace.
type Decision struct {
	TraceID string `json:"trace_id"`
	Kept    bool   `json:"kept"`
	// Complete is false when the wait/TTL window expired before the trace
	// was observed as complete (root span missing). Such traces are
	// explicitly marked rather than silently sampled.
	Complete   bool   `json:"complete"`
	RootSpanID string `json:"root_span_id,omitempty"`
	SpanCount  int    `json:"span_count"`
	ErrorCount int    `json:"error_count"`
	DurationMs int64  `json:"duration_ms"`
	// Policy names the policy that produced the decision.
	Policy     string `json:"policy"`
	ReasonCode string `json:"reason_code"`
	Reason     string `json:"reason"`
	// Degraded is true when the sampler overran its keep budget and had to
	// downgrade an otherwise-keepable (latency) trace to drop.
	Degraded     bool  `json:"degraded"`
	BudgetUsed   bool  `json:"budget_used"`
	TraceStartMs int64 `json:"trace_start_ms"`
	TraceEndMs   int64 `json:"trace_end_ms"`
	DecidedAtMs  int64 `json:"decided_at_ms"`
	// LateSpans is a snapshot of late arrivals known at query time.
	LateSpans []LateEvent `json:"late_spans,omitempty"`
}

// Stats summarizes sampler operation since startup.
type Stats struct {
	GeneratedAtMs   int64          `json:"generated_at_ms"`
	OpenTraces      int            `json:"open_traces"`
	TotalDecided    int            `json:"total_decided"`
	Kept            int            `json:"kept"`
	Dropped         int            `json:"dropped"`
	KeptByPolicy    map[string]int `json:"kept_by_policy"`
	DroppedByReason map[string]int `json:"dropped_by_reason"`
	Incomplete      int            `json:"incomplete_decisions"`
	// DegradedDowngrades counts otherwise-keepable traces downgraded to
	// DROP because the keep budget was exhausted.
	DegradedDowngrades int `json:"degraded_downgrades"`
	// DegradedOverBudgetKeeps counts error-priority traces kept beyond the
	// exhausted budget (signal preservation; explicitly flagged).
	DegradedOverBudgetKeeps int `json:"degraded_over_budget_keeps"`
	LateArrivals            int `json:"late_arrivals"`
	// BudgetObservations reports the latest token-bucket view.
	BudgetObservations BudgetView `json:"budget"`
}

// BudgetView is a point-in-time snapshot of the keep budget token bucket.
type BudgetView struct {
	Capacity   float64 `json:"capacity"`
	Tokens     float64 `json:"tokens"`
	RefillRate float64 `json:"refill_per_sec"`
	Exhausted  bool    `json:"exhausted"`
}
