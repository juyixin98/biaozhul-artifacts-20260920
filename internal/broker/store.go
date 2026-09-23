package broker

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// storedMessage is the on-disk form of a queued/in-flight QoS1 (or QoS0
// offline-buffered) message. Payload is base64 under encoding/json.
type storedMessage struct {
	Topic   string `json:"topic"`
	QoS     byte   `json:"qos"`
	Payload []byte `json:"payload"`
	// PacketID is zero until the broker assigns one (pending queue).
	PacketID uint16 `json:"packet_id,omitempty"`
}

// sessionSnapshot is one persistent session in the state file. Only
// clean-session=false sessions are ever stored.
type sessionSnapshot struct {
	ClientID    string          `json:"client_id"`
	Subs        map[string]byte `json:"subs"`
	Pending     []storedMessage `json:"pending"`
	Inflight    []storedMessage `json:"inflight"`
	SeenInbound []uint16        `json:"seen_inbound,omitempty"`
	NextPID     uint16          `json:"next_pid"`
}

type snapshotFile struct {
	Version  int               `json:"version"`
	Sessions []sessionSnapshot `json:"sessions"`
}

const snapshotVersion = 1

// Store persists durable sessions as a single JSON document. Writes are
// debounced by the Broker (markDirty) and the file is rewritten atomically
// (temp file + rename) so a crash never leaves a torn document.
type Store struct {
	mu   sync.Mutex
	path string
}

func NewStore(path string) *Store { return &Store{path: path} }

func (s *Store) load() (*snapshotFile, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	data, err := os.ReadFile(s.path)
	if os.IsNotExist(err) {
		return &snapshotFile{Version: snapshotVersion}, nil
	}
	if err != nil {
		return nil, err
	}
	var sf snapshotFile
	if err := json.Unmarshal(data, &sf); err != nil {
		return nil, fmt.Errorf("corrupt session store %s: %w", s.path, err)
	}
	if sf.Version != snapshotVersion {
		return nil, fmt.Errorf("unsupported session store version %d", sf.Version)
	}
	return &sf, nil
}

// save rewrites the document atomically.
func (s *Store) save(sf *snapshotFile) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.path == "" {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(sf, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}
