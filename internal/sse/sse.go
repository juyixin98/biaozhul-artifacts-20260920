// Package sse encodes events using the Server-Sent Events wire format
// (https://html.spec.whatwg.org/multipage/server-sent-events.html).
package sse

import (
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"sse-resume/internal/broker"
)

// flusher is implemented by the server's frame sink: after a complete block
// is written, Flush pushes it onto the wire immediately.
type flusher interface {
	Flush() error
}

// ErrInvalidEvent is returned when an event name contains illegal characters.
var ErrInvalidEvent = errors.New("sse: invalid event name (must not contain CR/LF or NUL)")

// ValidateEventName rejects names that could break the wire framing.
// Names are written into an "event:" field, so CR and LF are forbidden;
// NUL is rejected defensively.
func ValidateEventName(name string) error {
	if name == "" {
		return nil
	}
	if strings.ContainsAny(name, "\r\n\x00") {
		return ErrInvalidEvent
	}
	return nil
}

// splitDataLines normalizes the data payload into the text lines that must
// follow "data:" directives: CR and CRLF are normalized to LF, then split.
func splitDataLines(data string) []string {
	data = strings.ReplaceAll(data, "\r\n", "\n")
	data = strings.ReplaceAll(data, "\r", "\n")
	return strings.Split(data, "\n")
}

// WriteFrame writes one full SSE block (terminated by a blank line) and
// flushes it. Multi-line data is emitted as multiple data: directives per
// the spec, so an EventSource reconstructs the original payload with "\n".
func WriteFrame(w io.Writer, f flusher, e broker.Event) error {
	var b strings.Builder
	fmt.Fprintf(&b, "id: %d\n", e.ID)
	if e.Event != "" {
		fmt.Fprintf(&b, "event: %s\n", e.Event)
	}
	for _, line := range splitDataLines(e.Data) {
		fmt.Fprintf(&b, "data: %s\n", line)
	}
	b.WriteByte('\n')
	if _, err := io.WriteString(w, b.String()); err != nil {
		return err
	}
	return f.Flush()
}

// WriteHeartbeat sends an SSE comment line, which keeps the connection alive
// but is ignored by EventSource dispatchers.
func WriteHeartbeat(w io.Writer, f flusher) error {
	if _, err := io.WriteString(w, ": ping\n\n"); err != nil {
		return err
	}
	return f.Flush()
}

// ResetReason values appear in the reset frame's "reason:" field.
const (
	ResetCursorExpired = "cursor_expired"
	ResetCursorAhead   = "cursor_ahead"
)

// WriteReset tells the client its Last-Event-ID cannot be resumed from the
// retained window and that a full re-synchronization is required. The frame
// deliberately carries NO id: line, so an EventSource does not update its
// lastEventId and does not loop replaying it. The server closes right after.
func WriteReset(w io.Writer, f flusher, reason string, oldest, last int64) error {
	var b strings.Builder
	b.WriteString("event: reset\n")
	fmt.Fprintf(&b, "data: {\"reason\":%q,\"oldest_id\":%d,\"last_id\":%d,\"at\":%q}\n",
		reason, oldest, last, time.Now().UTC().Format(time.RFC3339))
	b.WriteString("data: resume impossible from the requested cursor; perform a full resync\n\n")
	if _, err := io.WriteString(w, b.String()); err != nil {
		return err
	}
	return f.Flush()
}

// WriteNotice emits a named, non-resumable control frame (no id: line).
func WriteNotice(w io.Writer, f flusher, name, detail string) error {
	var b strings.Builder
	fmt.Fprintf(&b, "event: %s\n", name)
	for _, line := range splitDataLines(detail) {
		fmt.Fprintf(&b, "data: %s\n", line)
	}
	b.WriteByte('\n')
	if _, err := io.WriteString(w, b.String()); err != nil {
		return err
	}
	return f.Flush()
}
