package mqtt

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// storedMessage is the on-disk form of an MQTT application message. Payload is
// base64-encoded by encoding/json so arbitrary binary survives a round trip.
type storedMessage struct {
	Topic   string `json:"topic"`
	QoS     byte   `json:"qos"`
	Payload []byte `json:"payload"`
}

type storedInflight struct {
	PacketID uint16        `json:"packet_id"`
	Message  storedMessage `json:"message"`
}

// storedSession is the JSON snapshot of one persistent session (§3.1.2-4).
type storedSession struct {
	Subscriptions map[string]byte  `json:"subscriptions"`
	Inflight      []storedInflight `json:"inflight"`
	// Queue holds offline QoS>=1 messages not yet given a packet ID.
	Queue        []storedMessage `json:"queue"`
	NextPacketID uint16          `json:"next_packet_id"`
}

type snapshot struct {
	// Sessions keyed by Client ID.
	Sessions map[string]storedSession `json:"sessions"`
}

// store is the JSON-file persistence backend. All durable state lives in one
// snapshot that is rewritten (temp file + rename) on every mutation. The
// subset targets local single-process use; fsync-per-write durability is
// sufficient for the acceptance scenarios.
type store struct {
	path string
}

func newStore(dir string) (*store, error) {
	if dir == "" {
		return nil, errors.New("mqtt: persistence directory required")
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("mqtt: create state dir: %w", err)
	}
	return &store{path: filepath.Join(dir, "mqttstate.json")}, nil
}

func (s *store) load() (*snapshot, error) {
	data, err := os.ReadFile(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return &snapshot{Sessions: map[string]storedSession{}}, nil
	}
	if err != nil {
		return nil, err
	}
	snap := &snapshot{}
	if len(data) == 0 {
		snap.Sessions = map[string]storedSession{}
		return snap, nil
	}
	if err := json.Unmarshal(data, snap); err != nil {
		return nil, fmt.Errorf("mqtt: parse state file: %w", err)
	}
	if snap.Sessions == nil {
		snap.Sessions = map[string]storedSession{}
	}
	return snap, nil
}

// save atomically replaces the snapshot file: write temp, fsync, rename,
// fsync the directory so the replacement survives a process/OS crash.
func (s *store) save(snap *snapshot) error {
	data, err := json.MarshalIndent(snap, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp, s.path); err != nil {
		return err
	}
	if dir, err := os.Open(filepath.Dir(s.path)); err == nil {
		_ = dir.Sync()
		_ = dir.Close()
	}
	return nil
}
