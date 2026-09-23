// Package frame implements an offline, incremental parser for a strict subset
// of HTTP/1.1 request framing (RFC 7230/9112).
//
// It never opens sockets or forwards traffic: it is fed raw request bytes and
// reports parsed messages, or a *Error whose Offset is an absolute byte
// position inside the fed stream.
//
// Supported framing:
//   - Content-Length (single value; repeated identical fields accepted)
//   - Transfer-Encoding: chunked (chunk extensions and trailers are parsed)
//
// Rejected, with a precise offset:
//   - Content-Length and Transfer-Encoding present together
//   - Conflicting/duplicated Content-Length values, malformed lengths
//   - Obsolete header line folding (leading SP/HT on a header line)
//   - Bare LF / stray CR line endings, NUL bytes in request line or fields
//   - Any Transfer-Encoding other than exactly "chunked"
//   - Truncated messages and chunks
package frame

import (
	"bytes"
	"fmt"
	"strings"
)

// Error codes returned in Error.Code.
const (
	ErrTruncated          = "truncated"
	ErrBadRequestLine     = "bad_request_line"
	ErrBareLF             = "bare_lf"
	ErrStrayCR            = "stray_cr"
	ErrNULByte            = "nul_byte"
	ErrBadHeader          = "bad_header"
	ErrFoldedHeader       = "folded_header"
	ErrBadContentLength   = "bad_content_length"
	ErrContentLenConflict = "content_length_conflict"
	ErrCLAndChunked       = "content_length_and_transfer_encoding"
	ErrBadTransferEnc     = "bad_transfer_encoding"
	ErrBodyTooLarge       = "body_too_large"
	ErrBadChunkSize       = "bad_chunk_size"
	ErrBadChunkExt        = "bad_chunk_extension"
	ErrBadChunkTerm       = "bad_chunk_terminator"
	ErrBadTrailer         = "bad_trailer"
	ErrTooManyMessages    = "too_many_messages"
)

// Error is a framing error anchored at an absolute byte offset.
type Error struct {
	Code   string `json:"code"`
	Offset int    `json:"offset"`
	Msg    string `json:"message"`
}

func (e *Error) Error() string {
	return fmt.Sprintf("%s at offset %d: %s", e.Code, e.Offset, e.Msg)
}

// Header is one received header field, with its line's absolute offset.
type Header struct {
	Name      string
	Value     string
	LineStart int // absolute offset of the first byte of the field line
}

// Chunk is one chunked-coding chunk.
type Chunk struct {
	Index         int
	Size          uint64
	Extension     string // raw bytes between ';' and CRLF, empty if none
	SizeLineStart int    // absolute offset of the chunk-size line
	Data          []byte
	DataStart     int // absolute offset of the first chunk-data byte
}

// Message is one fully parsed HTTP/1.1 request.
type Message struct {
	Method      string
	Target      string
	Proto       string // always "HTTP/1.1"
	Start       int    // absolute offset of the request line
	Headers     []Header
	HeaderStart int // absolute offset of the first header line
	HeaderEnd   int // absolute offset immediately after the blank terminating line (= body start)

	Chunked bool
	// Body is the entity body: Content-Length bytes, or the concatenation
	// of all chunk payloads (chunk framing stripped).
	Body       []byte
	BodyStart  int // CL: offset of first body byte. Chunked: offset of first chunk-data byte (0 if no chunks).
	BodyLength int64

	Chunks   []Chunk
	Trailers []Header
	End      int // absolute offset immediately after the final message byte
}

const (
	phaseIdle = iota
	phaseReqLine
	phaseHeaders
	phaseCLBody
	phaseChunkSize
	phaseChunkData
	phaseChunkCRLF
	phaseTrailer
)

const defaultMaxBody = 10 << 20 // 10 MiB

type options struct {
	maxBody     int64
	maxMessages int
}

// Option configures a Decoder.
type Option func(*options)

// WithMaxBody limits the accepted entity body to n bytes. n <= 0 disables.
func WithMaxBody(n int64) Option { return func(o *options) { o.maxBody = n } }

// WithMaxMessages limits the number of pipelined messages accepted. n <= 0 is unlimited.
func WithMaxMessages(n int) Option { return func(o *options) { o.maxMessages = n } }

// Decoder is a stateful, incremental HTTP/1.1 request-framing parser.
// Feed bytes with Write as they arrive; call Close at end of stream.
// Once a hard *Error is returned the decoder is permanently failed; an
// unterminated stream reported at Close is ErrTruncated, which is likewise fatal.
type Decoder struct {
	maxBody     int64
	maxMessages int

	buf    []byte
	total  int // absolute offset of buf[0]
	closed bool

	phase int
	msgs  []*Message
	msg   *Message

	// header-block accumulators
	clValue       uint64
	clValueSet    bool
	clHeaderStart int
	te            bool
	teHeaderStart int

	// Content-Length body
	clNeed int64

	// chunked
	curChunk *Chunk
	curGot   int64
	bodyHave int64

	fatal bool
	err   *Error
}

// NewDecoder creates a Decoder. Default body limit is 10 MiB.
func NewDecoder(opts ...Option) *Decoder {
	o := options{maxBody: defaultMaxBody}
	for _, opt := range opts {
		opt(&o)
	}
	return &Decoder{maxBody: o.maxBody, maxMessages: o.maxMessages, phase: phaseIdle}
}

func (d *Decoder) fail(code string, offset int, format string, args ...any) *Error {
	e := &Error{Code: code, Offset: offset, Msg: fmt.Sprintf(format, args...)}
	d.fatal, d.err = true, e
	return e
}

func (d *Decoder) consume(n int) {
	d.buf = d.buf[n:]
	d.total += n
}

// peekLine returns the next CRLF-terminated line (terminator excluded), plus
// its raw length including CRLF. A bare LF, stray CR or NUL is a hard error
// with an exact offset. ok==false means more bytes are needed (or, when the
// decoder is closed, the caller treats the residue as truncated).
func (d *Decoder) peekLine() (data []byte, rawLen int, ok bool, err *Error) {
	i := bytes.IndexByte(d.buf, '\n')
	if i < 0 {
		return nil, 0, false, nil
	}
	if i == 0 || d.buf[i-1] != '\r' {
		return nil, 0, false, d.fail(ErrBareLF, d.total+i, "line not terminated by CRLF")
	}
	line := d.buf[:i-1]
	for j, b := range line {
		switch b {
		case '\r':
			return nil, 0, false, d.fail(ErrStrayCR, d.total+j, "stray CR inside line")
		case 0:
			return nil, 0, false, d.fail(ErrNULByte, d.total+j, "NUL byte in line")
		}
	}
	return line, i + 1, true, nil
}

func (d *Decoder) beginMessage() {
	d.msg = &Message{Start: d.total, HeaderStart: -1, BodyStart: -1}
	d.clValueSet = false
	d.te = false
	d.clHeaderStart = -1
	d.teHeaderStart = -1
	d.clNeed = 0
	d.curChunk = nil
	d.curGot = 0
	d.bodyHave = 0
}

func (d *Decoder) finishMessage() {
	d.msg.End = d.total
	d.msgs = append(d.msgs, d.msg)
	d.msg = nil
	d.phase = phaseIdle
}

// Write feeds more raw bytes and returns any messages completed by this write.
func (d *Decoder) Write(p []byte) ([]*Message, *Error) {
	if d.fatal {
		return nil, d.err
	}
	d.buf = append(d.buf, p...)
	start := len(d.msgs)
	if err := d.pump(); err != nil {
		return d.msgs[start:], err
	}
	return d.msgs[start:], nil
}

// Close signals end of stream. A message in progress is ErrTruncated.
func (d *Decoder) Close() ([]*Message, *Error) {
	if d.fatal {
		return nil, d.err
	}
	d.closed = true
	start := len(d.msgs)
	if err := d.pump(); err != nil {
		return d.msgs[start:], err
	}
	if d.phase != phaseIdle {
		off := d.total + len(d.buf)
		return d.msgs[start:], d.fail(ErrTruncated, off, "unexpected end of stream: incomplete message")
	}
	return d.msgs[start:], nil
}

func (d *Decoder) pump() *Error {
	for {
		switch d.phase {
		case phaseIdle:
			if d.maxMessages > 0 && len(d.msgs) >= d.maxMessages {
				if len(d.buf) > 0 {
					return d.fail(ErrTooManyMessages, d.total, "message limit %d exceeded; trailing bytes at %d", d.maxMessages, d.total)
				}
				return nil
			}
			if len(d.buf) == 0 {
				return nil
			}
			d.beginMessage()
			d.phase = phaseReqLine

		case phaseReqLine:
			line, raw, ok, herr := d.peekLine()
			if herr != nil {
				return herr
			}
			if !ok {
				return nil
			}
			if e := d.parseRequestLine(line); e != nil {
				return e
			}
			d.consume(raw)
			d.msg.HeaderStart = d.total
			d.phase = phaseHeaders

		case phaseHeaders:
			line, raw, ok, herr := d.peekLine()
			if herr != nil {
				return herr
			}
			if !ok {
				return nil
			}
			lineStart := d.total
			if len(line) == 0 {
				headerEnd := lineStart + raw
				d.consume(raw)
				if e := d.endHeaders(headerEnd); e != nil {
					return e
				}
				continue
			}
			if line[0] == ' ' || line[0] == '\t' {
				return d.fail(ErrFoldedHeader, lineStart, "obsolete line folding (leading OWS) is not allowed")
			}
			if e := d.parseHeader(line, lineStart); e != nil {
				return e
			}
			d.consume(raw)

		case phaseCLBody:
			need := d.clNeed - int64(len(d.msg.Body))
			avail := int64(len(d.buf))
			if avail < need {
				d.msg.Body = append(d.msg.Body, d.buf...)
				d.consume(len(d.buf))
				return nil
			}
			d.msg.Body = append(d.msg.Body, d.buf[:need]...)
			d.consume(int(need))
			d.msg.BodyLength = int64(len(d.msg.Body))
			d.finishMessage()

		case phaseChunkSize:
			line, raw, ok, herr := d.peekLine()
			if herr != nil {
				return herr
			}
			if !ok {
				return nil
			}
			lineStart := d.total
			size, ext, e := d.parseChunkSizeLine(line, lineStart)
			if e != nil {
				return e
			}
			d.consume(raw)
			if d.maxBody > 0 && size > uint64(d.maxBody-d.bodyHave) {
				return d.fail(ErrBodyTooLarge, lineStart, "declared body exceeds limit of %d bytes", d.maxBody)
			}
			d.curGot = 0
			d.curChunk = &Chunk{
				Index:         len(d.msg.Chunks),
				Size:          size,
				Extension:     ext,
				SizeLineStart: lineStart,
				DataStart:     -1,
			}
			if size == 0 {
				d.phase = phaseTrailer
			} else {
				d.phase = phaseChunkData
			}

		case phaseChunkData:
			need := int64(d.curChunk.Size) - d.curGot
			avail := int64(len(d.buf))
			if d.curGot == 0 {
				d.curChunk.DataStart = d.total
				if d.msg.BodyStart < 0 {
					d.msg.BodyStart = d.total
				}
			}
			if avail < need {
				d.curChunk.Data = append(d.curChunk.Data, d.buf...)
				d.msg.Body = append(d.msg.Body, d.buf...)
				d.curGot += avail
				d.bodyHave += avail
				d.consume(len(d.buf))
				return nil
			}
			d.curChunk.Data = append(d.curChunk.Data, d.buf[:need]...)
			d.msg.Body = append(d.msg.Body, d.buf[:need]...)
			d.curGot += need
			d.bodyHave += need
			d.consume(int(need))
			d.msg.Chunks = append(d.msg.Chunks, *d.curChunk)
			d.curChunk = nil
			d.phase = phaseChunkCRLF

		case phaseChunkCRLF:
			if len(d.buf) < 2 {
				return nil
			}
			if d.buf[0] != '\r' {
				return d.fail(ErrBadChunkTerm, d.total, "expected CRLF after chunk data")
			}
			if d.buf[1] != '\n' {
				return d.fail(ErrBadChunkTerm, d.total+1, "expected LF after CR in chunk terminator")
			}
			d.consume(2)
			d.phase = phaseChunkSize

		case phaseTrailer:
			line, raw, ok, herr := d.peekLine()
			if herr != nil {
				return herr
			}
			if !ok {
				return nil
			}
			lineStart := d.total
			if len(line) == 0 {
				d.consume(raw)
				d.msg.BodyLength = d.bodyHave
				d.finishMessage()
				continue
			}
			if line[0] == ' ' || line[0] == '\t' {
				return d.fail(ErrFoldedHeader, lineStart, "obsolete line folding in trailer is not allowed")
			}
			h, e := d.parseOneHeader(line, lineStart)
			if e != nil {
				return e
			}
			if strings.EqualFold(h.Name, "Content-Length") || strings.EqualFold(h.Name, "Transfer-Encoding") {
				return d.fail(ErrBadTrailer, lineStart, "%s is not allowed in trailer", h.Name)
			}
			d.msg.Trailers = append(d.msg.Trailers, h)
			d.consume(raw)
		}
	}
}

func (d *Decoder) parseRequestLine(line []byte) *Error {
	off := d.total
	parts := bytes.Split(line, []byte{' '})
	if len(parts) != 3 {
		return d.fail(ErrBadRequestLine, off, "request line must be exactly \"METHOD SP TARGET SP HTTP/1.1\"")
	}
	method, target, version := parts[0], parts[1], parts[2]
	if !isToken(method) {
		return d.fail(ErrBadRequestLine, off, "invalid method token")
	}
	if !isRequestTarget(target) {
		return d.fail(ErrBadRequestLine, off, "invalid request target")
	}
	if !bytes.Equal(version, []byte("HTTP/1.1")) {
		return d.fail(ErrBadRequestLine, off, "only HTTP/1.1 is supported")
	}
	d.msg.Method = string(method)
	d.msg.Target = string(target)
	d.msg.Proto = "HTTP/1.1"
	return nil
}

func (d *Decoder) parseHeader(line []byte, lineStart int) *Error {
	h, e := d.parseOneHeader(line, lineStart)
	if e != nil {
		return e
	}
	d.msg.Headers = append(d.msg.Headers, h)
	switch {
	case strings.EqualFold(h.Name, "Content-Length"):
		v, ok := parseCLValue(h.Value)
		if !ok {
			return d.fail(ErrBadContentLength, h.LineStart, "invalid Content-Length %q", h.Value)
		}
		if d.clValueSet && v != d.clValue {
			return d.fail(ErrContentLenConflict, h.LineStart, "conflicting Content-Length values: %d vs %d", d.clValue, v)
		}
		d.clValue, d.clValueSet = v, true
		if d.clHeaderStart < 0 {
			d.clHeaderStart = h.LineStart
		}
	case strings.EqualFold(h.Name, "Transfer-Encoding"):
		if d.te {
			return d.fail(ErrBadTransferEnc, h.LineStart, "only a single Transfer-Encoding field is supported")
		}
		if !strings.EqualFold(strings.TrimSpace(h.Value), "chunked") {
			return d.fail(ErrBadTransferEnc, h.LineStart, "only Transfer-Encoding: chunked is supported, got %q", h.Value)
		}
		d.te, d.teHeaderStart = true, h.LineStart
	}
	return nil
}

func (d *Decoder) parseOneHeader(line []byte, lineStart int) (Header, *Error) {
	colon := bytes.IndexByte(line, ':')
	if colon <= 0 {
		return Header{}, d.fail(ErrBadHeader, lineStart, "header field missing ':'")
	}
	name := line[:colon]
	if !isToken(name) {
		return Header{}, d.fail(ErrBadHeader, lineStart, "invalid header name %q", string(name))
	}
	value := trimOWS(line[colon+1:])
	if !isFieldValue(value) {
		return Header{}, d.fail(ErrBadHeader, lineStart, "invalid field value for %q", string(name))
	}
	return Header{Name: string(name), Value: string(value), LineStart: lineStart}, nil
}

func (d *Decoder) endHeaders(bodyStart int) *Error {
	d.msg.HeaderEnd = bodyStart
	if d.clValueSet && d.te {
		later := d.teHeaderStart
		if d.clHeaderStart > later {
			later = d.clHeaderStart
		}
		return d.fail(ErrCLAndChunked, later, "Content-Length and Transfer-Encoding must not appear together")
	}
	switch {
	case d.te:
		d.msg.Chunked = true
		d.phase = phaseChunkSize
	case d.clValueSet:
		d.msg.Chunked = false
		d.clNeed = int64(d.clValue)
		if d.maxBody > 0 && d.clNeed > d.maxBody {
			return d.fail(ErrBodyTooLarge, bodyStart, "declared body of %d bytes exceeds limit %d", d.clNeed, d.maxBody)
		}
		d.msg.BodyStart = bodyStart
		d.phase = phaseCLBody
	default:
		d.msg.BodyStart = bodyStart
		d.msg.BodyLength = 0
		d.finishMessage()
	}
	return nil
}

func (d *Decoder) parseChunkSizeLine(line []byte, lineStart int) (size uint64, ext string, err *Error) {
	semi := bytes.IndexByte(line, ';')
	sizePart := line
	if semi >= 0 {
		sizePart = line[:semi]
	}
	if len(sizePart) == 0 {
		return 0, "", d.fail(ErrBadChunkSize, lineStart, "empty chunk size")
	}
	if semi >= 0 {
		ext = string(line[semi+1:])
	}
	var v uint64
	for i, b := range sizePart {
		x, ok := hexVal(b)
		if !ok {
			return 0, "", d.fail(ErrBadChunkSize, lineStart+i, "invalid hex digit %q in chunk size", b)
		}
		if v > (1<<60)-1 {
			return 0, "", d.fail(ErrBadChunkSize, lineStart, "chunk size overflows 64 bits")
		}
		v = v<<4 | uint64(x)
	}
	if semi >= 0 {
		if e := d.validateChunkExt(line[semi+1:], lineStart+semi+1); e != nil {
			return 0, "", e
		}
	}
	return v, ext, nil
}

// validateChunkExt parses chunk-ext = *( BWS ";" BWS chunk-ext-name
// [ BWS "=" BWS chunk-ext-val ] ), i.e. ';'-separated token parameters with
// optional token/quoted-string values. The error offset is absolute.
func (d *Decoder) validateChunkExt(p []byte, base int) *Error {
	i := 0
	skipWS := func() {
		for i < len(p) && (p[i] == ' ' || p[i] == '\t') {
			i++
		}
	}
	skipWS()
	if i >= len(p) {
		return d.fail(ErrBadChunkExt, base+i, "empty chunk extension name")
	}
	for {
		nameStart := i
		for i < len(p) && isExtTokenChar(p[i]) {
			i++
		}
		if i == nameStart {
			return d.fail(ErrBadChunkExt, base+i, "invalid chunk extension name")
		}
		skipWS()
		if i < len(p) && p[i] == '=' {
			i++
			skipWS()
			if i < len(p) && p[i] == '"' {
				i++
				closed := false
				for i < len(p) {
					c := p[i]
					if c == '"' {
						i++
						closed = true
						break
					}
					if c == '\\' {
						i++
						if i >= len(p) {
							return d.fail(ErrBadChunkExt, base+i, "unterminated quoted-pair in chunk extension")
						}
						q := p[i]
						if q == ' ' || q == '\t' || isVChar(q) {
							i++
							continue
						}
						return d.fail(ErrBadChunkExt, base+i, "bad quoted-pair in chunk extension")
					}
					if c == ' ' || c == '\t' || isVChar(c) {
						i++
						continue
					}
					return d.fail(ErrBadChunkExt, base+i, "bad byte in quoted extension value")
				}
				if !closed {
					return d.fail(ErrBadChunkExt, base+len(p), "unterminated quoted extension value")
				}
			} else {
				vStart := i
				for i < len(p) && isExtTokenChar(p[i]) {
					i++
				}
				if i == vStart {
					return d.fail(ErrBadChunkExt, base+i, "invalid chunk extension value")
				}
			}
			skipWS()
		}
		if i == len(p) {
			return nil
		}
		if p[i] != ';' {
			return d.fail(ErrBadChunkExt, base+i, "expected ';' in chunk extension")
		}
		i++
		skipWS()
		if i == len(p) {
			return d.fail(ErrBadChunkExt, base+i, "trailing ';' in chunk extension")
		}
	}
}

func isExtTokenChar(b byte) bool {
	if b >= 'a' && b <= 'z' || b >= 'A' && b <= 'Z' || b >= '0' && b <= '9' {
		return true
	}
	switch b {
	case '!', '#', '$', '%', '&', '\'', '*', '+', '-', '.', '^', '_', '`', '|', '~':
		return true
	}
	return false
}

func hexVal(b byte) (int, bool) {
	switch {
	case b >= '0' && b <= '9':
		return int(b - '0'), true
	case b >= 'a' && b <= 'f':
		return int(b-'a') + 10, true
	case b >= 'A' && b <= 'F':
		return int(b-'A') + 10, true
	}
	return 0, false
}

func parseCLValue(v string) (uint64, bool) {
	if len(v) == 0 {
		return 0, false
	}
	var n uint64
	for i := 0; i < len(v); i++ {
		c := v[i]
		if c < '0' || c > '9' {
			return 0, false
		}
		n1 := n*10 + uint64(c-'0')
		if n1 < n {
			return 0, false
		}
		n = n1
	}
	return n, true
}

func trimOWS(b []byte) []byte {
	return bytes.Trim(b, " \t")
}

func isVChar(b byte) bool { return b >= 0x21 && b <= 0x7E || b >= 0x80 }

func isFieldValue(b []byte) bool {
	for _, c := range b {
		if c == '\t' {
			continue
		}
		if c < 0x20 || c == 0x7F {
			return false
		}
	}
	return true
}

func isToken(b []byte) bool {
	if len(b) == 0 {
		return false
	}
	for _, c := range b {
		if c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' {
			continue
		}
		switch c {
		case '!', '#', '$', '%', '&', '\'', '*', '+', '-', '.', '^', '_', '`', '|', '~':
			continue
		default:
			return false
		}
	}
	return true
}

func isRequestTarget(b []byte) bool {
	if len(b) == 0 {
		return false
	}
	for _, c := range b {
		if c <= 0x20 || c == 0x7F {
			return false
		}
	}
	return true
}

// Parse feeds the complete buffer to a single-message decoder and closes it.
// Trailing bytes after one complete request are reported as
// ErrTooManyMessages at the start of the second request; use ParsePipeline
// for intentionally pipelined input.
func Parse(data []byte, opts ...Option) (*Message, *Error) {
	d := NewDecoder(append(opts, WithMaxMessages(1))...)
	if _, e := d.Write(data); e != nil {
		return nil, e
	}
	if _, e := d.Close(); e != nil {
		return nil, e
	}
	if len(d.msgs) == 0 {
		return nil, &Error{Code: ErrTruncated, Offset: len(data), Msg: "empty input"}
	}
	return d.msgs[0], nil
}

// ParsePipeline feeds the complete buffer and returns every fully parsed,
// pipelined message.
func ParsePipeline(data []byte, opts ...Option) ([]*Message, *Error) {
	d := NewDecoder(opts...)
	if _, e := d.Write(data); e != nil {
		return nil, e
	}
	if _, e := d.Close(); e != nil {
		return nil, e
	}
	return d.msgs, nil
}
