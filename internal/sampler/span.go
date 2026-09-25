package sampler

import "time"

// Span is a single unit of work in a trace. Timestamps are Unix
// milliseconds so the wire format stays language-neutral.
type Span struct {
	TraceID     string            `json:"trace_id"`
	SpanID      string            `json:"span_id"`
	ParentID    string            `json:"parent_id,omitempty"`
	Service     string            `json:"service"`
	Name        string            `json:"name"`
	StartUnixMs int64             `json:"start_unix_ms"`
	DurationMs  int64             `json:"duration_ms"`
	Status      string            `json:"status"` // "ok" | "error"
	Attributes  map[string]string `json:"attributes,omitempty"`
}

// EndUnixMs returns the span end time in Unix milliseconds.
func (s Span) EndUnixMs() int64 { return s.StartUnixMs + s.DurationMs }

// IngestStatus is the per-trace outcome reported back to the ingest caller.
type IngestStatus string

const (
	StatusPending IngestStatus = "pending" // buffered, waiting for the decision window
	StatusKeep    IngestStatus = "keep"    // already decided: keep
	StatusDrop    IngestStatus = "drop"    // already decided: drop
)

// Decision is the final, immutable sampling verdict for a trace.
type Decision struct {
	TraceID           string    `json:"trace_id"`
	Keep              bool      `json:"keep"`
	Reasons           []string  `json:"reasons"`
	Degraded          bool      `json:"degraded"`
	Incomplete        bool      `json:"incomplete"`
	IncompleteReasons []string  `json:"incomplete_reasons,omitempty"`
	SpanCount         int       `json:"span_count"`
	LateSpans         int       `json:"late_spans"`
	DecidedAt         time.Time `json:"decided_at"`
}

// Stats is a snapshot of sampler counters for the /v1/stats endpoint.
type Stats struct {
	SpansReceived     int64 `json:"spans_received"`
	LateSpans         int64 `json:"late_spans"`
	TracesDecided     int64 `json:"traces_decided"`
	TracesKept        int64 `json:"traces_kept"`
	TracesDropped     int64 `json:"traces_dropped"`
	DegradedDecisions int64 `json:"degraded_decisions"`
	BudgetExhausted   int64 `json:"budget_exhausted_events"`
	InflightForced    int64 `json:"inflight_forced_decisions"`
	InflightTraces    int   `json:"inflight_traces"`
	BudgetUsed        int   `json:"budget_used_this_window"`
}
