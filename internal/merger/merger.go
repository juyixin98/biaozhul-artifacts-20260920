// Package merger assembles multi-line log records from raw lines.
//
// Lines are grouped per source. A line matching the configured start
// pattern opens a new record; any other line is appended to the source's
// current pending record. A pending record is flushed when:
//
//   - a new start line arrives for the same source (Complete=true, "new_start")
//   - it has been idle longer than FlushTimeout (Complete=false, "timeout")
//   - appending a line would exceed MaxBytes (Complete=false, "max_bytes")
//   - the merger is shut down (Complete=false, "shutdown")
//
// Buffers are strictly per-source: interleaved lines from different
// sources never mix.
package merger

import (
	"regexp"
	"sync"
	"time"
)

// Flush reasons recorded on Entry.Reason.
const (
	ReasonNewStart = "new_start"
	ReasonTimeout  = "timeout"
	ReasonMaxBytes = "max_bytes"
	ReasonShutdown = "shutdown"
)

// Entry is one assembled multi-line log record.
type Entry struct {
	Source    string    `json:"source"`
	Message   string    `json:"message"`
	StartTime time.Time `json:"start_time"`
	EndTime   time.Time `json:"end_time"`
	LineCount int       `json:"line_count"`
	ByteSize  int       `json:"byte_size"`
	// Complete is false when the record was cut short by timeout,
	// byte limit or shutdown, or when it never began with a start line.
	Complete bool   `json:"complete"`
	Reason   string `json:"reason"`
	// HasStartLine reports whether the record opened with a line
	// matching the start pattern.
	HasStartLine bool `json:"has_start_line"`
}

// Config controls the Merger.
type Config struct {
	// StartPattern marks the first line of a new record
	// (e.g. a timestamp prefix).
	StartPattern *regexp.Regexp
	// MaxBytes caps the assembled message size; 0 means unlimited.
	MaxBytes int
	// FlushTimeout flushes a pending record after this much idle time;
	// 0 disables timeout flushing.
	FlushTimeout time.Duration
	// Now supplies the current time; defaults to time.Now.
	// Tests inject a fake clock.
	Now func() time.Time
}

type pending struct {
	lines    []string
	size     int
	start    time.Time
	last     time.Time
	hasStart bool
}

// Merger holds per-source pending records.
type Merger struct {
	cfg     Config
	mu      sync.Mutex
	pending map[string]*pending
	onFlush func(Entry)
}

// New creates a Merger; onFlush is called for every completed record.
func New(cfg Config, onFlush func(Entry)) *Merger {
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	return &Merger{
		cfg:     cfg,
		pending: make(map[string]*pending),
		onFlush: onFlush,
	}
}

// Add ingests one raw line for a source at time ts.
func (m *Merger) Add(source, line string, ts time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()

	isStart := m.cfg.StartPattern != nil && m.cfg.StartPattern.MatchString(line)
	p := m.pending[source]

	if isStart {
		if p != nil {
			m.flushLocked(source, p, true, ReasonNewStart)
		}
		m.pending[source] = &pending{
			lines:    []string{line},
			size:     len(line),
			start:    ts,
			last:     ts,
			hasStart: true,
		}
		return
	}

	// Continuation line.
	if p == nil {
		p = &pending{start: ts, hasStart: false}
		m.pending[source] = p
	}
	addSize := len(line)
	if len(p.lines) > 0 {
		addSize++ // joining newline
	}
	if m.cfg.MaxBytes > 0 && p.size+addSize > m.cfg.MaxBytes {
		// Byte limit exceeded: flush what we have as incomplete and
		// start a fresh (start-less) record with the current line.
		m.flushLocked(source, p, false, ReasonMaxBytes)
		m.pending[source] = &pending{
			lines:    []string{line},
			size:     len(line),
			start:    ts,
			last:     ts,
			hasStart: false,
		}
		return
	}
	p.lines = append(p.lines, line)
	p.size += addSize
	p.last = ts
	if len(p.lines) == 1 {
		p.start = ts
	}
}

// FlushStale flushes every pending record idle since cutoff, marking it
// incomplete with reason "timeout".
func (m *Merger) FlushStale(cutoff time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for src, p := range m.pending {
		if !p.last.After(cutoff) {
			m.flushLocked(src, p, false, ReasonTimeout)
		}
	}
}

// FlushAll flushes every pending record with the given reason and
// complete=false. Used on shutdown.
func (m *Merger) FlushAll(reason string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for src, p := range m.pending {
		m.flushLocked(src, p, false, reason)
	}
}

// PendingCount reports how many sources have an open record (for tests
// and diagnostics).
func (m *Merger) PendingCount() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.pending)
}

func (m *Merger) flushLocked(source string, p *pending, complete bool, reason string) {
	msg := ""
	for i, l := range p.lines {
		if i > 0 {
			msg += "\n"
		}
		msg += l
	}
	e := Entry{
		Source:       source,
		Message:      msg,
		StartTime:    p.start,
		EndTime:      p.last,
		LineCount:    len(p.lines),
		ByteSize:     p.size,
		Complete:     complete && p.hasStart,
		Reason:       reason,
		HasStartLine: p.hasStart,
	}
	delete(m.pending, source)
	if m.onFlush != nil {
		m.onFlush(e)
	}
}
