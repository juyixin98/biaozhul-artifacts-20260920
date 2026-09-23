package queue

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestExponentialBackoff(t *testing.T) {
	b := ExponentialBackoff{Base: 100 * time.Millisecond, Factor: 2, Max: time.Second}
	cases := []struct {
		attempt int
		want    time.Duration
	}{
		{1, 0},
		{2, 100 * time.Millisecond},
		{3, 200 * time.Millisecond},
		{4, 400 * time.Millisecond},
		{5, 800 * time.Millisecond},
		{6, time.Second}, // capped
		{100, time.Second},
	}
	for _, c := range cases {
		if got := b.Backoff(c.attempt); got != c.want {
			t.Errorf("Backoff(%d) = %v, want %v", c.attempt, got, c.want)
		}
	}
}

func TestConstantBackoff(t *testing.T) {
	b := ConstantBackoff(7 * time.Second)
	if got := b.Backoff(1); got != 7*time.Second {
		t.Fatalf("got %v", got)
	}
	if got := b.Backoff(99); got != 7*time.Second {
		t.Fatalf("got %v", got)
	}
}

func TestMemorySinkRingAndEvents(t *testing.T) {
	s := NewMemorySink(2)
	s.Emit(Event{Type: EventStarted, JobID: "a"})
	s.Emit(Event{Type: EventSucceeded, JobID: "b"})
	s.Emit(Event{Type: EventFailed, JobID: "c"})
	evs := s.Events()
	if len(evs) != 2 {
		t.Fatalf("ring capacity not honored: %d", len(evs))
	}
	if evs[0].JobID != "b" || evs[1].JobID != "c" {
		t.Fatalf("oldest should be dropped, got %s %s", evs[0].JobID, evs[1].JobID)
	}
	if !s.WaitFor(2, time.Second) {
		t.Fatal("WaitFor should already be satisfied")
	}
}

func TestMultiSink(t *testing.T) {
	a, b := NewMemorySink(0), NewMemorySink(0)
	m := MultiSink{a, b}
	m.Emit(Event{Type: EventStarted, JobID: "x"})
	if a.Len() != 1 || b.Len() != 1 {
		t.Fatal("MultiSink must fan out to all sinks")
	}
}

func TestJSONLSinkRoundTrip(t *testing.T) {
	var buf bytes.Buffer
	s := NewJSONLSink(&buf)
	s.Emit(Event{Type: EventSubmitted, JobID: "j", Detail: map[string]any{"k": "v"}})
	line := strings.TrimSpace(buf.String())
	if line == "" {
		t.Fatal("no record written")
	}
	var got Event
	if err := json.Unmarshal([]byte(line), &got); err != nil {
		t.Fatalf("record not JSON: %v", err)
	}
	if got.Type != EventSubmitted || got.JobID != "j" {
		t.Fatalf("round trip mismatch: %+v", got)
	}
}

func TestDurationJSON(t *testing.T) {
	var d Duration
	if err := json.Unmarshal([]byte(`"250ms"`), &d); err != nil {
		t.Fatalf("string unmarshal: %v", err)
	}
	if d.Std() != 250*time.Millisecond {
		t.Fatalf("got %v", d.Std())
	}
	if err := json.Unmarshal([]byte(`1000000`), &d); err != nil {
		t.Fatalf("int unmarshal: %v", err)
	}
	if d.Std() != time.Millisecond {
		t.Fatalf("got %v", d.Std())
	}
	if b, err := json.Marshal(Duration(3 * time.Second)); err != nil || string(b) != `"3s"` {
		t.Fatalf("marshal = %s err=%v", b, err)
	}
	var bad Duration
	if err := json.Unmarshal([]byte(`true`), &bad); err == nil {
		t.Fatal("bool must fail to unmarshal as Duration")
	}
}
