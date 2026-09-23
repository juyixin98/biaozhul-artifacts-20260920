package resp

import (
	"bufio"
	"bytes"
	"io"
	"strconv"
)

// Limits caps what a Decoder will accept. Zero fields fall back to the
// DefaultLimits values when using NewDecoder; explicit caps make it possible
// to mount a small decoder in tests.
type Limits struct {
	// MaxLineLen bounds simple strings, error payloads, integer fields and
	// the header lines of bulks/arrays (excluding trailing CRLF).
	MaxLineLen int
	// MaxBulkLen bounds the declared payload length of a bulk string.
	MaxBulkLen int64
	// MaxArrayLen bounds the declared element count of an array.
	MaxArrayLen int64
	// MaxNesting bounds total nesting depth: an array nested so that its
	// level exceeds MaxNesting is rejected. Level 1 is the outermost array.
	MaxNesting int
	// MaxInlineLen bounds an inline-command line (the text-protocol form
	// accepted by real Redis servers).
	MaxInlineLen int
}

// DefaultLimits are deliberately far below any memory exhaustion risk while
// still accepting any reasonable command stream.
func DefaultLimits() Limits {
	return Limits{
		MaxLineLen:   64 * 1024,
		MaxBulkLen:   64 * 1024 * 1024, // 64 MiB declared payload
		MaxArrayLen:  1_000_000,
		MaxNesting:   7,
		MaxInlineLen: 64 * 1024,
	}
}

// Decoder reads RESP2 values from a buffered stream. It is safe to feed one
// byte at a time: no frame is consumed until fully validated, and the
// scanner never commits to a declared bulk larger than MaxBulkLen.
type Decoder struct {
	r      *bufio.Reader
	limits Limits
}

// NewDecoder wraps r with the default limits.
func NewDecoder(r io.Reader) *Decoder {
	return NewDecoderLimits(r, DefaultLimits())
}

// NewDecoderLimits wraps r using limits; non-positive fields fall back to
// the corresponding DefaultLimits entry.
func NewDecoderLimits(r io.Reader, limits Limits) *Decoder {
	d := DefaultLimits()
	if limits.MaxLineLen > 0 {
		d.MaxLineLen = limits.MaxLineLen
	}
	if limits.MaxBulkLen > 0 {
		d.MaxBulkLen = limits.MaxBulkLen
	}
	if limits.MaxArrayLen > 0 {
		d.MaxArrayLen = limits.MaxArrayLen
	}
	if limits.MaxNesting > 0 {
		d.MaxNesting = limits.MaxNesting
	}
	if limits.MaxInlineLen > 0 {
		d.MaxInlineLen = limits.MaxInlineLen
	}
	br, ok := r.(*bufio.Reader)
	if !ok || br.Size() < 64 {
		br = bufio.NewReaderSize(r, 64*1024)
	}
	return &Decoder{r: br, limits: d}
}

// readLine returns one CRLF-terminated line without its trailing CRLF. A
// bare '\n' (missing CR) is a protocol error. Overlong lines are rejected
// without unbounded buffering (the reader peeks ahead one byte at a time).
func (d *Decoder) readLine() ([]byte, error) {
	var line []byte
	for {
		if len(line) >= d.limits.MaxLineLen {
			return nil, NewProtocolError("line exceeds maximum length")
		}
		b, err := d.r.ReadByte()
		if err != nil {
			if err == io.EOF {
				if len(line) == 0 {
					return nil, io.EOF
				}
				return nil, NewTruncatedError("unexpected EOF inside line")
			}
			return nil, err
		}
		if b == '\n' {
			if len(line) == 0 || line[len(line)-1] != '\r' {
				return nil, NewProtocolError("expected CRLF terminator")
			}
			return line[:len(line)-1], nil
		}
		line = append(line, b)
	}
}

// Next reads one complete RESP2 value. Clean EOF between frames returns
// io.EOF; a half frame returns a *ProtocolError with Truncated() true.
func (d *Decoder) Next() (Value, error) {
	for {
		b, err := d.r.ReadByte()
		if err != nil {
			if err == io.EOF {
				return Value{}, io.EOF
			}
			return Value{}, err
		}
		switch b {
		case '+', '-', ':', '$', '*':
			// The frame has begun: any EOF from here is a half packet.
			v, perr := d.parseValue(b, 1)
			if perr == io.EOF {
				return Value{}, NewTruncatedError("unexpected EOF after type byte")
			}
			return v, perr
		default:
			// Inline command: first byte is ordinary text. A blank line
			// (just CRLF) is skipped the way redis-cli's mixed traffic is.
			if err := d.r.UnreadByte(); err != nil {
				return Value{}, err
			}
			v, err := d.parseInline()
			if err == errEmptyInline {
				continue
			}
			return v, err
		}
	}
}

// parseIntegerField parses a header line as a signed 64-bit integer with a
// strict grammar: optional single '-' followed by digits, no spaces, no
// '+' sign, no duplicate signs.
func parseSignedHeader(line []byte) (int64, error) {
	if len(line) == 0 {
		return 0, NewProtocolError("empty integer field")
	}
	neg := false
	i := 0
	if line[0] == '-' {
		neg = true
		i = 1
		if len(line) == 1 {
			return 0, NewProtocolError("lone minus in integer field")
		}
	}
	var n uint64
	// Positive int64 tops out at 2^63-1; the negative range permits one
	// more unit (2^63).
	limit := uint64(1<<63 - 1)
	if neg {
		limit = 1 << 63
	}
	for ; i < len(line); i++ {
		c := line[i]
		if c < '0' || c > '9' {
			return 0, NewProtocolError("non-numeric byte in integer field")
		}
		digit := uint64(c - '0')
		if n > (limit-digit)/10 {
			return 0, NewProtocolError("integer field out of range")
		}
		n = n*10 + digit
	}
	if neg {
		return -int64(n), nil
	}
	return int64(n), nil
}

// readCRLF consumes the two terminator bytes after a bulk payload.
func (d *Decoder) readCRLF() error {
	cr, err := d.r.ReadByte()
	if err != nil {
		if err == io.EOF {
			return NewTruncatedError("unexpected EOF before bulk terminator")
		}
		return err
	}
	lf, err := d.r.ReadByte()
	if err != nil {
		if err == io.EOF {
			return NewTruncatedError("unexpected EOF in bulk terminator")
		}
		return err
	}
	if cr != '\r' || lf != '\n' {
		return NewProtocolError("bulk not terminated by CRLF")
	}
	return nil
}

// readN reads exactly n bytes, mapping a short read to a truncated-frame
// protocol error rather than io.ErrUnexpectedEOF.
func (d *Decoder) readN(n int64) ([]byte, error) {
	buf := make([]byte, n)
	if _, err := io.ReadFull(d.r, buf); err != nil {
		if err == io.EOF || err == io.ErrUnexpectedEOF {
			return nil, NewTruncatedError("unexpected EOF inside bulk payload")
		}
		return nil, err
	}
	return buf, nil
}

func (d *Decoder) parseValue(typeByte byte, level int) (Value, error) {
	line, err := d.readLine()
	if err != nil {
		return Value{}, err
	}
	switch typeByte {
	case '+':
		return SimpleString(string(line)), nil
	case '-':
		return SimpleError(string(line)), nil
	case ':':
		n, err := parseSignedHeader(line)
		if err != nil {
			return Value{}, err
		}
		return Integer(n), nil
	case '$':
		n, err := parseSignedHeader(line)
		if err != nil {
			return Value{}, err
		}
		if n < 0 {
			if n != -1 {
				return Value{}, NewProtocolError("bulk length is neither non-negative nor -1")
			}
			return NullBulk(), nil
		}
		if n > d.limits.MaxBulkLen {
			return Value{}, NewProtocolError("bulk length exceeds maximum: " +
				strconv.FormatInt(n, 10))
		}
		buf, err := d.readN(n)
		if err != nil {
			return Value{}, err
		}
		if err := d.readCRLF(); err != nil {
			return Value{}, err
		}
		return Value{Kind: KindBulk, Bulk: buf}, nil
	case '*':
		n, err := parseSignedHeader(line)
		if err != nil {
			return Value{}, err
		}
		if n < 0 {
			if n != -1 {
				return Value{}, NewProtocolError("array length is neither non-negative nor -1")
			}
			return NullArray(), nil
		}
		if n > d.limits.MaxArrayLen {
			return Value{}, NewProtocolError("array length exceeds maximum: " +
				strconv.FormatInt(n, 10))
		}
		if level > d.limits.MaxNesting {
			return Value{}, NewProtocolError("array nesting exceeds maximum depth")
		}
		elems := make([]Value, 0, capFor(n))
		for i := int64(0); i < n; i++ {
			tb, err := d.r.ReadByte()
			if err != nil {
				if err == io.EOF {
					return Value{}, NewTruncatedError("unexpected EOF inside array")
				}
				return Value{}, err
			}
			switch tb {
			case '+', '-', ':', '$', '*':
				v, err := d.parseValue(tb, level+1)
				if err != nil {
					return Value{}, err
				}
				elems = append(elems, v)
			default:
				if err := d.r.UnreadByte(); err != nil {
					return Value{}, err
				}
				return Value{}, NewProtocolError("array element has invalid type byte")
			}
		}
		return Value{Kind: KindArray, Array: elems}, nil
	default:
		return Value{}, NewProtocolError("unknown RESP type byte")
	}
}

// capFor preallocates small arrays exactly while avoiding huge allocations
// from an adversarial but under-limit header.
func capFor(n int64) int {
	if n <= 1024 {
		return int(n)
	}
	return 1024
}

// errEmptyInline marks a blank inline line; Next skips such lines.
var errEmptyInline = NewProtocolError("empty inline line")

// parseInline reads one line of the whitespace-separated text protocol
// (PING, SUBSCRIBE-style debugging clients use it). Quoting is not
// supported; every token becomes a bulk string, so the result is shaped as
// a normal command array.
func (d *Decoder) parseInline() (Value, error) {
	var line []byte
	for {
		if len(line) >= d.limits.MaxInlineLen {
			return Value{}, NewProtocolError("inline line exceeds maximum length")
		}
		b, err := d.r.ReadByte()
		if err != nil {
			if err == io.EOF {
				if len(line) == 0 {
					return Value{}, io.EOF
				}
				break
			}
			return Value{}, err
		}
		if b == '\n' {
			if len(line) > 0 && line[len(line)-1] == '\r' {
				line = line[:len(line)-1]
			}
			break
		}
		line = append(line, b)
	}
	fields := bytes.Fields(line)
	if len(fields) == 0 {
		return Value{}, errEmptyInline
	}
	elems := make([]Value, len(fields))
	for i, f := range fields {
		elems[i] = BulkBytes(f)
	}
	return ArrayValue(elems...), nil
}
