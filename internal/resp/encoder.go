package resp

import (
	"bytes"
	"io"
	"strconv"
)

// Encoder writes RESP2 values to a stream. It is safe for concurrent use as
// long as the underlying writer serializes individual Write calls; callers
// needing atomic multi-value writes should pass a guarded writer (the
// server buffers one whole HTTP response in a bytes.Buffer).
type Encoder struct {
	w io.Writer
}

// NewEncoder wraps w.
func NewEncoder(w io.Writer) *Encoder { return &Encoder{w: w} }

// WriteTo encodes v into w. It is the standalone counterpart to Encoder and
// returns the byte count.
func WriteTo(w io.Writer, v Value) (int64, error) {
	return encodeValue(w, v)
}

// Encode writes one value.
func (e *Encoder) Encode(v Value) error {
	_, err := encodeValue(e.w, v)
	return err
}

// EncodeAll writes values back to back, the wire shape of a pipeline reply.
func (e *Encoder) EncodeAll(vs ...Value) error {
	for _, v := range vs {
		if err := e.Encode(v); err != nil {
			return err
		}
	}
	return nil
}

// Append encodes v and appends the bytes to buf.
func Append(buf []byte, v Value) []byte {
	var b bytes.Buffer
	_, _ = encodeValue(&b, v)
	return append(buf, b.Bytes()...)
}

func encodeValue(w io.Writer, v Value) (int64, error) {
	switch v.Kind {
	case KindSimple:
		return writeSafeLine(w, '+', v.Str)
	case KindError:
		return writeSafeLine(w, '-', v.Str)
	case KindInteger:
		var n int64
		k, err := writeBytes(w, ':', strconv.AppendInt(nil, v.N, 10))
		n += k
		if err != nil {
			return n, err
		}
		k, err = writeBytes(w, 0, []byte("\r\n"))
		n += k
		return n, err
	case KindBulk:
		if v.Bulk == nil {
			return writeBytes(w, 0, []byte("$-1\r\n"))
		}
		var n int64
		k, err := writeBytes(w, 0, []byte("$"+strconv.Itoa(len(v.Bulk))+"\r\n"))
		n += k
		if err != nil {
			return n, err
		}
		k2, err := w.Write(v.Bulk)
		n += int64(k2)
		if err != nil {
			return n, err
		}
		k, err = writeBytes(w, 0, []byte("\r\n"))
		n += k
		return n, err
	case KindArray:
		if v.Array == nil {
			return writeBytes(w, 0, []byte("*-1\r\n"))
		}
		var n int64
		k, err := writeBytes(w, 0, []byte("*"+strconv.Itoa(len(v.Array))+"\r\n"))
		n += k
		if err != nil {
			return n, err
		}
		for _, elem := range v.Array {
			k, err := encodeValue(w, elem)
			n += k
			if err != nil {
				return n, err
			}
		}
		return n, nil
	default:
		return 0, NewProtocolError("encoder: value has unknown kind")
	}
}

// writeBytes writes raw data, optionally prefixed by a one-byte type marker.
func writeBytes(w io.Writer, marker byte, data []byte) (int64, error) {
	if marker != 0 {
		if _, err := w.Write([]byte{marker}); err != nil {
			return 0, err
		}
	}
	k, err := w.Write(data)
	return int64(k), err
}

// writeSafeLine writes a simple string or error line. Embedded CR/LF bytes
// are replaced with spaces so a value can never forge additional frames.
func writeSafeLine(w io.Writer, marker byte, s string) (int64, error) {
	var n int64
	if _, err := w.Write([]byte{marker}); err != nil {
		return 0, err
	}
	n++
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c == '\r' || c == '\n' {
			c = ' '
		}
		if _, err := w.Write([]byte{c}); err != nil {
			return n, err
		}
		n++
	}
	if _, err := w.Write([]byte("\r\n")); err != nil {
		return n, err
	}
	return n + 2, nil
}
