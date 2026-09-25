// Package model defines the core data model for trace ingestion:
// traces, spans and the synchronous/asynchronous child relation.
package model

import "fmt"

// Span is a single unit of work in a distributed trace.
//
// Time fields are integer nanoseconds relative to an arbitrary epoch
// (Unix-nanos is typical, but only ordering and differences matter).
//
// Relation declares how this span relates to its parent:
//   - "sync": the parent blocks waiting for this child; the child's
//     elapsed time contributes to the parent's synchronous work.
//   - "async": the parent launches this child as a parallel subtask
//     and does NOT block for its duration; only the longest async
//     subtree can extend the critical path, and async children may
//     overlap each other / sync work in wall-clock time.
//
// An empty relation defaults to "sync".
type Span struct {
	SpanID       string `json:"span_id"`
	ParentSpanID string `json:"parent_span_id,omitempty"`
	Name         string `json:"name"`
	StartTime    int64  `json:"start_time"`
	EndTime      int64  `json:"end_time"`
	Relation     string `json:"relation,omitempty"`
}

const (
	RelationSync  = "sync"
	RelationAsync = "async"
)

// Normalize fills defaults. Returns an error if relation is set to a
// value other than sync/async.
func (s *Span) Normalize() error {
	if s.Relation == "" {
		s.Relation = RelationSync
	}
	if s.Relation != RelationSync && s.Relation != RelationAsync {
		return fmt.Errorf("span %q has invalid relation %q (want %q or %q)",
			s.SpanID, s.Relation, RelationSync, RelationAsync)
	}
	return nil
}

// Duration returns end-start. Negative values are preserved: a negative
// duration is a clock contradiction diagnosed at analysis time, never
// silently rewritten.
func (s *Span) Duration() int64 {
	return s.EndTime - s.StartTime
}

// Trace is a set of spans sharing a trace id.
type Trace struct {
	TraceID string `json:"trace_id"`
	Spans   []Span `json:"spans"`
}
