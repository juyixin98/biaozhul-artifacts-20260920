// Package protocol defines the NDJSON wire format used between the streaming
// server and its clients. Every line is one self-delimited JSON record so a
// client can consume partial results incrementally and still learn about a
// failure that happens after some records were already sent.
package protocol

import (
	"encoding/json"
	"fmt"
	"io"
)

// Record types on the wire.
const (
	TypeData  = "data"
	TypeEnd   = "end"
	TypeError = "error"
)

// Terminal statuses carried by TypeEnd records.
const (
	StatusOK = "ok"
)

// Error codes carried by TypeError records.
const (
	CodeUpstreamError = "UPSTREAM_ERROR"
	CodeItemTooLarge  = "ITEM_TOO_LARGE"
	CodeShuttingDown  = "SHUTTING_DOWN"
)

// Record is a single NDJSON line. Fields are reused across record types to
// keep the decoder trivial; irrelevant fields stay empty.
type Record struct {
	Type    string `json:"type"`
	Seq     int    `json:"seq,omitempty"`
	Payload string `json:"payload,omitempty"`
	Status  string `json:"status,omitempty"`
	Code    string `json:"code,omitempty"`
	Message string `json:"message,omitempty"`
	// Sent is the number of data records delivered before this terminal
	// record, letting clients reconcile partial streams.
	Sent int `json:"sent,omitempty"`
}

// Data builds a data record.
func Data(seq int, payload []byte) Record {
	return Record{Type: TypeData, Seq: seq, Payload: string(payload)}
}

// End builds the successful terminal record.
func End(sent int) Record {
	return Record{Type: TypeEnd, Status: StatusOK, Sent: sent}
}

// Error builds the terminal error record used when the stream fails after
// headers (and possibly data) were already sent.
func Error(code, message string, sent int) Record {
	return Record{Type: TypeError, Code: code, Message: message, Sent: sent}
}

// Encode writes one record as a single NDJSON line.
func Encode(w io.Writer, r Record) error {
	b, err := json.Marshal(r)
	if err != nil {
		return fmt.Errorf("protocol: marshal record: %w", err)
	}
	if _, err := w.Write(append(b, '\n')); err != nil {
		return fmt.Errorf("protocol: write record: %w", err)
	}
	return nil
}

// Decode parses one NDJSON line.
func Decode(line []byte) (Record, error) {
	var r Record
	if err := json.Unmarshal(line, &r); err != nil {
		return Record{}, fmt.Errorf("protocol: decode record: %w", err)
	}
	switch r.Type {
	case TypeData, TypeEnd, TypeError:
		return r, nil
	default:
		return Record{}, fmt.Errorf("protocol: unknown record type %q", r.Type)
	}
}
