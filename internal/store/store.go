// Package store implements the durable, append-only event log.
//
// Events are appended to a binary write-ahead log on disk, one length-prefixed
// JSON record per line:
//
//	<8-byte big-endian payload length><payload JSON>
//
// Each payload is {"id":<uint64>,"ts":<unix nano>,"data":"..."}.
//
// When retention evicts the oldest events, the WAL file is rewritten
// (compaction) and the new file is atomically renamed into place. All events
// still fit in memory for this single-stream service; the WAL exists so that
// replay survives a process restart.
package store

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Event is one stored SSE event.
type Event struct {
	ID   uint64
	Time time.Time
	Data string
}

// record is the on-disk JSON encoding of an Event.
type record struct {
	ID   uint64 `json:"id"`
	TS   int64  `json:"ts"`
	Data string `json:"data"`
}

// Options configures retention of the log.
type Options struct {
	// Dir is the directory that holds events.log (created if missing).
	Dir string
	// MaxEvents retains at most this many newest events. 0 = unlimited count.
	MaxEvents int
	// MaxAge evicts events older than the duration on publish. 0 = no age limit.
	MaxAge time.Duration
	// Sync, when true, fsyncs after every append (test-safe durability).
	Sync bool
	// CompactEventDelta triggers a file rewrite once compaction would remove
	// at least this many evicted events (amortises rewrites).
	CompactEventDelta int
	// CompactAgeDelta triggers a rewrite once evicted bytes cross this size.
	CompactAgeDelta int64
}

// Store is a mutex-protected in-memory slice backed by a WAL file.
type Store struct {
	opts Options

	mu     sync.Mutex
	events []Event
	nextID uint64 // monotonically increasing; lastID = nextID-1

	file       *os.File
	evictedCnt int   // events dropped since last rewrite
	evictedByt int64 // bytes of records dropped since last rewrite
}

// Open loads (or creates) the log in opts.Dir.
func Open(opts Options) (*Store, error) {
	if opts.Dir == "" {
		return nil, errors.New("store: Dir is required")
	}
	if err := os.MkdirAll(opts.Dir, 0o755); err != nil {
		return nil, fmt.Errorf("store: mkdir %s: %w", opts.Dir, err)
	}
	path := filepath.Join(opts.Dir, "events.log")
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o644)
	if err != nil {
		return nil, fmt.Errorf("store: open %s: %w", path, err)
	}
	s := &Store{opts: opts, file: f, nextID: 1}
	if err := s.load(); err != nil {
		_ = f.Close()
		return nil, err
	}
	// File may contain trailing garbage from a torn write; shrink to good end.
	pos, err := f.Seek(0, io.SeekCurrent)
	if err != nil {
		_ = f.Close()
		return nil, err
	}
	if err := f.Truncate(pos); err != nil {
		_ = f.Close()
		return nil, err
	}
	// Apply retention at startup in case limits were lowered between runs.
	if err := s.applyRetentionLocked(); err != nil {
		_ = f.Close()
		return nil, err
	}
	return s, nil
}

// load replays the WAL into memory, stopping at (and rewinding past) the first
// malformed/torn record.
func (s *Store) load() error {
	if _, err := s.file.Seek(0, io.SeekStart); err != nil {
		return err
	}
	var hdr [8]byte
	for {
		recStart, err := s.file.Seek(0, io.SeekCurrent)
		if err != nil {
			return err
		}
		if _, err := io.ReadFull(s.file, hdr[:]); err != nil {
			if errors.Is(err, io.EOF) {
				break // clean EOF: position is already at end
			}
			if errors.Is(err, io.ErrUnexpectedEOF) {
				// Torn header: ReadFull advanced to EOF; rewind so the
				// garbage bytes get truncated away.
				if _, serr := s.file.Seek(recStart, io.SeekStart); serr != nil {
					return serr
				}
				break
			}
			return fmt.Errorf("store: read length: %w", err)
		}
		n := binary.BigEndian.Uint64(hdr[:])
		if n > 64*1024*1024 {
			// Implausibly large record: treat the tail as corrupt and rewind.
			if _, serr := s.file.Seek(recStart, io.SeekStart); serr != nil {
				return serr
			}
			break
		}
		buf := make([]byte, n)
		if _, err := io.ReadFull(s.file, buf); err != nil {
			if _, serr := s.file.Seek(recStart, io.SeekStart); serr != nil {
				return serr
			}
			break
		}
		var rec record
		if err := json.Unmarshal(buf, &rec); err != nil {
			if _, serr := s.file.Seek(recStart, io.SeekStart); serr != nil {
				return serr
			}
			break
		}
		ev := Event{ID: rec.ID, Time: time.Unix(0, rec.TS).UTC(), Data: rec.Data}
		if rec.ID >= s.nextID {
			s.nextID = rec.ID + 1
		}
		s.events = append(s.events, ev)
	}
	return nil
}

// Append stores a new event with the next monotonic ID, applies retention and
// returns the stored event.
func (s *Store) Append(data string, now time.Time) (Event, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	ev := Event{ID: s.nextID, Time: now.UTC(), Data: data}
	s.nextID++
	if err := s.writeRecordLocked(ev); err != nil {
		return Event{}, err
	}
	s.events = append(s.events, ev)
	if err := s.applyRetentionLocked(); err != nil {
		return Event{}, err
	}
	return ev, nil
}

func (s *Store) writeRecordLocked(ev Event) error {
	payload, err := json.Marshal(record{ID: ev.ID, TS: ev.Time.UnixNano(), Data: ev.Data})
	if err != nil {
		return fmt.Errorf("store: marshal: %w", err)
	}
	var hdr [8]byte
	binary.BigEndian.PutUint64(hdr[:], uint64(len(payload)))
	if _, err := s.file.Write(hdr[:]); err != nil {
		return fmt.Errorf("store: write header: %w", err)
	}
	if _, err := s.file.Write(payload); err != nil {
		return fmt.Errorf("store: write payload: %w", err)
	}
	if s.opts.Sync {
		if err := s.file.Sync(); err != nil {
			return fmt.Errorf("store: sync: %w", err)
		}
	}
	return nil
}

// Since returns up to limit retained events with ID strictly greater than
// afterID, in ascending order. limit <= 0 means no cap.
func (s *Store) Since(afterID uint64, limit int) []Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	i := indexAfter(s.events, afterID)
	if i >= len(s.events) {
		return nil
	}
	out := s.events[i:]
	if limit > 0 && len(out) > limit {
		out = out[:limit]
	}
	// Return a copy: callers must not be able to mutate the log slice.
	cp := make([]Event, len(out))
	copy(cp, out)
	return cp
}

// Bounds describes the retained ID window. Empty is true when no events exist.
type Bounds struct {
	Empty  bool
	Oldest uint64 // ID of the oldest retained event (0 when Empty)
	Last   uint64 // ID of the newest retained event (0 when Empty)
}

// Bounds returns the current retained window.
func (s *Store) Bounds() Bounds {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.events) == 0 {
		return Bounds{Empty: true}
	}
	return Bounds{Oldest: s.events[0].ID, Last: s.events[len(s.events)-1].ID}
}

// Count returns the number of retained events.
func (s *Store) Count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.events)
}

// Close flushes and closes the WAL.
func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.opts.Sync {
		_ = s.file.Sync()
	}
	return s.file.Close()
}

// indexAfter returns the first index whose ID > afterID using binary search.
// Event IDs are strictly ascending and contiguous except across compactions.
func indexAfter(events []Event, afterID uint64) int {
	lo, hi := 0, len(events)
	for lo < hi {
		mid := int(uint(lo+hi) >> 1)
		if events[mid].ID <= afterID {
			lo = mid + 1
		} else {
			hi = mid
		}
	}
	return lo
}

// applyRetentionLocked evicts expired/over-count events and rewrites the WAL
// when enough garbage has accumulated.
func (s *Store) applyRetentionLocked() error {
	cut := 0
	if s.opts.MaxAge > 0 {
		threshold := time.Now().UTC().Add(-s.opts.MaxAge)
		for cut < len(s.events) && s.events[cut].Time.Before(threshold) {
			cut++
		}
	}
	if s.opts.MaxEvents > 0 {
		extra := len(s.events) - s.opts.MaxEvents
		if extra > cut {
			cut = extra
		}
	}
	if cut == 0 {
		return nil
	}
	// Approximate evicted bytes (8-byte header per record + JSON payload).
	for _, ev := range s.events[:cut] {
		s.evictedByt += int64(8+len(ev.Data)) + 64
	}
	s.evictedCnt += cut
	s.events = s.events[cut:]

	deltaN := s.opts.CompactEventDelta
	if deltaN == 0 {
		deltaN = 256
	}
	deltaB := s.opts.CompactAgeDelta
	if deltaB == 0 {
		deltaB = 1 << 20
	}
	if s.evictedCnt >= deltaN || s.evictedByt >= deltaB {
		return s.rewriteLocked()
	}
	return nil
}

// rewriteLocked atomically rewrites the WAL from the retained slice.
func (s *Store) rewriteLocked() error {
	dir := s.opts.Dir
	tmp, err := os.CreateTemp(dir, "events.log.tmp-*")
	if err != nil {
		return fmt.Errorf("store: compact create: %w", err)
	}
	tmpName := tmp.Name()
	cleanup := func() { _ = os.Remove(tmpName) }

	w := io.Writer(tmp)
	for _, ev := range s.events {
		payload, err := json.Marshal(record{ID: ev.ID, TS: ev.Time.UnixNano(), Data: ev.Data})
		if err != nil {
			_ = tmp.Close()
			cleanup()
			return fmt.Errorf("store: compact marshal: %w", err)
		}
		var hdr [8]byte
		binary.BigEndian.PutUint64(hdr[:], uint64(len(payload)))
		if _, err := tmp.Write(hdr[:]); err != nil {
			_ = tmp.Close()
			cleanup()
			return fmt.Errorf("store: compact write: %w", err)
		}
		if _, err := w.Write(payload); err != nil {
			_ = tmp.Close()
			cleanup()
			return fmt.Errorf("store: compact write: %w", err)
		}
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		cleanup()
		return fmt.Errorf("store: compact sync: %w", err)
	}
	if err := tmp.Close(); err != nil {
		cleanup()
		return err
	}
	finalName := filepath.Join(dir, "events.log")
	if err := os.Rename(tmpName, finalName); err != nil {
		cleanup()
		return fmt.Errorf("store: compact rename: %w", err)
	}
	// Reopen for future appends; directory fsync is skipped for portability.
	f, err := os.OpenFile(finalName, os.O_RDWR|os.O_APPEND, 0o644)
	if err != nil {
		return fmt.Errorf("store: compact reopen: %w", err)
	}
	old := s.file
	s.file = f
	s.evictedCnt = 0
	s.evictedByt = 0
	_ = old.Close()
	return nil
}
