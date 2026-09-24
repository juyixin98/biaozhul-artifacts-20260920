// Package broker implements a durable, monotonically-ordered SSE event log
// with bounded retention and live fan-out to subscribers.
package broker

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// Event is one stored SSE event. IDs are positive and strictly increasing in
// publication order. Data may contain arbitrary bytes, including newlines;
// the sse package is responsible for wire encoding.
type Event struct {
	ID        int64     `json:"id"`
	Event     string    `json:"event,omitempty"`
	Data      string    `json:"data"`
	Timestamp time.Time `json:"ts"`
}

// Stats is a point-in-time snapshot of broker state.
type Stats struct {
	LastID      int64 `json:"last_id"`
	OldestID    int64 `json:"oldest_id"`
	Retained    int   `json:"retained"`
	Subscribers int   `json:"subscribers"`
	Dropped     int64 `json:"dropped"`
	MaxEvents   int   `json:"max_events"`
}

// Config configures a Broker opened with Open.
type Config struct {
	Dir       string // data directory (created if missing)
	MaxEvents int    // retained-event bound; <=0 means unlimited
	QueueSize int    // per-subscriber live-event buffer; <=0 uses 128
	NoSync    bool   // skip fsync per event (tests/demo only; off by default)
}

// ErrClosed is returned by Publish after Close.
var ErrClosed = errors.New("broker: closed")

// Subscription is a live tail returned by Subscribe. Events with IDs greater
// than the Subscribe-time head arrive on C until Unsubscribe or Close.
type Subscription struct {
	C   <-chan Event
	sub *sub
}

type sub struct {
	ch     chan Event
	done   bool
	mu     sync.Mutex // guards done while ch sends happen outside broker lock
	broker *Broker
}

// Broker owns the durable log and subscriber set.
type Broker struct {
	mu        sync.RWMutex
	dir       string
	maxEvents int
	queueSize int
	noSync    bool

	f       *os.File
	logPath string
	events  []Event
	lastID  int64
	closed  bool

	subs    map[*sub]struct{}
	dropped int64
}

const (
	logFileName = "events.log"
	tmpSuffix   = ".tmp"
)

// Open loads (or creates) the durable log in cfg.Dir and applies retention.
func Open(cfg Config) (*Broker, error) {
	if cfg.Dir == "" {
		return nil, errors.New("broker: empty Dir")
	}
	if err := os.MkdirAll(cfg.Dir, 0o755); err != nil {
		return nil, fmt.Errorf("broker: create data dir: %w", err)
	}
	if cfg.QueueSize <= 0 {
		cfg.QueueSize = 128
	}

	b := &Broker{
		dir:       cfg.Dir,
		maxEvents: cfg.MaxEvents,
		queueSize: cfg.QueueSize,
		noSync:    cfg.NoSync,
		logPath:   filepath.Join(cfg.Dir, logFileName),
		subs:      make(map[*sub]struct{}),
	}

	if err := b.loadLocked(); err != nil {
		return nil, err
	}
	// Retention is applied on load too, so a lowered bound takes effect
	// immediately on restart.
	if b.maxEvents > 0 && len(b.events) > b.maxEvents {
		if err := b.compactLocked(b.maxEvents); err != nil {
			return nil, err
		}
	}
	return b, nil
}

// loadLocked reads the whole JSONL log into memory and rebuilds lastID.
// A truncated final line (partial write) is ignored.
func (b *Broker) loadLocked() error {
	f, err := os.OpenFile(b.logPath, os.O_RDWR|os.O_CREATE, 0o644)
	if err != nil {
		return fmt.Errorf("broker: open log: %w", err)
	}
	b.f = f

	scanner := bufio.NewScanner(f)
	// Events are user supplied; allow large lines.
	scanner.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)
	var loaded []Event
	lineNo := 0
	for scanner.Scan() {
		lineNo++
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var e Event
		if err := json.Unmarshal(line, &e); err != nil {
			return fmt.Errorf("broker: log line %d corrupt: %w", lineNo, err)
		}
		// The first retained id is the baseline (the log may have been
		// compacted, so it need not be 1); every following id must be +1.
		if e.ID <= 0 {
			return fmt.Errorf("broker: log line %d has non-positive id %d", lineNo, e.ID)
		}
		if b.lastID != 0 && e.ID != b.lastID+1 {
			return fmt.Errorf("broker: log line %d id gap (want %d, got %d)", lineNo, b.lastID+1, e.ID)
		}
		if e.Timestamp.IsZero() {
			e.Timestamp = time.Now()
		}
		loaded = append(loaded, e)
		b.lastID = e.ID
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("broker: read log: %w", err)
	}
	b.events = loaded

	// Reposition the handle at EOF for subsequent appends.
	if _, err := f.Seek(0, io.SeekEnd); err != nil {
		return fmt.Errorf("broker: seek log: %w", err)
	}
	return nil
}

// Publish durably appends an event and fans it out to live subscribers.
// The assigned monotonic ID is returned. No gap can appear: the ID is only
// consumed after the durable append succeeds.
func (b *Broker) Publish(name, data string) (Event, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed {
		return Event{}, ErrClosed
	}
	e := Event{ID: b.lastID + 1, Event: name, Data: data, Timestamp: time.Now().UTC()}
	if err := b.appendLocked(e); err != nil {
		return Event{}, err
	}
	b.events = append(b.events, e)
	b.lastID = e.ID

	if b.maxEvents > 0 && len(b.events) > b.maxEvents {
		if err := b.compactLocked(b.maxEvents); err != nil {
			// Publish already succeeded; compaction failure must not lose it.
			log.Printf("sse-resume: compaction failed: %v", err)
		}
	}

	for s := range b.subs {
		select {
		case s.ch <- e:
		default:
			b.dropSubscriberLocked(s)
		}
	}
	return e, nil
}

// appendLocked serializes one event as a JSON line, fsyncs unless disabled.
func (b *Broker) appendLocked(e Event) error {
	buf, err := json.Marshal(e)
	if err != nil {
		return fmt.Errorf("broker: encode event: %w", err)
	}
	buf = append(buf, '\n')
	if _, err := b.f.Write(buf); err != nil {
		return fmt.Errorf("broker: append log: %w", err)
	}
	if !b.noSync {
		if err := b.f.Sync(); err != nil {
			return fmt.Errorf("broker: fsync log: %w", err)
		}
	}
	return nil
}

// compactLocked rewrites the log keeping only the newest keep events.
func (b *Broker) compactLocked(keep int) error {
	cut := len(b.events) - keep
	if cut < 0 {
		cut = 0
	}
	survivors := b.events[cut:]

	tmpPath := b.logPath + tmpSuffix
	tmp, err := os.OpenFile(tmpPath, os.O_RDWR|os.O_CREATE|os.O_TRUNC, 0o644)
	if err != nil {
		return fmt.Errorf("broker: create compaction temp: %w", err)
	}
	w := bufio.NewWriterSize(tmp, 64*1024)
	enc := json.NewEncoder(w)
	for _, e := range survivors {
		if err := enc.Encode(e); err != nil {
			tmp.Close()
			os.Remove(tmpPath)
			return fmt.Errorf("broker: rewrite log: %w", err)
		}
	}
	if err := w.Flush(); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return fmt.Errorf("broker: flush compaction: %w", err)
	}
	if !b.noSync {
		if err := tmp.Sync(); err != nil {
			tmp.Close()
			os.Remove(tmpPath)
			return fmt.Errorf("broker: fsync compaction: %w", err)
		}
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("broker: close compaction: %w", err)
	}
	if err := os.Rename(tmpPath, b.logPath); err != nil {
		return fmt.Errorf("broker: install compacted log: %w", err)
	}
	// fsync the directory so the rename is durable.
	if dir, derr := os.Open(b.dir); derr == nil {
		_ = dir.Sync()
		_ = dir.Close()
	}

	f, err := os.OpenFile(b.logPath, os.O_RDWR|os.O_APPEND, 0o644)
	if err != nil {
		return fmt.Errorf("broker: reopen compacted log: %w", err)
	}
	old := b.f
	b.f = f
	b.events = survivors
	_ = old.Close()
	return nil
}

// Replay returns retained events with id > after, in ascending order.
// ok is false when after is older than the oldest retained event (the caller
// must request a reset); ahead is true when after points beyond the log head
// (impossible / corrupt cursor).
func (b *Broker) Replay(after int64) (events []Event, oldest, last int64, ok, ahead bool) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	oldest, last = b.oldestLocked(), b.lastID
	// Cursor 0 means "no client state": stream from the oldest retained event.
	if after == 0 {
		out := make([]Event, len(b.events))
		copy(out, b.events)
		return out, oldest, last, true, false
	}
	if after < oldest-1 {
		return nil, oldest, last, false, false
	}
	if after > last {
		return nil, oldest, last, false, true
	}
	idx := sort.Search(len(b.events), func(i int) bool { return b.events[i].ID > after })
	out := make([]Event, 0, len(b.events)-idx)
	out = append(out, b.events[idx:]...)
	return out, oldest, last, true, false
}

// CheckCursor classifies a resume cursor against the retained window.
func (b *Broker) CheckCursor(after int64) (oldest, last int64, status CursorStatus) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	oldest, last = b.oldestLocked(), b.lastID
	switch {
	case after == 0:
		// Fresh client: always resumable from the oldest retained event.
		return oldest, last, CursorOK
	case after < oldest-1:
		return oldest, last, CursorExpired
	case after > last:
		return oldest, last, CursorAhead
	default:
		return oldest, last, CursorOK
	}
}

// CursorStatus is the result of CheckCursor.
type CursorStatus int

// Cursor classification results.
const (
	CursorOK      CursorStatus = iota
	CursorExpired              // older than retention; reset required
	CursorAhead                // beyond head; cannot resume
)

// Subscribe registers a live tail and returns the subscription plus the
// current head ID. The channel is buffered (Config.QueueSize); a subscriber
// whose buffer fills is dropped and its channel closed.
//
// Handlers must Subscribe BEFORE replaying with the returned head: the live
// channel can only ever deliver id > head, which makes the replay/live seam
// gap-free (with possible duplicates at the seam).
func (b *Broker) Subscribe() (*Subscription, int64) {
	s := &sub{
		ch:     make(chan Event, b.queueSize),
		broker: b,
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed {
		// Close ch via the normal path so range-loops terminate.
		s.done = true
		close(s.ch)
	} else {
		b.subs[s] = struct{}{}
	}
	return &Subscription{C: s.ch, sub: s}, b.lastID
}

// Unsubscribe removes a live tail and closes its channel. Safe to call once.
func (b *Broker) Unsubscribe(s *Subscription) {
	if s == nil {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if _, exists := b.subs[s.sub]; exists {
		delete(b.subs, s.sub)
		s.sub.markDone()
		close(s.sub.ch)
	}
}

// dropSubscriberLocked evicts an over-buffered slow consumer.
// Note the ch send already happened under b.mu, so close-after-send ordering
// is guaranteed for that event.
func (b *Broker) dropSubscriberLocked(s *sub) {
	if _, exists := b.subs[s]; !exists {
		return
	}
	delete(b.subs, s)
	b.dropped++
	s.mu.Lock()
	s.done = true
	s.mu.Unlock()
	close(s.ch)
	log.Printf("sse-resume: dropping slow subscriber (queue=%d full, total dropped=%d)", b.queueSize, b.dropped)
}

func (s *sub) markDone() {
	s.mu.Lock()
	s.done = true
	s.mu.Unlock()
}

func (b *Broker) oldestLocked() int64 {
	if len(b.events) == 0 {
		return b.lastID + 1
	}
	return b.events[0].ID
}

// Stats returns a point-in-time snapshot.
func (b *Broker) Stats() Stats {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return Stats{
		LastID:      b.lastID,
		OldestID:    b.oldestLocked(),
		Retained:    len(b.events),
		Subscribers: len(b.subs),
		Dropped:     b.dropped,
		MaxEvents:   b.maxEvents,
	}
}

// Close flushes, closes the log file and terminates all subscriptions.
func (b *Broker) Close() error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed {
		return nil
	}
	b.closed = true
	for s := range b.subs {
		delete(b.subs, s)
		s.markDone()
		close(s.ch)
	}
	var err error
	if b.f != nil {
		err = b.f.Close()
		b.f = nil
	}
	return err
}
