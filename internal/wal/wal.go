// Package wal is the durable write-ahead log. Every Append is followed by an
// fsync, so once Append returns the record survives a simulated crash.
// Nothing here is in-memory: a restarted node recovers purely by re-reading
// the log file.
package wal

import (
	"bufio"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
)

// KVWrite is one key/value mutation carried by a transaction.
type KVWrite struct {
	Key   string `json:"key"`
	Value string `json:"value"`
}

// Record is one log entry. Kind-specific payloads are kept in the same
// struct so the log is a single, append-only JSON-lines stream.
//
// Coordinator records: "begin" (TxnID, Participants),
//
//	"commit" (TxnID), "abort" (TxnID).
//
// Participant records: "prepared" (TxnID, Writes),
//
//	"commit" (TxnID, Writes), "abort" (TxnID).
type Record struct {
	Kind         string    `json:"kind"`
	TxnID        string    `json:"txnId"`
	Participants []string  `json:"participants,omitempty"`
	Writes       []KVWrite `json:"writes,omitempty"`
}

// Log is an append-only, fsync-per-record JSON-lines log.
type Log struct {
	f *os.File
	w *bufio.Writer
}

// Open creates <dir>/<name>.log (and its directory) for appending.
func Open(dir, name string) (*Log, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	path := filepath.Join(dir, name+".log")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	return &Log{f: f, w: bufio.NewWriter(f)}, nil
}

// Append writes one record, flushes and fsyncs before returning.
func (l *Log) Append(r Record) error {
	data, err := json.Marshal(r)
	if err != nil {
		return err
	}
	if _, err := l.w.Write(data); err != nil {
		return err
	}
	if err := l.w.WriteByte('\n'); err != nil {
		return err
	}
	if err := l.w.Flush(); err != nil {
		return err
	}
	return l.f.Sync()
}

// Close releases the file.
func (l *Log) Close() error {
	if err := l.w.Flush(); err != nil {
		return err
	}
	return l.f.Close()
}

// ReadAll replays a node's log from disk without mutating it.
func ReadAll(dir, name string) ([]Record, error) {
	f, err := os.Open(filepath.Join(dir, name+".log"))
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	defer f.Close()
	var out []Record
	dec := json.NewDecoder(bufio.NewReader(f))
	for {
		var r Record
		if err := dec.Decode(&r); err != nil {
			if err == io.EOF {
				break
			}
			return nil, err
		}
		out = append(out, r)
	}
	return out, nil
}
