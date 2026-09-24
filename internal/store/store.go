// Package store persists received mail messages as JSON files with atomic
// commit semantics. Each message is written to a temporary file first and
// then renamed into place, so a crash during a write can never leave a
// partially recorded message behind.
package store

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"sync"
	"time"
)

// ErrNotFound is returned when no message with the given ID exists.
var ErrNotFound = errors.New("message not found")

// Message is one fully completed mail transaction.
type Message struct {
	ID         string    `json:"id"`
	From       string    `json:"from"`
	To         []string  `json:"to"`
	Data       string    `json:"data"` // raw message body with CRLF line endings
	Bytes      int       `json:"bytes"`
	ReceivedAt time.Time `json:"received_at"`
}

// Store is a file-backed, concurrency-safe collection of messages.
type Store struct {
	dir     string
	mu      sync.RWMutex
	ids     []string // ordered, oldest first
	byID    map[string]*Message
	counter int64 // monotonic nanosecond counter; immune to clock jumps
}

// Open opens (or creates) a store rooted at dir.
func Open(dir string) (*Store, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create store directory: %w", err)
	}
	s := &Store{dir: dir, byID: map[string]*Message{}}

	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("read store directory: %w", err)
	}
	var maxNano int64
	for _, e := range entries {
		name := e.Name()
		// Remove stale temp files from an interrupted process; they never
		// represent committed messages.
		if filepath.Ext(name) == ".tmp" {
			_ = os.Remove(filepath.Join(dir, name))
			continue
		}
		if filepath.Ext(name) != ".json" || !e.Type().IsRegular() {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			return nil, fmt.Errorf("read %s: %w", name, err)
		}
		var m Message
		if err := json.Unmarshal(raw, &m); err != nil {
			// Skip unparseable files rather than taking the whole receiver down.
			fmt.Fprintf(os.Stderr, "store: skipping unreadable file %s: %v\n", name, err)
			continue
		}
		if m.ID == "" || m.ID != name[:len(name)-len(".json")] {
			fmt.Fprintf(os.Stderr, "store: skipping file with mismatched id: %s\n", name)
			continue
		}
		if n := nanoOf(m.ID); n > maxNano {
			maxNano = n
		}
		if _, dup := s.byID[m.ID]; dup {
			continue
		}
		s.byID[m.ID] = &m
		s.ids = append(s.ids, m.ID)
	}
	sort.Slice(s.ids, func(i, j int) bool {
		return nanoOf(s.ids[i]) < nanoOf(s.ids[j])
	})
	s.counter = maxNano
	return s, nil
}

// nanoOf extracts the monotonic nanosecond prefix embedded in an ID.
func nanoOf(id string) int64 {
	for i, ch := range id {
		if ch == '-' {
			n, _ := strconv.ParseInt(id[:i], 10, 64)
			return n
		}
	}
	return 0
}

// nextID returns a strictly increasing nanosecond value plus random suffix.
// The nanosecond prefix is maintained on a monotonic counter so that wall
// clock jumps backwards can never reorder or collide IDs.
// Caller must hold s.mu.
func (s *Store) nextID() (int64, string, error) {
	var b [12]byte
	if _, err := rand.Read(b[:]); err != nil {
		return 0, "", err
	}
	n := s.counter + 1
	if now := time.Now().UnixNano(); now > n {
		n = now
	}
	s.counter = n
	return n, hex.EncodeToString(b[:]), nil
}

// Add records a completed message atomically: the JSON payload is first
// written to a temp file and fsynced, then renamed to its final name.
func (s *Store) Add(from string, to []string, data string) (*Message, error) {
	s.mu.Lock()
	nano, randHex, err := s.nextID()
	if err != nil {
		s.mu.Unlock()
		return nil, err
	}
	id := fmt.Sprintf("%d-%s", nano, randHex)
	m := &Message{
		ID:         id,
		From:       from,
		To:         append([]string(nil), to...),
		Data:       data,
		Bytes:      len(data),
		ReceivedAt: time.Now().UTC(),
	}
	s.mu.Unlock()

	final := filepath.Join(s.dir, id+".json")
	tmp := filepath.Join(s.dir, id+".tmp")

	payload, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return nil, err
	}

	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return nil, err
	}
	if _, err := f.Write(payload); err != nil {
		f.Close()
		os.Remove(tmp)
		return nil, err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		os.Remove(tmp)
		return nil, err
	}
	if err := f.Close(); err != nil {
		os.Remove(tmp)
		return nil, err
	}
	if err := os.Rename(tmp, final); err != nil {
		os.Remove(tmp)
		return nil, err
	}

	s.mu.Lock()
	if _, ok := s.byID[id]; !ok {
		s.byID[id] = m
		s.ids = append(s.ids, id)
	}
	s.mu.Unlock()
	return m, nil
}

// List returns up to limit stored messages, newest first. A limit <= 0 means
// no limit. The returned Data field is always empty in a listing.
func (s *Store) List(limit int) []*Message {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]*Message, 0, len(s.ids))
	for i := len(s.ids) - 1; i >= 0; i-- {
		m := s.byID[s.ids[i]]
		cp := *m
		cp.Data = ""
		out = append(out, &cp)
		if limit > 0 && len(out) >= limit {
			break
		}
	}
	return out
}

// Get returns the message with the given ID, or ErrNotFound.
func (s *Store) Get(id string) (*Message, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	m, ok := s.byID[id]
	if !ok {
		return nil, ErrNotFound
	}
	cp := *m
	return &cp, nil
}

// Delete removes the message with the given ID. It is not an error if the
// message does not exist; the return value reports whether it did.
func (s *Store) Delete(id string) (bool, error) {
	s.mu.Lock()
	if _, ok := s.byID[id]; !ok {
		s.mu.Unlock()
		return false, nil
	}
	delete(s.byID, id)
	for i, v := range s.ids {
		if v == id {
			s.ids = append(s.ids[:i], s.ids[i+1:]...)
			break
		}
	}
	s.mu.Unlock()

	if err := os.Remove(filepath.Join(s.dir, id+".json")); err != nil && !os.IsNotExist(err) {
		return true, err
	}
	return true, nil
}

// Len returns the number of stored messages.
func (s *Store) Len() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.ids)
}
