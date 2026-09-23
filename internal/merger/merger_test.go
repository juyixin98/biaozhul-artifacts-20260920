package merger

import (
	"regexp"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
	"unicode/utf8"
)

var testRule = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T`)

type fakeClock struct{ t time.Time }

func (c *fakeClock) now() time.Time                    { return c.t }
func (c *fakeClock) advance(d time.Duration) time.Time { c.t = c.t.Add(d); return c.t }

func newTestMerger(t *testing.T, timeout time.Duration, maxBytes int) (*Merger, *fakeClock, *[]Entry) {
	t.Helper()
	clk := &fakeClock{t: time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)}
	var out []Entry
	var outMu sync.Mutex
	m := New(Config{
		StartRule: testRule,
		Timeout:   timeout,
		MaxBytes:  maxBytes,
		Now:       clk.now,
		OnFlush: func(e Entry) {
			outMu.Lock()
			out = append(out, e)
			outMu.Unlock()
		},
	})
	t.Cleanup(m.Close)
	return m, clk, &out
}

func mustIngest(t *testing.T, m *Merger, ts time.Time, source string, pid *int, text string) {
	t.Helper()
	if err := m.Ingest(Line{Source: source, PID: pid, Text: text, Ts: ts}); err != nil {
		t.Fatalf("ingest %q/%q: %v", source, text, err)
	}
}

func TestStartLineAssemblyAndNextStart(t *testing.T) {
	m, clk, out := newTestMerger(t, 5*time.Second, 4096)
	mustIngest(t, m, clk.t, "s", nil, "2026-09-24T10:00:00 header")
	mustIngest(t, m, clk.advance(time.Second), "s", nil, "  frame 1")
	mustIngest(t, m, clk.advance(time.Second), "s", nil, "  frame 2")
	mustIngest(t, m, clk.advance(time.Second), "s", nil, "2026-09-24T10:00:03 next header")

	if len(*out) != 1 {
		t.Fatalf("flushed %d entries, want 1", len(*out))
	}
	e := (*out)[0]
	if !e.Complete || e.Reason != ReasonNextStart {
		t.Fatalf("got complete=%v reason=%q, want complete next_start", e.Complete, e.Reason)
	}
	if e.LineCount != 3 || !strings.Contains(e.Text, "frame 2") {
		t.Fatalf("unexpected entry: %+v", e)
	}

	clk.advance(6 * time.Second)
	m.Sweep()
	if len(*out) != 2 {
		t.Fatalf("after sweep flushed %d entries, want 2", len(*out))
	}
	tail := (*out)[1]
	if !tail.Complete || tail.Reason != ReasonTimeout || tail.LineCount != 1 {
		t.Fatalf("tail = %+v, want complete timeout with 1 line", tail)
	}
}

func TestPerSourceIndependentBuffersNoMixing(t *testing.T) {
	m, clk, out := newTestMerger(t, time.Minute, 4096)
	pa, pb := 1, 2
	// Interleave two stack traces line by line.
	mustIngest(t, m, clk.t, "alpha", &pa, "2026-09-24T10:00:00 A-header")
	mustIngest(t, m, clk.advance(time.Second), "beta", &pb, "2026-09-24T10:00:01 B-header")
	mustIngest(t, m, clk.advance(time.Second), "alpha", &pa, "  A-frame-1")
	mustIngest(t, m, clk.advance(time.Second), "beta", &pb, "  B-frame-1")
	mustIngest(t, m, clk.advance(time.Second), "alpha", &pa, "  A-frame-2")
	mustIngest(t, m, clk.advance(time.Second), "beta", &pb, "  B-frame-2")
	mustIngest(t, m, clk.advance(time.Second), "alpha", &pa, "2026-09-24T10:00:06 A-header2")
	mustIngest(t, m, clk.advance(time.Second), "beta", &pb, "2026-09-24T10:00:07 B-header2")

	if len(*out) != 2 {
		t.Fatalf("flushed %d entries, want 2", len(*out))
	}
	for _, e := range *out {
		for _, foreign := range []string{"A-frame", "B-frame"} {
			own := "A-frame"
			if e.Source == "beta" {
				own = "B-frame"
			}
			lines := strings.Split(e.Text, "\n")
			for _, ln := range lines {
				if strings.Contains(ln, foreign) && foreign != own {
					t.Fatalf("entry for %q contains foreign line %q: %q", e.Source, ln, e.Text)
				}
			}
		}
		if e.LineCount != 3 {
			t.Fatalf("entry for %q has %d lines, want 3", e.Source, e.LineCount)
		}
	}
}

func TestOrphanLineWithoutStart(t *testing.T) {
	m, clk, out := newTestMerger(t, time.Minute, 4096)
	mustIngest(t, m, clk.t, "s", nil, "  stray continuation")
	if len(*out) != 1 {
		t.Fatalf("flushed %d, want 1 orphan", len(*out))
	}
	e := (*out)[0]
	if e.Complete || e.Reason != ReasonOrphan || e.LineCount != 1 {
		t.Fatalf("orphan = %+v", e)
	}

	// A following proper start begins a fresh pending entry.
	mustIngest(t, m, clk.advance(time.Second), "s", nil, "2026-09-24T10:00:01 header")
	mustIngest(t, m, clk.advance(time.Second), "s", nil, "  frame")
	if len(*out) != 1 {
		t.Fatalf("start line should not flush orphan-era entries")
	}
}

func TestTimeoutFlushesComplete(t *testing.T) {
	m, clk, out := newTestMerger(t, 2*time.Second, 4096)
	mustIngest(t, m, clk.t, "s", nil, "2026-09-24T10:00:00 header")
	mustIngest(t, m, clk.advance(time.Second), "s", nil, "  frame")

	clk.advance(2 * time.Second) // idle 2s => timeout boundary reached
	m.Sweep()
	if len(*out) != 1 {
		t.Fatalf("flushed %d, want 1", len(*out))
	}
	e := (*out)[0]
	if !e.Complete || e.Reason != ReasonTimeout {
		t.Fatalf("got %+v, want complete timeout", e)
	}
}

func TestRestartByPIDChange(t *testing.T) {
	m, clk, out := newTestMerger(t, time.Minute, 4096)
	old, newPID := 10, 11
	mustIngest(t, m, clk.t, "s", &old, "2026-09-24T10:00:00 header")
	mustIngest(t, m, clk.advance(time.Second), "s", &old, "  old-frame")
	// Continuation from the new process: old pending entry is flushed
	// as restart, and the new line is itself an orphan.
	mustIngest(t, m, clk.advance(time.Second), "s", &newPID, "  frame from restarted proc")
	if len(*out) != 2 {
		t.Fatalf("flushed %d entries, want restart+orphan = 2", len(*out))
	}
	oldEntry := (*out)[0]
	if oldEntry.Complete || oldEntry.Reason != ReasonRestart || oldEntry.LineCount != 2 {
		t.Fatalf("restart entry = %+v", oldEntry)
	}
	orphan := (*out)[1]
	if orphan.Complete || orphan.Reason != ReasonOrphan {
		t.Fatalf("orphan after restart = %+v", orphan)
	}

	// New start from new pid is unaffected.
	mustIngest(t, m, clk.advance(time.Second), "s", &newPID, "2026-09-24T10:00:03 new header")
	mustIngest(t, m, clk.advance(time.Second), "s", &newPID, "  new-frame")
	if len(*out) != 2 {
		t.Fatalf("new entry should stay pending")
	}
}

func TestRestartStartLineFlushesOldAndOpensNew(t *testing.T) {
	m, clk, out := newTestMerger(t, time.Minute, 4096)
	old, newPID := 10, 11
	mustIngest(t, m, clk.t, "s", &old, "2026-09-24T10:00:00 old header")
	mustIngest(t, m, clk.advance(time.Second), "s", &newPID, "2026-09-24T10:00:01 new header")
	if len(*out) != 1 {
		t.Fatalf("flushed %d, want 1", len(*out))
	}
	e := (*out)[0]
	if e.Complete || e.Reason != ReasonRestart {
		t.Fatalf("want incomplete restart, got %+v", e)
	}
}

func TestBytesLimitSingleOversizedLine(t *testing.T) {
	m, clk, out := newTestMerger(t, time.Minute, 32)
	huge := "2026-09-24T10:00:00 " + strings.Repeat("Z", 100)
	mustIngest(t, m, clk.t, "s", nil, huge)

	// Overflow entry is only finalized on timeout/close/restart/next
	// start; closing here flushes it as bytes_limit.
	m.Close()

	if len(*out) != 1 {
		t.Fatalf("flushed %d, want 1", len(*out))
	}
	e := (*out)[0]
	if e.Complete || e.Reason != ReasonBytesLimit {
		t.Fatalf("got %+v, want incomplete bytes_limit", e)
	}
	if !e.Truncated {
		t.Fatalf("truncated flag not set: %+v", e)
	}
	if e.Bytes > 32 {
		t.Fatalf("entry text bytes %d exceeds budget 32", e.Bytes)
	}
	if e.DroppedBytes == 0 {
		t.Fatalf("dropped bytes not recorded: %+v", e)
	}
}

func TestBytesLimitDropsFurtherFramesFromSameSource(t *testing.T) {
	m, clk, out := newTestMerger(t, time.Minute, 32)
	mustIngest(t, m, clk.t, "s", nil, "2026-09-24T10:00:00 "+strings.Repeat("Y", 40))
	mustIngest(t, m, clk.advance(time.Second), "s", nil, "  continuation after overflow")
	mustIngest(t, m, clk.advance(time.Second), "s", nil, "  another frame")

	clk.advance(3 * time.Minute)
	m.Sweep()
	if len(*out) != 1 {
		t.Fatalf("flushed %d, want 1", len(*out))
	}
	e := (*out)[0]
	if e.Reason != ReasonBytesLimit || e.DroppedLines != 2 {
		t.Fatalf("got %+v, want bytes_limit with 2 dropped lines", e)
	}
	if strings.Contains(e.Text, "continuation after overflow") {
		t.Fatalf("dropped frame leaked into text: %q", e.Text)
	}
}

func TestTruncateUTF8NeverSplitsRune(t *testing.T) {
	// "中" is 3 bytes; a 20-byte timestamp header followed by runes.
	m, clk, out := newTestMerger(t, time.Minute, 24)
	mustIngest(t, m, clk.t, "s", nil, "2026-09-24T10:00:00 "+strings.Repeat("中", 20))
	m.Close()

	if len(*out) != 1 {
		t.Fatalf("flushed %d, want 1", len(*out))
	}
	e := (*out)[0]
	if e.Bytes > 24 {
		t.Fatalf("bytes %d > 24", e.Bytes)
	}
	if !utf8.ValidString(e.Text) {
		t.Fatalf("truncated text is not valid UTF-8: % x", e.Text)
	}
}

func TestCloseFlushesPendingAsShutdown(t *testing.T) {
	clk := &fakeClock{t: time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)}
	var out []Entry
	m := New(Config{StartRule: testRule, Timeout: time.Minute, MaxBytes: 4096,
		Now: clk.now, OnFlush: func(e Entry) { out = append(out, e) }})
	mustIngest(t, m, clk.t, "s", nil, "2026-09-24T10:00:00 header")
	mustIngest(t, m, clk.advance(time.Second), "s", nil, "  frame")
	m.Close()
	if len(out) != 1 {
		t.Fatalf("flushed %d, want 1", len(out))
	}
	if out[0].Complete || out[0].Reason != ReasonShutdown {
		t.Fatalf("got %+v, want incomplete shutdown", out[0])
	}
}

func TestForceFlush(t *testing.T) {
	m, clk, _ := newTestMerger(t, time.Minute, 4096)
	var got []Entry
	m.cfg.OnFlush = func(e Entry) { got = append(got, e) }
	mustIngest(t, m, clk.t, "s", nil, "2026-09-24T10:00:00 header")
	if n := m.ForceFlush(); n != 1 {
		t.Fatalf("ForceFlush returned %d, want 1", n)
	}
	if got[0].Complete || got[0].Reason != ReasonForced {
		t.Fatalf("got %+v, want incomplete forced", got[0])
	}
	// Idempotent: nothing pending now.
	if n := m.ForceFlush(); n != 0 {
		t.Fatalf("second ForceFlush returned %d, want 0", n)
	}
}

func TestInvalidLinesRejected(t *testing.T) {
	m, _, _ := newTestMerger(t, time.Minute, 4096)
	if err := m.Ingest(Line{Source: "  ", Text: "x"}); err == nil {
		t.Fatal("empty source accepted")
	}
	if err := m.Ingest(Line{Source: "s", Text: ""}); err == nil {
		t.Fatal("empty text accepted")
	}
}

func TestConcurrentIngestIsSafeAndPerSource(t *testing.T) {
	m, _, _ := newTestMerger(t, time.Minute, 1<<20)
	var wg sync.WaitGroup
	for src := 0; src < 8; src++ {
		wg.Add(1)
		go func(src int) {
			defer wg.Done()
			source := string(rune('a' + src))
			for i := 0; i < 200; i++ {
				mustIngest(t, m, time.Now(), source, nil,
					"2026-09-24T10:00:00 "+source+"-header-"+strconv.Itoa(i))
				_ = m.Ingest(Line{Source: source, Text: "  " + source + "-frame-" + strconv.Itoa(i)})
			}
		}(src)
	}
	wg.Wait()
	m.ForceFlush()
}
