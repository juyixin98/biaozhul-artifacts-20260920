package sse

import (
	"bytes"
	"strings"
	"testing"
	"time"

	"sse-resume/internal/broker"
)

type capture struct{}

func (c *capture) Flush() error { return nil }

func encode(t *testing.T, e broker.Event) string {
	t.Helper()
	var buf bytes.Buffer
	if err := WriteFrame(&buf, &capture{}, e); err != nil {
		t.Fatalf("WriteFrame: %v", err)
	}
	return buf.String()
}

func TestWriteFrameSingleLine(t *testing.T) {
	got := encode(t, broker.Event{ID: 42, Event: "msg", Data: "hello", Timestamp: time.Now()})
	want := "id: 42\nevent: msg\ndata: hello\n\n"
	if got != want {
		t.Fatalf("frame = %q, want %q", got, want)
	}
}

func TestWriteFrameMultiLine(t *testing.T) {
	// Multi-line payload: one data: directive per line, blank line terminator.
	got := encode(t, broker.Event{ID: 7, Data: "line1\nline2\nline3"})
	want := "id: 7\ndata: line1\ndata: line2\ndata: line3\n\n"
	if got != want {
		t.Fatalf("frame = %q, want %q", got, want)
	}

	// Trailing newline => final empty data: line (spec round-trips it).
	got = encode(t, broker.Event{ID: 8, Data: "a\nb\n"})
	want = "id: 8\ndata: a\ndata: b\ndata: \n\n"
	if got != want {
		t.Fatalf("trailing-nl frame = %q, want %q", got, want)
	}
}

func TestWriteFrameCRLFNormalized(t *testing.T) {
	got := encode(t, broker.Event{ID: 9, Data: "x\r\ny\rz"})
	want := "id: 9\ndata: x\ndata: y\ndata: z\n\n"
	if got != want {
		t.Fatalf("CRLF frame = %q, want %q", got, want)
	}
}

func TestValidateEventName(t *testing.T) {
	for _, ok := range []string{"", "msg", "user.created", "order-42", "x y"} {
		if err := ValidateEventName(ok); err != nil {
			t.Fatalf("ValidateEventName(%q) = %v, want nil", ok, err)
		}
	}
	for _, bad := range []string{"a\nb", "cr\r", "nul\x00"} {
		if err := ValidateEventName(bad); err == nil {
			t.Fatalf("ValidateEventName(%q) = nil, want error", bad)
		}
	}
}

func TestResetFrameHasNoID(t *testing.T) {
	var buf bytes.Buffer
	if err := WriteReset(&buf, &capture{}, ResetCursorExpired, 5, 11); err != nil {
		t.Fatalf("WriteReset: %v", err)
	}
	got := buf.String()
	if !strings.HasPrefix(got, "event: reset\n") {
		t.Fatalf("reset frame does not start with event: %q", got)
	}
	if strings.Contains(got, "id:") {
		t.Fatalf("reset frame must not carry id line: %q", got)
	}
	if !strings.Contains(got, `"oldest_id":5`) || !strings.Contains(got, `"last_id":11`) {
		t.Fatalf("reset frame missing window metadata: %q", got)
	}
	if !strings.HasSuffix(got, "\n\n") {
		t.Fatalf("reset frame not terminated: %q", got)
	}
}

func TestHeartbeatIsComment(t *testing.T) {
	var buf bytes.Buffer
	if err := WriteHeartbeat(&buf, &capture{}); err != nil {
		t.Fatalf("WriteHeartbeat: %v", err)
	}
	if got := buf.String(); got != ": ping\n\n" {
		t.Fatalf("heartbeat = %q", got)
	}
}
