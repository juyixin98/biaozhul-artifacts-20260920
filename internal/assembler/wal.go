package assembler

import "tracestitch/internal/model"

// Event kinds recorded in the WAL.
const (
	EventSpan    = "span"    // a span was accepted
	EventTimeout = "timeout" // an incomplete trace was sealed by Sweep
)

// WALRecord is one entry in the replay log.
//
// For EventSpan (the default when Event is empty), Span carries the payload.
// For EventTimeout, TraceID names the trace that was sealed. The assigned
// ingest sequence and receive timestamp are persisted in both cases so a
// replay reproduces revision versions, reasons and timing identically.
type WALRecord struct {
	Event            string      `json:"event,omitempty"`
	IngestSeq        int64       `json:"ingestSeq"`
	ReceivedUnixNano int64       `json:"receivedUnixNano"`
	TraceID          string      `json:"traceId,omitempty"` // EventTimeout
	Span             *model.Span `json:"span,omitempty"`    // EventSpan
}
