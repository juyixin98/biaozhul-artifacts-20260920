// Package resp implements a strict, streaming RESP2 (Redis Serialization
// Protocol version 2) reader and writer.
//
// RESP2 distinguishes five value kinds:
//
//   - simple strings ("+OK\r\n"),
//   - errors ("-ERR ...\r\n"),
//   - integers (":1000\r\n"),
//   - bulk strings ("$5\r\nhello\r\n"; "$-1\r\n" is the NULL bulk),
//   - arrays ("*2\r\n..."; "*-1\r\n" is the NULL array).
//
// The empty bulk "$0\r\n\r\n" and the null bulk "$-1\r\n" are different
// values; the decoder preserves the distinction.
package resp

import "errors"

// Kind enumerates the five RESP2 value kinds.
type Kind uint8

const (
	KindUnknown Kind = iota
	KindSimple
	KindError
	KindInteger
	KindBulk
	KindArray
)

// Value is a decoded RESP2 message or a message waiting to be encoded.
//
// For bulk strings the raw bytes are kept in Bulk: a nil slice means the
// NULL bulk ("$-1"), a non-nil zero-length slice means the empty bulk
// ("$0"). A nil Array means the NULL array ("*-1"); an empty non-nil slice
// is the empty array ("*0").
type Value struct {
	Kind Kind

	// Str holds the payload of simple strings and errors (no CRLF).
	Str string

	// N holds the value of integers.
	N int64

	// Bulk holds bulk-string payload bytes; nil == NULL bulk.
	Bulk []byte

	// Array holds array elements; nil == NULL array.
	Array []Value
}

// SimpleString builds a simple string reply ("+<s>\r\n").
func SimpleString(s string) Value { return Value{Kind: KindSimple, Str: s} }

// SimpleError builds an error reply ("-<s>\r\n"). The code (e.g. "ERR",
// "WRONGTYPE") is part of s.
func SimpleError(s string) Value { return Value{Kind: KindError, Str: s} }

// Integer builds an integer reply.
func Integer(n int64) Value { return Value{Kind: KindInteger, N: n} }

// BulkString builds a bulk string; the empty string yields "$0", never NULL.
func BulkString(s string) Value { return Value{Kind: KindBulk, Bulk: []byte(s)} }

// BulkBytes builds a bulk string, copying the payload. A nil slice produces
// the NULL bulk; a non-nil empty slice produces "$0".
func BulkBytes(b []byte) Value {
	if b == nil {
		return NullBulk()
	}
	cp := make([]byte, len(b))
	copy(cp, b)
	return Value{Kind: KindBulk, Bulk: cp}
}

// NullBulk returns the NULL bulk ("$-1\r\n").
func NullBulk() Value { return Value{Kind: KindBulk} }

// NullArray returns the NULL array ("*-1\r\n").
func NullArray() Value { return Value{Kind: KindArray} }

// ArrayValue builds an array (nil elements produces the empty array "*0").
func ArrayValue(elements ...Value) Value { return Value{Kind: KindArray, Array: elements} }

// IsNull reports whether v is a NULL bulk or NULL array.
func (v Value) IsNull() bool {
	switch v.Kind {
	case KindBulk:
		return v.Bulk == nil
	case KindArray:
		return v.Array == nil
	default:
		return false
	}
}

// ProtocolError means the byte stream violates the RESP2 grammar. The
// connection carrying it must be considered poisoned. Truncation in the
// middle of a frame is a ProtocolError too (with Truncated() true), while a
// clean EOF before any frame byte arrives is a plain io.EOF.
type ProtocolError struct {
	msg       string
	truncated bool
}

// Error implements the error interface.
func (e *ProtocolError) Error() string { return "resp: protocol error: " + e.msg }

// Truncated reports whether the error is an unexpected end of stream in the
// middle of a frame (a half packet).
func (e *ProtocolError) Truncated() bool { return e.truncated }

// Is lets errors.Is(err, ErrProtocol) match any protocol error.
func (e *ProtocolError) Is(target error) bool { return target == ErrProtocol }

// ErrProtocol matches every ProtocolError via errors.Is.
var ErrProtocol = &ProtocolError{msg: "invalid resp stream"}

// NewProtocolError builds a grammar-violation error.
func NewProtocolError(msg string) error {
	return &ProtocolError{msg: msg}
}

// NewTruncatedError builds a mid-frame EOF ("half packet") error.
func NewTruncatedError(msg string) error {
	return &ProtocolError{msg: msg, truncated: true}
}

// AsProtocolError extracts the *ProtocolError behind err, or nil.
func AsProtocolError(err error) *ProtocolError {
	var pe *ProtocolError
	if errors.As(err, &pe) {
		return pe
	}
	return nil
}
