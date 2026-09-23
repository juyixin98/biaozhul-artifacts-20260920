package tpc

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sync"
)

// Transaction states recorded in the write-ahead logs.
const (
	// Coordinator states.
	StatePreparing = "PREPARING" // tx created, prepare phase in progress, no decision yet
	StateCommit    = "COMMIT"    // decision to commit is durable
	StateAbort     = "ABORT"     // decision to abort is durable
	StateDone      = "DONE"      // decision delivered to every participant

	// Participant states.
	StatePrepared  = "PREPARED"  // voted yes, locks held, waiting for the decision
	StateCommitted = "COMMITTED" // decision applied: commit
	StateAborted   = "ABORTED"   // decision applied: abort
)

// Record is one durable log entry. The log is append-only JSON lines;
// every Append is followed by fsync before the caller is allowed to act
// on the state change (e.g. reply to an RPC).
type Record struct {
	TxID         string          `json:"txId"`
	State        string          `json:"state"`
	Decision     string          `json:"decision,omitempty"`     // set on DONE records
	Payload      json.RawMessage `json:"payload,omitempty"`      // set on PREPARING / PREPARED
	Participants []string        `json:"participants,omitempty"` // set on PREPARING
}

// WAL is an append-only, fsynced log. It is the only persistent state of
// both the coordinator and the participants.
type WAL struct {
	mu sync.Mutex
	f  *os.File
}

// OpenWAL opens (creating if needed) the log at path and returns it along
// with all records already stored, in append order.
func OpenWAL(path string) (*WAL, []Record, error) {
	var recs []Record
	data, err := os.ReadFile(path)
	switch {
	case err == nil:
		dec := json.NewDecoder(bytes.NewReader(data))
		for {
			var r Record
			err := dec.Decode(&r)
			if err == io.EOF {
				break
			}
			if err != nil {
				return nil, nil, fmt.Errorf("corrupt WAL %s: %w", path, err)
			}
			recs = append(recs, r)
		}
	case os.IsNotExist(err):
		// fresh node
	default:
		return nil, nil, err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, nil, err
	}
	return &WAL{f: f}, recs, nil
}

// Append writes one record and fsyncs it before returning.
func (w *WAL) Append(r Record) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	b, err := json.Marshal(r)
	if err != nil {
		return err
	}
	if _, err := w.f.Write(append(b, '\n')); err != nil {
		return err
	}
	return w.f.Sync()
}

// Close closes the underlying file.
func (w *WAL) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.f.Close()
}
