// Package model defines the wire format and assembled-trace data model.
//
// IMPORTANT: timestamps (StartUnixNano/EndUnixNano) are wall-clock readings
// reported by potentially unsynchronised service clocks. They are NEVER used
// to infer causality — parent/child relations come exclusively from the
// parentSpanId reference. Timestamps are only inspected to flag clock skew.
package model

// Span is a single completed operation within a distributed trace.
// Attributes is a free-form string map so synthetic payloads need no schema.
type Span struct {
	TraceID       string            `json:"traceId"`
	SpanID        string            `json:"spanId"`
	ParentSpanID  string            `json:"parentSpanId"`
	ServiceName   string            `json:"serviceName"`
	Name          string            `json:"name"`
	StartUnixNano int64             `json:"startUnixNano"`
	EndUnixNano   int64             `json:"endUnixNano"`
	Attributes    map[string]string `json:"attributes,omitempty"`
}

// ClockSkewWarning flags a parent->child edge whose reported wall-clock
// timestamps violate the expected nesting, beyond the configured tolerance.
// This is informational: causality is still determined by references only.
type ClockSkewWarning struct {
	ParentID         string `json:"parentId"`
	ChildID          string `json:"childId"`
	ParentService    string `json:"parentService,omitempty"`
	ChildService     string `json:"childService,omitempty"`
	ChildStartNanos  int64  `json:"childStartUnixNano"`
	ParentStartNanos int64  `json:"parentStartUnixNano"`
	StartDeltaNanos  int64  `json:"startDeltaNanos"`
	ToleranceNanos   int64  `json:"toleranceNanos"`
	Kind             string `json:"kind"` // "child-starts-before-parent" or "child-ends-after-parent"
}

// SpanNode is a span plus its assembled position in the trace tree.
// Cycle members keep Children populated with the in-trace edges as reported;
// whether the parent edge participates in a cycle is signalled via the
// analysis result, not by mutating the node.
type SpanNode struct {
	Span         Span        `json:"span"`
	Depth        int         `json:"depth"`
	Path         []string    `json:"path"` // span ids from a forest root down to this node
	Children     []*SpanNode `json:"children"`
	Orphan       bool        `json:"orphan,omitempty"`       // parent referenced but absent from the trace
	InCycle      bool        `json:"inCycle,omitempty"`      // parent chain participates in a cycle
	CycleEntryID string      `json:"cycleEntryId,omitempty"` // canonical entry of that cycle
}

// Conflict records a duplicate spanId whose payload differs from the span
// already stored. The first-seen payload always wins.
type Conflict struct {
	SpanID string `json:"spanId"`
	// ReceivedIngestSeq is the sequence number of the rejected duplicate.
	ReceivedIngestSeq int64    `json:"receivedIngestSeq"`
	ExistingIngestSeq int64    `json:"existingIngestSeq"`
	DifferingFields   []string `json:"differingFields"`
}

// Revision is an immutable assembled view of a trace at one point in time.
// Revisions are append-only; later revisions' SpanID sets are supersets of
// earlier ones (see Assembler.CheckRevisionContainment).
type Revision struct {
	Version         int                `json:"version"`
	Reason          string             `json:"reason"`
	AtUnixNano      int64              `json:"atUnixNano"`
	IngestSeq       int64              `json:"ingestSeq"` // assembler sequence that produced it
	SpanIDs         []string           `json:"spanIds"`
	RootSpanID      string             `json:"rootSpanId,omitempty"`
	Complete        bool               `json:"complete"`
	MissingRoot     bool               `json:"missingRoot"`
	HasOrphans      bool               `json:"hasOrphans"`
	HasCycles       bool               `json:"hasCycles"`
	CyclePath       []string           `json:"cyclePath,omitempty"`
	ClockSkew       []ClockSkewWarning `json:"clockSkew,omitempty"`
	ConflictSpanIDs []string           `json:"conflictSpanIds,omitempty"`
}

// TraceView is the full queryable state for one trace: the canonical spans,
// the assembled forest, conflicts and the full revision history.
type TraceView struct {
	TraceID   string      `json:"traceId"`
	Sealed    bool        `json:"sealed"` // sealed by timeout; further spans are "late"
	Complete  bool        `json:"complete"`
	Latest    *Revision   `json:"latestRevision,omitempty"`
	Spans     []Span      `json:"spans"`
	Forest    []*SpanNode `json:"forest"` // canonical roots, then orphan/cycle roots
	Conflicts []Conflict  `json:"conflicts,omitempty"`
	Revisions []Revision  `json:"revisions"`
}
