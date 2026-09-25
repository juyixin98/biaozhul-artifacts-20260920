// Package stream defines the wire protocol between server and client:
// newline-delimited JSON records. A successful stream is a sequence of
// "item" records followed by an "end" trailer. A failure that occurs
// after some items were already sent is expressed as an "error" trailer
// record, because the HTTP status code can no longer be changed.
package stream

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
)

// Record types on the wire.
const (
	TypeItem  = "item"
	TypeEnd   = "end"
	TypeError = "error"
)

// Error codes carried by error trailer records.
const (
	CodeUpstreamFailure = "UPSTREAM_FAILURE"
	CodeItemTooLarge    = "ITEM_TOO_LARGE"
	CodeServerShutdown  = "SERVER_SHUTDOWN"
)

// Record is one line of the NDJSON stream.
type Record struct {
	Type string `json:"type"`
	// Item records.
	Seq     int    `json:"seq,omitempty"`
	Bytes   int    `json:"bytes,omitempty"`
	Payload string `json:"payload,omitempty"`
	// Trailer records (end / error): how many items were sent before it.
	Sent    int    `json:"sent,omitempty"`
	Code    string `json:"code,omitempty"`
	Message string `json:"message,omitempty"`
}

// Item builds an item record.
func Item(seq int, payload []byte) Record {
	return Record{Type: TypeItem, Seq: seq, Bytes: len(payload), Payload: string(payload)}
}

// End builds the success trailer.
func End(sent int) Record {
	return Record{Type: TypeEnd, Sent: sent}
}

// Error builds an error trailer.
func Error(code, message string, sent int) Record {
	return Record{Type: TypeError, Code: code, Message: message, Sent: sent}
}

// Encode writes one record as a JSON line.
func Encode(w io.Writer, r Record) error {
	b, err := json.Marshal(r)
	if err != nil {
		return fmt.Errorf("encode record: %w", err)
	}
	if _, err := w.Write(append(b, '\n')); err != nil {
		return fmt.Errorf("write record: %w", err)
	}
	return nil
}

// Decoder reads records line by line with a bounded line size so a
// malformed peer cannot exhaust memory.
type Decoder struct {
	sc *bufio.Scanner
}

// NewDecoder builds a Decoder rejecting any single line over maxLineBytes.
func NewDecoder(r io.Reader, maxLineBytes int) *Decoder {
	sc := bufio.NewScanner(r)
	// Scanner's effective limit is max(max, cap(initial buffer)), so the
	// initial buffer must not exceed maxLineBytes.
	initial := 64 * 1024
	if maxLineBytes < initial {
		initial = maxLineBytes
	}
	sc.Buffer(make([]byte, 0, initial), maxLineBytes)
	return &Decoder{sc: sc}
}

// Next returns the next record, io.EOF at clean end of stream, or an error.
func (d *Decoder) Next() (Record, error) {
	if !d.sc.Scan() {
		if err := d.sc.Err(); err != nil {
			return Record{}, fmt.Errorf("decode stream: %w", err)
		}
		return Record{}, io.EOF
	}
	var r Record
	if err := json.Unmarshal(d.sc.Bytes(), &r); err != nil {
		return Record{}, fmt.Errorf("decode record: %w", err)
	}
	return r, nil
}
