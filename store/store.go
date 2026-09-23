// Package store persists received mail messages to a local spool directory.
//
// A message is written atomically: the full payload is first written to a
// temporary file in the same directory, synced, and then renamed to its final
// name. Readers therefore can only ever observe complete messages; a crash or
// a client disconnecting mid-DATA leaves at most an orphan .tmp file, never a
// half-written message.
package store

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// Message is one fully received (end-of-DATA acknowledged) mail.
type Message struct {
	ID       string    `json:"id"`
	From     string    `json:"from"`
	To       []string  `json:"to"`
	Raw      []byte    `json:"-"`
	Received time.Time `json:"received_at"`
	Size     int       `json:"size"`
}

const (
	msgDir  = "msgs"
	tmpDir  = "tmp"
	tmpExt  = ".tmp"
	msgExt  = ".eml"
	maxRand = 16
)

// Store is a filesystem-backed, goroutine-safe mail store.
type Store struct {
	root string
	mu   sync.RWMutex
	msgs []*Message // newest first
}

// Open opens (creating if needed) a store rooted at dir and rebuilds its index
// from files already present on disk.
func Open(dir string) (*Store, error) {
	for _, sub := range []string{dir, filepath.Join(dir, msgDir), filepath.Join(dir, tmpDir)} {
		if err := os.MkdirAll(sub, 0o755); err != nil {
			return nil, fmt.Errorf("store: create %s: %w", sub, err)
		}
	}
	s := &Store{root: dir}
	if err := s.load(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *Store) msgsDir() string { return filepath.Join(s.root, msgDir) }
func (s *Store) tmpDir() string  { return filepath.Join(s.root, tmpDir) }

// Save atomically persists a new message and returns its assigned ID. The
// raw payload is written exactly as received after SMTP dot-unescaping.
func (s *Store) Save(from string, to []string, raw []byte, now time.Time) (string, error) {
	id := newID(now)
	recipients := append([]string(nil), to...)

	finalPath := filepath.Join(s.msgsDir(), id+msgExt)
	tmpPath := filepath.Join(s.tmpDir(), id+tmpExt)

	f, err := os.OpenFile(tmpPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return "", fmt.Errorf("store: create temp: %w", err)
	}
	// Write metadata header so the on-disk file is self-describing and the
	// index can be rebuilt after a restart.
	meta := envelope{ID: id, From: from, To: recipients, ReceivedAt: now, Size: len(raw)}
	if _, err := f.Write(meta.encodingHeader()); err != nil {
		f.Close()
		s.removeQuiet(tmpPath)
		return "", err
	}
	if _, err := f.Write(raw); err != nil {
		f.Close()
		s.removeQuiet(tmpPath)
		return "", err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		s.removeQuiet(tmpPath)
		return "", err
	}
	if err := f.Close(); err != nil {
		s.removeQuiet(tmpPath)
		return "", err
	}
	// rename(2) within the same filesystem is atomic.
	if err := os.Rename(tmpPath, finalPath); err != nil {
		s.removeQuiet(tmpPath)
		return "", fmt.Errorf("store: rename: %w", err)
	}
	s.syncDir(s.msgsDir())

	m := &Message{ID: id, From: from, To: recipients, Raw: append([]byte(nil), raw...), Received: now, Size: len(raw)}
	s.mu.Lock()
	s.msgs = append(s.msgs, m)
	s.mu.Unlock()
	return id, nil
}

// List returns summaries of all messages, newest first.
func (s *Store) List() []*Message {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]*Message, len(s.msgs))
	for i, m := range s.msgs {
		cp := *m
		cp.Raw = nil
		cp.To = append([]string(nil), m.To...)
		out[i] = &cp
	}
	return out
}

// Get returns the message with the given ID, or ErrNotFound.
func (s *Store) Get(id string) (*Message, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, m := range s.msgs {
		if m.ID == id {
			cp := *m
			cp.To = append([]string(nil), m.To...)
			cp.Raw = append([]byte(nil), m.Raw...)
			return &cp, nil
		}
	}
	return nil, ErrNotFound
}

// Delete removes a message; it returns ErrNotFound if no such message exists.
func (s *Store) Delete(id string) error {
	s.mu.Lock()
	idx := -1
	for i, m := range s.msgs {
		if m.ID == id {
			idx = i
			break
		}
	}
	if idx < 0 {
		s.mu.Unlock()
		return ErrNotFound
	}
	s.msgs = append(s.msgs[:idx], s.msgs[idx+1:]...)
	s.mu.Unlock()

	if err := os.Remove(filepath.Join(s.msgsDir(), id+msgExt)); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("store: delete: %w", err)
	}
	return nil
}

// load rebuilds the in-memory index from disk and sweeps temp files left by
// crashed or interrupted DATA commands.
func (s *Store) load() error {
	entries, err := os.ReadDir(s.msgsDir())
	if err != nil {
		return fmt.Errorf("store: read msgs dir: %w", err)
	}
	var loaded []*Message
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), msgExt) {
			continue
		}
		path := filepath.Join(s.msgsDir(), e.Name())
		data, err := os.ReadFile(path)
		if err != nil {
			return fmt.Errorf("store: read %s: %w", e.Name(), err)
		}
		env, raw, err := decodeFile(data)
		if err != nil {
			return fmt.Errorf("store: parse %s: %w", e.Name(), err)
		}
		loaded = append(loaded, &Message{
			ID:       env.ID,
			From:     env.From,
			To:       env.To,
			Raw:      raw,
			Received: env.ReceivedAt,
			Size:     env.Size,
		})
	}
	sort.Slice(loaded, func(i, j int) bool { return loaded[i].ID < loaded[j].ID })
	s.msgs = loaded

	// Sweep orphan temp files: they never represented a complete message.
	tmps, err := os.ReadDir(s.tmpDir())
	if err != nil {
		return fmt.Errorf("store: read tmp dir: %w", err)
	}
	for _, e := range tmps {
		if !e.IsDir() && strings.HasSuffix(e.Name(), tmpExt) {
			_ = os.Remove(filepath.Join(s.tmpDir(), e.Name()))
		}
	}
	return nil
}

func (s *Store) removeQuiet(path string) { _ = os.Remove(path) }

// syncDir best-effort fsyncs a directory so a rename is durable. Errors are
// ignored; the rename itself is still atomic on every supported platform.
func (s *Store) syncDir(dir string) {
	if d, err := os.Open(dir); err == nil {
		_ = d.Sync()
		_ = d.Close()
	}
}

func newID(now time.Time) string {
	var b [maxRand]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failing is not recoverable for a unique ID; surface it.
		panic(fmt.Errorf("store: crypto/rand failed: %w", err))
	}
	return now.UTC().Format("20060102T150405.000000000") + "-" + hex.EncodeToString(b[:])
}

// envelope is the JSON metadata prepended to each stored .eml file.
type envelope struct {
	ID         string    `json:"id"`
	From       string    `json:"from"`
	To         []string  `json:"to"`
	ReceivedAt time.Time `json:"received_at"`
	Size       int       `json:"size"`
}

// encodingHeader serialises the envelope as a single CRLF-terminated JSON
// line, making each stored file: one metadata line followed by the raw RFC822
// payload.
func (e envelope) encodingHeader() []byte {
	b, err := json.Marshal(e)
	if err != nil {
		// All fields are trivially marshallable.
		panic(err)
	}
	return append(b, '\n')
}

func decodeFile(data []byte) (envelope, []byte, error) {
	nl := bytes.IndexByte(data, '\n')
	if nl < 0 {
		return envelope{}, nil, errors.New("missing metadata header")
	}
	var env envelope
	if err := json.Unmarshal(data[:nl], &env); err != nil {
		return envelope{}, nil, err
	}
	raw := data[nl+1:]
	return env, raw, nil
}

// ErrNotFound is returned by Get/Delete for unknown message IDs.
var ErrNotFound = errors.New("message not found")
