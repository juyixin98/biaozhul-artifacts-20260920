// Package resp implements a strict, binary-safe RESP2 protocol codec.
//
// RESP2 (REdis Serialization Protocol, version 2) is the wire format used by
// Redis. It can encode five element types:
//
//	+Simple strings   "+OK\r\n"
//	-Errors           "-ERR ...\r\n"
//	:Integers         ":42\r\n"
//	$Bulk strings     "$5\r\nhello\r\n"   (binary safe; "$-1\r\n" is NULL)
//	*Arrays           "*2\r\n$1\r\na\r\n$1\r\nb\r\n" ("*-1\r\n" is NULL)
//
// This parser is intentionally strict: declared lengths are bounded, array
// nesting is bounded, and malformed framing (bad line endings, leading zeros
// on integers, trailing data, negative lengths other than the null marker) is
// rejected.
package resp

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"
)

// Protocol limits. They are deliberately small for an in-memory demo server:
// every declared size is validated up front, so a peer advertising a 4 GB bulk
// is rejected immediately rather than consuming resources.
const (
	MaxBulkLength    = 16 << 20 // 16 MiB for a single bulk string
	MaxInlineLine    = 64 << 10 // 64 KiB for simple/error/integer header lines
	MaxArrayElements = 1 << 20  // declared element count per array
	MaxNestingDepth  = 7        // nested-array depth (root counts as 1)
)

// Sentinel errors. Values wrap these so callers can classify failures with
// errors.Is.
var (
	// ErrProtocol means the byte stream is not valid RESP and the
	// connection must be closed; no further commands can be parsed
	// because the parser no longer knows where a frame begins.
	ErrProtocol = errors.New("resp: protocol error")
	// ErrUnexpectedEOF means the stream ended cleanly (EOF) in the middle
	// of a frame: a half packet followed by disconnect.
	ErrUnexpectedEOF = errors.New("resp: unexpected EOF")
)

// Type identifies a parsed Value.
type Type uint8

const (
	SimpleString Type = iota + 1
	Error
	Integer
	BulkString
	Array
	NullBulk  // the null bulk "$-1\r\n"
	NullArray // the null array "*-1\r\n"
)

// Value is one RESP element. Bulk is binary safe. Array holds children.
type Value struct {
	Type  Type
	Str   []byte // BulkString payload
	Text  string // SimpleString / Error payload
	Int   int64  // Integer payload
	Array []*Value
}

// Reader decodes RESP frames from an io.Reader.
type Reader struct {
	br *bufio.Reader
}

// NewReader returns a Reader with the default 64 KiB peek/read buffer.
func NewReader(r io.Reader) *Reader {
	return &Reader{br: bufio.NewReaderSize(r, MaxInlineLine)}
}

// ReadMessage parses exactly one complete RESP value. It returns
// io.EOF only when the stream is empty (clean shutdown between frames); a
// truncated frame returns ErrUnexpectedEOF; malformed framing returns
// ErrProtocol.
func (r *Reader) ReadMessage() (*Value, error) {
	b, err := r.readLine()
	if err != nil {
		return nil, err
	}
	if len(b) == 0 {
		return nil, protof("empty type prefix line")
	}
	return r.readValue(b[0], b[1:], 1)
}

// readValue parses the body of a frame. first is the type byte ('+', '-',
// ':', '$', '*'), rest is everything after the type byte on the header line.
// depth is 1-based nesting depth of the current array frame.
func (r *Reader) readValue(first byte, rest []byte, depth int) (*Value, error) {
	switch first {
	case '+':
		if len(rest) > MaxInlineLine {
			return nil, protof("simple string too long")
		}
		return &Value{Type: SimpleString, Text: string(rest)}, nil
	case '-':
		if len(rest) > MaxInlineLine {
			return nil, protof("error message too long")
		}
		return &Value{Type: Error, Text: string(rest)}, nil
	case ':':
		n, err := parseStrictInt(rest)
		if err != nil {
			return nil, err
		}
		return &Value{Type: Integer, Int: n}, nil
	case '$':
		n, err := parseStrictInt(rest)
		if err != nil {
			return nil, err
		}
		if n == -1 {
			return &Value{Type: NullBulk}, nil
		}
		if n < 0 {
			return nil, protof("bulk length %d must be >= 0 (only -1 means null)", n)
		}
		if n > MaxBulkLength {
			return nil, protof("bulk length %d exceeds limit %d", n, MaxBulkLength)
		}
		buf := make([]byte, n)
		if err := r.readExact(buf); err != nil {
			return nil, asUnexpectedEOF(err)
		}
		if err := r.expectCRLF(); err != nil {
			return nil, err
		}
		return &Value{Type: BulkString, Str: buf}, nil
	case '*':
		n, err := parseStrictInt(rest)
		if err != nil {
			return nil, err
		}
		if n == -1 {
			return &Value{Type: NullArray}, nil
		}
		if n < 0 {
			return nil, protof("array length %d must be >= 0 (only -1 means null)", n)
		}
		if n > MaxArrayElements {
			return nil, protof("array length %d exceeds limit %d", n, MaxArrayElements)
		}
		if depth > MaxNestingDepth {
			return nil, protof("array nesting depth exceeds limit %d", MaxNestingDepth)
		}
		arr := make([]*Value, n)
		for i := int64(0); i < n; i++ {
			line, err := r.readLine()
			if err != nil {
				// The array header promised an element; a clean EOF here
				// means the stream ended mid-frame (half packet).
				if errors.Is(err, io.EOF) {
					return nil, ErrUnexpectedEOF
				}
				return nil, err
			}
			if len(line) == 0 {
				return nil, protof("empty type prefix line in array element %d", i)
			}
			child, err := r.readValue(line[0], line[1:], depth+1)
			if err != nil {
				return nil, err
			}
			arr[i] = child
		}
		return &Value{Type: Array, Array: arr}, nil
	default:
		return nil, protof("unknown type byte %q", string(first))
	}
}

// readLine returns one CRLF-terminated line without the trailing CRLF.
// A bare EOF between frames returns io.EOF; EOF with data but without CRLF
// is a half packet (ErrUnexpectedEOF); \r not followed by \n or any line
// longer than MaxInlineLine is a protocol error.
func (r *Reader) readLine() ([]byte, error) {
	line, err := r.br.ReadSlice('\n')
	if err != nil && !errors.Is(err, bufio.ErrBufferFull) {
		if errors.Is(err, io.EOF) {
			if len(line) == 0 {
				return nil, io.EOF
			}
			return nil, ErrUnexpectedEOF
		}
		return nil, err
	}
	if errors.Is(err, bufio.ErrBufferFull) {
		return nil, protof("line exceeds %d bytes", MaxInlineLine)
	}
	if len(line) < 2 || line[len(line)-2] != '\r' {
		// ReadSlice only returns a non-empty chunk ending \n here; a lone
		// \n with no preceding \r is framing garbage.
		return nil, protof("expected CRLF line ending")
	}
	return line[:len(line)-2], nil
}

// readExact fills b fully, translating EOF to ErrUnexpectedEOF.
func (r *Reader) readExact(b []byte) error {
	if _, err := io.ReadFull(r.br, b); err != nil {
		return asUnexpectedEOF(err)
	}
	return nil
}

// expectCRLF consumes the mandatory two-byte trailer after a bulk payload.
func (r *Reader) expectCRLF() error {
	var two [2]byte
	if err := r.readExact(two[:]); err != nil {
		return err
	}
	if two[0] != '\r' || two[1] != '\n' {
		return protof("expected CRLF after bulk payload, got %q", two[:])
	}
	return nil
}

func asUnexpectedEOF(err error) error {
	if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
		return ErrUnexpectedEOF
	}
	return err
}

// parseStrictInt parses a RESP integer field. Rules: an optional leading '-',
// at least one decimal digit, no '+' sign, no spaces, and no leading zeros
// ("00", "-00", "-0" are all rejected). This matches canonical framing and
// rejects ambiguous/abusive input such as ":00001\r\n".
func parseStrictInt(b []byte) (int64, error) {
	if len(b) == 0 {
		return 0, protof("empty integer field")
	}
	i := 0
	neg := false
	if b[0] == '-' {
		neg = true
		i = 1
		if len(b) == 1 {
			return 0, protof("integer field is just '-'")
		}
	}
	if len(b)-i > 1 && b[i] == '0' {
		return 0, protof("non-canonical integer %q: leading zero", string(b))
	}
	for ; i < len(b); i++ {
		if b[i] < '0' || b[i] > '9' {
			return 0, protof("invalid integer %q", string(b))
		}
	}
	n, err := strconv.ParseInt(string(b), 10, 64)
	if err != nil {
		return 0, protof("integer %q out of range", string(b))
	}
	if neg && n == 0 {
		return 0, protof("non-canonical integer %q: negative zero", string(b))
	}
	return n, nil
}

func protof(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrProtocol, fmt.Sprintf(format, args...))
}

// ---------------------------------------------------------------------------
// Encoder
// ---------------------------------------------------------------------------

// Writer encodes RESP frames. It is safe for single-goroutine use; the server
// buffers one HTTP response with one Writer.
type Writer struct {
	w io.Writer
}

// NewWriter returns a Writer writing to w.
func NewWriter(w io.Writer) *Writer { return &Writer{w: w} }

func (ww *Writer) writeRaw(b []byte) error {
	_, err := ww.w.Write(b)
	return err
}

// WriteValue encodes any *Value (including nulls).
func (ww *Writer) WriteValue(v *Value) error {
	switch v.Type {
	case SimpleString:
		return ww.writeRaw(append(append([]byte{'+'}, v.Text...), '\r', '\n'))
	case Error:
		return ww.writeRaw(append(append([]byte{'-'}, v.Text...), '\r', '\n'))
	case Integer:
		return ww.writeRaw(appendInt(':', v.Int))
	case BulkString:
		return ww.WriteBulk(v.Str)
	case NullBulk:
		return ww.writeRaw([]byte("$-1\r\n"))
	case NullArray:
		return ww.writeRaw([]byte("*-1\r\n"))
	case Array:
		if err := ww.writeRaw(appendInt('*', int64(len(v.Array)))); err != nil {
			return err
		}
		for _, child := range v.Array {
			if err := ww.WriteValue(child); err != nil {
				return err
			}
		}
		return nil
	default:
		return fmt.Errorf("resp: cannot encode value type %d", v.Type)
	}
}

func appendInt(prefix byte, n int64) []byte {
	b := strconv.AppendInt([]byte{prefix}, n, 10)
	return append(b, '\r', '\n')
}

// WriteBulk encodes a byte slice as a binary-safe bulk string. A nil slice is
// encoded as an empty bulk ("$0\r\n\r\n"), NOT as a null: callers that need a
// null must write NullBulk explicitly.
func (ww *Writer) WriteBulk(b []byte) error {
	out := appendInt('$', int64(len(b)))
	out = append(out, b...)
	out = append(out, '\r', '\n')
	return ww.writeRaw(out)
}

// WriteSimpleString, WriteError, WriteInteger are convenience encoders.
func (ww *Writer) WriteSimpleString(s string) error {
	if strings.ContainsAny(s, "\r\n") {
		return fmt.Errorf("resp: simple string %q contains CR/LF", s)
	}
	return ww.writeRaw(append(append([]byte{'+'}, s...), '\r', '\n'))
}

func (ww *Writer) WriteError(s string) error {
	if strings.ContainsAny(s, "\r\n") {
		return fmt.Errorf("resp: error message %q contains CR/LF", s)
	}
	return ww.writeRaw(append(append([]byte{'-'}, s...), '\r', '\n'))
}

func (ww *Writer) WriteInteger(n int64) error {
	return ww.writeRaw(appendInt(':', n))
}

// Constructors used by command implementations.
var (
	okReply     = &Value{Type: SimpleString, Text: "OK"}
	pongReply   = &Value{Type: SimpleString, Text: "PONG"}
	queuedReply = &Value{Type: SimpleString, Text: "QUEUED"}
	nilBulk     = &Value{Type: NullBulk}
	nilArray    = &Value{Type: NullArray}
	zeroInt     = &Value{Type: Integer, Int: 0}
	oneInt      = &Value{Type: Integer, Int: 1}
)

// ReplyOK, ReplyPong, ReplyQueued, NilBulk, NilArray and the *Val
// constructors keep callers away from hand-building Values.
func ReplyOK() *Value         { return okReply }
func ReplyPong() *Value       { return pongReply }
func ReplyQueued() *Value     { return queuedReply }
func NilBulk() *Value         { return nilBulk }
func NilArray() *Value        { return nilArray }
func IntVal(n int64) *Value   { return &Value{Type: Integer, Int: n} }
func BulkVal(b []byte) *Value { return &Value{Type: BulkString, Str: b} }
func BulkStringVal(s string) *Value {
	return &Value{Type: BulkString, Str: []byte(s)}
}
func SimpleVal(s string) *Value { return &Value{Type: SimpleString, Text: s} }

// ErrVal builds an error reply. kind is the leading uppercase code, e.g.
// "ERR" or "EXECABORT", matching the Redis "<KIND> <message>" convention.
func ErrVal(kind, msg string) *Value {
	if kind == "" {
		kind = "ERR"
	}
	return &Value{Type: Error, Text: kind + " " + msg}
}
