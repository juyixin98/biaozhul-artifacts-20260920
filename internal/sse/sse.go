// Package sse contains Server-Sent Events framing primitives shared by the
// server and the example client.
//
// The wire format follows the W3C "Server-sent events" specification:
// fields are "name: value" lines, an event is terminated by a blank line,
// and a leading ": " comment line is a heartbeat that clients ignore.
package sse

import (
	"bufio"
	"errors"
	"io"
	"strconv"
	"strings"
)

// NormalizeData converts CR LF and lone CR line endings to LF. The SSE wire
// format treats all three as line terminators, so storing only LF keeps the
// emitted multi-line data: fields faithful to what a spec decoder reassembles.
func NormalizeData(data string) string {
	if !strings.ContainsRune(data, '\r') {
		return data
	}
	return strings.ReplaceAll(strings.ReplaceAll(data, "\r\n", "\n"), "\r", "\n")
}

// WriteField writes one "field: value" line. An empty value is written as
// "field:" (no leading space), which per spec decodes to an empty string.
func WriteField(w io.Writer, field, value string) error {
	if value == "" {
		_, err := io.WriteString(w, field+":\n")
		return err
	}
	_, err := io.WriteString(w, field+": "+value+"\n")
	return err
}

// WriteEvent writes a complete event block:
//
//	id: <id>
//	event: <eventType>   (only when eventType != "")
//	data: <line 1>
//	data: <line 2>
//	<blank line>
//
// Multiline data (split on "\n") is emitted as multiple data fields exactly
// as required by the spec; a client concatenates them with "\n". An empty
// data string produces a single "data:" field that decodes to "".
func WriteEvent(w io.Writer, id uint64, eventType, data string) error {
	if err := WriteField(w, "id", strconv.FormatUint(id, 10)); err != nil {
		return err
	}
	if eventType != "" {
		if err := WriteField(w, "event", eventType); err != nil {
			return err
		}
	}
	if data == "" {
		if err := WriteField(w, "data", ""); err != nil {
			return err
		}
	} else {
		lines := strings.Split(data, "\n")
		for _, line := range lines {
			if err := WriteField(w, "data", line); err != nil {
				return err
			}
		}
	}
	_, err := io.WriteString(w, "\n")
	return err
}

// WriteControlEvent writes an event block without an "id:" field, so the
// client's last-event-ID buffer is left unchanged. Use it for server control
// signals (reset/error) that must not alter the resume cursor.
func WriteControlEvent(w io.Writer, eventType, data string) error {
	if eventType != "" {
		if err := WriteField(w, "event", eventType); err != nil {
			return err
		}
	}
	if data == "" {
		if err := WriteField(w, "data", ""); err != nil {
			return err
		}
	} else {
		for _, line := range strings.Split(data, "\n") {
			if err := WriteField(w, "data", line); err != nil {
				return err
			}
		}
	}
	_, err := io.WriteString(w, "\n")
	return err
}

// WriteComment writes a heartbeat/comment line (": <text>\n").
func WriteComment(w io.Writer, text string) error {
	if text == "" {
		_, err := io.WriteString(w, ":\n")
		return err
	}
	_, err := io.WriteString(w, ": "+text+"\n")
	return err
}

// Event is a parsed inbound SSE event.
type Event struct {
	ID   string
	Type string
	Data string
	// Retry is the raw value of a "retry:" field (ms), kept for completeness.
	Retry string
}

// Decoder parses an SSE byte stream following the spec's dispatch algorithm:
// blank lines dispatch the accumulated event, lines starting with ":" are
// comments, and multiple data: lines are joined with "\n". The last-event-ID
// buffer persists across events for the life of the stream.
type Decoder struct {
	r     *bufio.Reader
	idBuf string // connection-scoped last event ID buffer
}

// NewDecoder wraps r.
func NewDecoder(r io.Reader) *Decoder {
	return &Decoder{r: bufio.NewReaderSize(r, 64*1024)}
}

// Next returns the next dispatched event. It returns io.EOF when the stream
// ends (a block cut mid-frame before its blank line is not dispatched).
func (d *Decoder) Next() (Event, error) {
	var (
		data      []string
		eventType string
		retry     string
	)
	for {
		line, err := d.r.ReadString('\n')
		line = strings.TrimSuffix(strings.TrimSuffix(line, "\n"), "\r")
		if line != "" && !strings.HasPrefix(line, ":") {
			field := line
			value := ""
			if i := strings.IndexByte(line, ':'); i >= 0 {
				field = line[:i]
				value = strings.TrimPrefix(line[i+1:], " ")
			}
			switch field {
			case "event":
				eventType = value
			case "data":
				data = append(data, value)
			case "id":
				if !strings.ContainsRune(value, 0) {
					d.idBuf = value
				}
			case "retry":
				retry = value
			default:
				// Unknown fields are ignored per spec.
			}
		}
		// A blank line dispatches only when the data buffer is non-empty AND
		// the line was actually terminated ("\n" read with err == nil). A
		// zero-length ReadString at io.EOF is end-of-stream, not a dispatch.
		if err == nil && line == "" && len(data) > 0 {
			ev := Event{
				ID:    d.idBuf,
				Type:  eventType,
				Data:  strings.Join(data, "\n"),
				Retry: retry,
			}
			if ev.Type == "" {
				ev.Type = "message"
			}
			return ev, nil
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				return Event{}, io.EOF
			}
			return Event{}, err
		}
	}
}
