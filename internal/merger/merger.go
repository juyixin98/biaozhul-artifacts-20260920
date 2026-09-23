// Package merger assembles raw, single-line log events into multiline
// entries (e.g. stack traces) using a start-line rule.
//
// Each log source keeps an independent pending buffer: lines from one
// source can never be merged into another source's entry. A pending entry
// is finalized when:
//
//   - the next start line for the same source arrives ("next_start"),
//   - no line arrived within the configured timeout ("timeout"),
//   - the source's process id changes ("restart"),
//   - the entry reaches the configured byte budget ("bytes_limit"),
//   - the merger is shut down ("shutdown") or force-flushed ("forced"),
//
// Lines that do not match the start rule while no entry is pending are
// emitted on their own as incomplete "orphan" entries.
package merger

import (
	"errors"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// Finalization reasons stored on Entry.Reason.
const (
	ReasonNextStart  = "next_start"  // flushed because the next start line arrived
	ReasonTimeout    = "timeout"     // flushed by the timeout sweeper
	ReasonRestart    = "restart"     // source process id changed
	ReasonBytesLimit = "bytes_limit" // byte budget exhausted
	ReasonShutdown   = "shutdown"    // merger closed
	ReasonForced     = "forced"      // explicit force flush
	ReasonOrphan     = "orphan"      // continuation line without a start line
)

// Line is one raw log line received from a source.
type Line struct {
	Source string    `json:"source"`
	PID    *int      `json:"pid,omitempty"`
	Text   string    `json:"text"`
	Ts     time.Time `json:"-"`
}

// Entry is an assembled multiline log record.
type Entry struct {
	ID           int64     `json:"id,omitempty"`
	Source       string    `json:"source"`
	StartTime    time.Time `json:"start_time"`
	EndTime      time.Time `json:"end_time"`
	Text         string    `json:"text"`
	LineCount    int       `json:"line_count"`
	Bytes        int       `json:"bytes"`
	Complete     bool      `json:"complete"`
	Reason       string    `json:"reason,omitempty"`
	Truncated    bool      `json:"truncated,omitempty"`
	DroppedLines int       `json:"dropped_lines,omitempty"`
	DroppedBytes int       `json:"dropped_bytes,omitempty"`
}

// Config configures a Merger.
type Config struct {
	// StartRule matches lines that begin a new multiline entry.
	StartRule *regexp.Regexp
	// Timeout after which a quiet pending entry is flushed.
	Timeout time.Duration
	// MaxBytes is the maximum size in bytes an entry's text may reach.
	MaxBytes int
	// Now is the clock; time.Now when nil.
	Now func() time.Time
	// OnFlush receives every finalized entry; no-op when nil.
	OnFlush func(Entry)
	// SweepInterval starts a background sweeper; zero means no
	// background goroutine (sweep must be driven manually, useful in
	// tests).
	SweepInterval time.Duration
}

// Merger is safe for concurrent use.
type Merger struct {
	cfg     Config
	mu      sync.Mutex
	sources map[string]*srcState
	stopCh  chan struct{}
	wg      sync.WaitGroup
	started bool
}

type srcState struct {
	pid *int
	cur *builder
}

type builder struct {
	source       string
	start, end   time.Time
	lines        []string
	bytes        int
	overflow     bool // byte budget hit; further lines are dropped
	truncated    bool
	droppedLines int
	droppedBytes int
}

// New creates a Merger and, when SweepInterval > 0, starts its
// background timeout sweeper.
func New(cfg Config) *Merger {
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.OnFlush == nil {
		cfg.OnFlush = func(Entry) {}
	}
	m := &Merger{
		cfg:     cfg,
		sources: map[string]*srcState{},
		stopCh:  make(chan struct{}),
	}
	if cfg.SweepInterval > 0 {
		m.started = true
		m.wg.Add(1)
		go m.sweepLoop()
	}
	return m
}

func (m *Merger) now() time.Time { return m.cfg.Now() }

// Ingest processes one log line.
func (m *Merger) Ingest(l Line) error {
	if strings.TrimSpace(l.Source) == "" {
		return errors.New("merger: line has empty source")
	}
	if l.Text == "" {
		return errors.New("merger: line has empty text")
	}
	if l.Ts.IsZero() {
		l.Ts = m.now()
	}
	isStart := m.cfg.StartRule.MatchString(l.Text)

	var flushed []Entry
	m.mu.Lock()
	st := m.sources[l.Source]
	if st == nil {
		st = &srcState{}
		m.sources[l.Source] = st
	}

	// A changed pid means the emitting process restarted: the pending
	// entry can never be completed and is flushed as-is.
	if st.cur != nil && l.PID != nil && st.pid != nil && *l.PID != *st.pid {
		flushed = append(flushed, st.cur.finish(ReasonRestart, false))
		st.cur = nil
	}
	if l.PID != nil {
		st.pid = l.PID
	}

	switch {
	case isStart:
		if st.cur != nil {
			flushed = append(flushed, st.cur.flushReason())
			st.cur = nil
		}
		b := &builder{source: l.Source, start: l.Ts, end: l.Ts}
		b.append(l.Text, m.cfg.MaxBytes)
		st.cur = b
	case st.cur != nil:
		st.cur.handleContinuation(l.Text, l.Ts, m.cfg.MaxBytes)
	default:
		// Continuation line without a pending start line: emit it on
		// its own so nothing is silently swallowed.
		b := &builder{source: l.Source, start: l.Ts, end: l.Ts}
		b.append(l.Text, m.cfg.MaxBytes)
		flushed = append(flushed, b.finish(ReasonOrphan, false))
	}
	m.mu.Unlock()

	for _, e := range flushed {
		m.cfg.OnFlush(e)
	}
	return nil
}

// append adds a line, truncating it at the byte budget. Truncation
// never splits a UTF-8 rune: the cut point is walked back to the last
// rune boundary.
func (b *builder) append(text string, maxBytes int) {
	sep := 0
	if len(b.lines) > 0 {
		sep = 1
	}
	budget := maxBytes - b.bytes - sep
	switch {
	case budget <= 0:
		b.droppedLines++
		b.droppedBytes += len(text)
		b.overflow = true
	case len(text) > budget:
		kept := truncateUTF8(text, budget)
		b.lines = append(b.lines, kept)
		b.bytes += len(kept) + sep
		b.truncated = true
		b.droppedBytes += len(text) - len(kept)
		b.overflow = true
	default:
		b.lines = append(b.lines, text)
		b.bytes += len(text) + sep
	}
}

// truncateUTF8 returns the longest prefix of s whose byte length is <=
// max and that ends on a rune boundary.
func truncateUTF8(s string, max int) string {
	if max >= len(s) {
		return s
	}
	if max <= 0 {
		return ""
	}
	end := max
	for end > 0 && !utf8.RuneStart(s[end]) {
		end--
	}
	return s[:end]
}

func (b *builder) handleContinuation(text string, ts time.Time, maxBytes int) {
	b.end = ts
	if b.overflow {
		b.droppedLines++
		b.droppedBytes += len(text)
		return
	}
	b.append(text, maxBytes)
}

// flushReason picks the finalization reason for a builder displaced by a
// new start line.
func (b *builder) flushReason() Entry {
	if b.overflow {
		return b.finish(ReasonBytesLimit, false)
	}
	return b.finish(ReasonNextStart, true)
}

func (b *builder) finish(reason string, complete bool) Entry {
	text := strings.Join(b.lines, "\n")
	return Entry{
		Source:       b.source,
		StartTime:    b.start,
		EndTime:      b.end,
		Text:         text,
		LineCount:    len(b.lines),
		Bytes:        len(text),
		Complete:     complete,
		Reason:       reason,
		Truncated:    b.truncated,
		DroppedLines: b.droppedLines,
		DroppedBytes: b.droppedBytes,
	}
}

// Sweep finalizes pending entries that have been idle for longer than
// the timeout. It is safe to call even when a background sweeper runs.
func (m *Merger) Sweep() {
	m.sweepAt(m.now())
}

func (m *Merger) sweepAt(now time.Time) {
	var flushed []Entry
	m.mu.Lock()
	for source, st := range m.sources {
		if st.cur == nil {
			continue
		}
		if now.Sub(st.cur.end) < m.cfg.Timeout {
			continue
		}
		if st.cur.overflow {
			flushed = append(flushed, st.cur.finish(ReasonBytesLimit, false))
		} else {
			flushed = append(flushed, st.cur.finish(ReasonTimeout, true))
		}
		st.cur = nil
		delete(m.sources, source)
	}
	m.mu.Unlock()
	for _, e := range flushed {
		m.cfg.OnFlush(e)
	}
}

// ForceFlush finalizes every pending entry immediately. Flushed entries
// are marked incomplete with reason "forced" (or "bytes_limit"). It
// returns the number of entries flushed.
func (m *Merger) ForceFlush() int {
	var flushed []Entry
	m.mu.Lock()
	for _, st := range m.sources {
		if st.cur == nil {
			continue
		}
		if st.cur.overflow {
			flushed = append(flushed, st.cur.finish(ReasonBytesLimit, false))
		} else {
			flushed = append(flushed, st.cur.finish(ReasonForced, false))
		}
		st.cur = nil
	}
	m.mu.Unlock()
	for _, e := range flushed {
		m.cfg.OnFlush(e)
	}
	return len(flushed)
}

// Close stops the background sweeper and flushes all remaining pending
// entries as incomplete "shutdown" entries.
func (m *Merger) Close() {
	if m.started {
		close(m.stopCh)
		m.wg.Wait()
	}
	var flushed []Entry
	m.mu.Lock()
	for _, st := range m.sources {
		if st.cur == nil {
			continue
		}
		if st.cur.overflow {
			flushed = append(flushed, st.cur.finish(ReasonBytesLimit, false))
		} else {
			flushed = append(flushed, st.cur.finish(ReasonShutdown, false))
		}
		st.cur = nil
	}
	m.sources = map[string]*srcState{}
	m.mu.Unlock()
	for _, e := range flushed {
		m.cfg.OnFlush(e)
	}
}

func (m *Merger) sweepLoop() {
	defer m.wg.Done()
	t := time.NewTicker(m.cfg.SweepInterval)
	defer t.Stop()
	for {
		select {
		case <-m.stopCh:
			return
		case <-t.C:
			m.sweepAt(m.now())
		}
	}
}
