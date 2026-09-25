package merger

import (
	"regexp"
	"strings"
	"testing"
	"time"
)

var startRe = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}`)

// collector gathers flushed entries.
type collector struct{ entries []Entry }

func (c *collector) add(e Entry) { c.entries = append(c.entries, e) }

func TestInterleavedSourcesDoNotMix(t *testing.T) {
	var c collector
	m := New(Config{StartPattern: startRe}, c.add)
	base := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)

	// Interleave a Java stack trace (source A) with a Go panic (source B).
	lines := []struct{ src, line string }{
		{"a", "2026-09-24 10:00:00 ERROR NullPointerException"},
		{"b", "2026-09-24 10:00:00 INFO panic: runtime error"},
		{"a", "\tat com.example.Foo.bar(Foo.java:42)"},
		{"b", "\tgoroutine 1 [running]:"},
		{"a", "\tat com.example.Main.main(Main.java:10)"},
		{"b", "\tmain.main()"},
		{"a", "2026-09-24 10:00:01 INFO recovered"},
		{"b", "2026-09-24 10:00:01 INFO restarted"},
	}
	for i, l := range lines {
		m.Add(l.src, l.line, base.Add(time.Duration(i)*time.Millisecond))
	}
	m.FlushAll(ReasonShutdown)

	if len(c.entries) != 4 {
		t.Fatalf("expected 4 entries, got %d", len(c.entries))
	}
	// Entry 1: source A stack trace, complete, 3 lines.
	e := c.entries[0]
	if e.Source != "a" || e.LineCount != 3 || !e.Complete || e.Reason != ReasonNewStart {
		t.Fatalf("entry0 wrong: %+v", e)
	}
	if !strings.Contains(e.Message, "Foo.java:42") || strings.Contains(e.Message, "goroutine") {
		t.Fatalf("entry0 message contaminated: %q", e.Message)
	}
	// Entry 2: source B panic, complete, 3 lines.
	e = c.entries[1]
	if e.Source != "b" || e.LineCount != 3 || !e.Complete {
		t.Fatalf("entry1 wrong: %+v", e)
	}
	if !strings.Contains(e.Message, "goroutine 1") || strings.Contains(e.Message, "Foo.java") {
		t.Fatalf("entry1 message contaminated: %q", e.Message)
	}
	// Trailing single-line entries.
	if c.entries[2].Source != "a" || c.entries[2].Message != "2026-09-24 10:00:01 INFO recovered" {
		t.Fatalf("entry2 wrong: %+v", c.entries[2])
	}
	if c.entries[3].Source != "b" {
		t.Fatalf("entry3 wrong: %+v", c.entries[3])
	}
}

func TestLinesWithoutStartLine(t *testing.T) {
	var c collector
	m := New(Config{StartPattern: startRe}, c.add)
	base := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)

	// Continuation lines with no prior start line: buffered, then flushed
	// (incomplete) when the first real start line arrives.
	m.Add("a", "\tat nowhere(Unknown:0)", base)
	m.Add("a", "\t... 3 more", base.Add(time.Millisecond))
	m.Add("a", "2026-09-24 10:00:01 INFO fresh start", base.Add(2*time.Millisecond))
	m.FlushAll(ReasonShutdown)

	if len(c.entries) != 2 {
		t.Fatalf("expected 2 entries, got %d", len(c.entries))
	}
	e := c.entries[0]
	if e.HasStartLine || e.Complete || e.LineCount != 2 {
		t.Fatalf("start-less entry should be incomplete: %+v", e)
	}
	if e.Reason != ReasonNewStart {
		t.Fatalf("expected reason new_start, got %q", e.Reason)
	}
	if !c.entries[1].HasStartLine || c.entries[1].Reason != ReasonShutdown {
		t.Fatalf("second entry should have start line, shutdown reason: %+v", c.entries[1])
	}
}

func TestMaxBytesTruncation(t *testing.T) {
	var c collector
	m := New(Config{StartPattern: startRe, MaxBytes: 60}, c.add)
	base := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)

	m.Add("a", "2026-09-24 10:00:00 ERROR huge", base) // 30 bytes
	m.Add("a", "\tat frame.one(One.java:1)", base.Add(time.Millisecond))
	m.Add("a", "\tat frame.two(Two.java:2)", base.Add(2*time.Millisecond))
	m.FlushAll(ReasonShutdown)

	if len(c.entries) < 2 {
		t.Fatalf("expected at least 2 entries, got %d", len(c.entries))
	}
	first := c.entries[0]
	if first.Complete || first.Reason != ReasonMaxBytes {
		t.Fatalf("first entry should be incomplete with max_bytes reason: %+v", first)
	}
	if first.ByteSize > 60 {
		t.Fatalf("entry exceeds max bytes: %d", first.ByteSize)
	}
	if !strings.Contains(first.Message, "frame.one") {
		t.Fatalf("first entry lost content: %q", first.Message)
	}
}

func TestTimeoutFlush(t *testing.T) {
	var c collector
	now := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	m := New(Config{
		StartPattern: startRe,
		FlushTimeout: time.Second,
		Now:          func() time.Time { return now },
	}, c.add)

	m.Add("a", "2026-09-24 10:00:00 ERROR boom", now)
	m.Add("a", "\tat x.y(Z.java:1)", now.Add(100*time.Millisecond))
	// Source B is still active; only A goes stale.
	m.Add("b", "2026-09-24 10:00:02 INFO alive", now.Add(2*time.Second))

	m.FlushStale(now.Add(2 * time.Second).Add(-time.Second))

	if len(c.entries) != 1 {
		t.Fatalf("expected 1 flushed entry, got %d", len(c.entries))
	}
	e := c.entries[0]
	if e.Source != "a" || e.Complete || e.Reason != ReasonTimeout {
		t.Fatalf("timeout flush wrong: %+v", e)
	}
	if m.PendingCount() != 1 {
		t.Fatalf("source b should still be pending, pending=%d", m.PendingCount())
	}
}

func TestShutdownFlush(t *testing.T) {
	var c collector
	m := New(Config{StartPattern: startRe}, c.add)
	now := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	m.Add("a", "2026-09-24 10:00:00 ERROR unfinished", now)
	m.FlushAll(ReasonShutdown)
	if len(c.entries) != 1 || c.entries[0].Complete || c.entries[0].Reason != ReasonShutdown {
		t.Fatalf("shutdown flush wrong: %+v", c.entries)
	}
	if m.PendingCount() != 0 {
		t.Fatalf("pending should be empty after FlushAll")
	}
}
