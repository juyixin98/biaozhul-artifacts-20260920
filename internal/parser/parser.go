// Package parser implements an offline parser for a strict subset of
// HTTP/1.x request wire streams (RFC 7230 framing).
//
// It never opens connections and never forwards traffic: it only examines
// the bytes handed to it. The same state machine drives both the one-shot
// Parse function and the streaming Parser (Feed/Finish), which is the basis
// of the byte-split framing-consistency guarantee.
package parser

import (
	"bytes"
	"strconv"
	"strings"
)

// FrameMode describes how a request's body is delimited on the wire.
type FrameMode string

const (
	// FrameNone: the request has no message body (no Content-Length,
	// no Transfer-Encoding).
	FrameNone FrameMode = "none"
	// FrameContentLength: body delimited by a Content-Length header.
	FrameContentLength FrameMode = "content-length"
	// FrameChunked: body delimited by Transfer-Encoding: chunked.
	FrameChunked FrameMode = "chunked"
)

// Header is one received header field, in wire order. Folded headers are
// rejected, so each struct corresponds to exactly one physical header line.
type Header struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

// Request is one fully parsed HTTP request from the stream.
type Request struct {
	Method         string    `json:"method"`
	Target         string    `json:"target"`
	Version        string    `json:"version"` // "HTTP/1.1" or "HTTP/1.0"
	Headers        []Header  `json:"headers"`
	Frame          FrameMode `json:"frame"`
	ContentLength  int64     `json:"content_length,omitempty"`
	Body           []byte    `json:"body"`
	BodyLength     int       `json:"body_length"`
	TrailingHeader []Header  `json:"trailers,omitempty"`
	StartOffset    int       `json:"start_offset"` // absolute byte offset in the input stream
	RawLength      int       `json:"raw_length"`   // wire bytes consumed for this request
}

// ErrorCode enumerates parser failures.
type ErrorCode string

const (
	ErrBareLF                      ErrorCode = "bare_lf"                       // bare LF, line ending is not CRLF
	ErrInvalidRequestLine          ErrorCode = "invalid_request_line"          // malformed request line
	ErrUnsupportedVersion          ErrorCode = "unsupported_http_version"      // not HTTP/1.0 or HTTP/1.1
	ErrInvalidHeader               ErrorCode = "invalid_header"                // malformed header line / field name
	ErrInvalidFieldValue           ErrorCode = "invalid_field_value"           // illegal control char in field value
	ErrFoldedHeader                ErrorCode = "folded_header"                 // obsolete line folding (RFC 7230 3.2.4)
	ErrInvalidContentLength        ErrorCode = "invalid_content_length"        // CL not 1*DIGIT or overflow
	ErrContentLengthConflict       ErrorCode = "content_length_conflict"       // differing CL field values
	ErrCLAndChunked                ErrorCode = "content_length_and_chunked"    // CL + TE:chunked together
	ErrUnsupportedTransferEncoding ErrorCode = "unsupported_transfer_encoding" // TE subset is exactly "chunked"
	ErrInvalidChunkSize            ErrorCode = "invalid_chunk_size"            // chunk-size not 1*HEXDIG
	ErrChunkDataTruncated          ErrorCode = "chunk_data_truncated"          // stream ends mid-chunk / chunk CRLF
	ErrChunkCRLF                   ErrorCode = "invalid_chunk_terminator"      // chunk data not followed by CRLF
	ErrBodyTruncated               ErrorCode = "body_truncated"                // stream ends before CL bytes
	ErrBodyTooLarge                ErrorCode = "body_too_large"                // declared body exceeds limit
	ErrIncomplete                  ErrorCode = "incomplete_stream"             // stream ended mid-message
)

// Error is a parse failure with an exact byte offset.
//
// Offset is a 0-based absolute index into the full input stream pointing at
// the first offending byte. For truncation errors discovered at end of
// stream it equals the total number of bytes received (i.e. one past the
// last byte), which is where the missing bytes would begin.
type Error struct {
	Code    ErrorCode `json:"code"`
	Offset  int       `json:"offset"`
	Message string    `json:"message"`
}

func (e *Error) Error() string {
	if e == nil {
		return "<nil>"
	}
	return string(e.Code) + " at byte " + strconv.Itoa(e.Offset) + ": " + e.Message
}

// Option configures a Parser.
type Option func(*Parser)

// WithMaxBody limits the accepted decoded body size of one request.
// A value <= 0 means unlimited. The check is performed against the declared
// framing (Content-Length value / chunk-size) before reading the bytes.
func WithMaxBody(n int64) Option {
	return func(p *Parser) { p.maxBody = n }
}

type phase int

const (
	phStartLine phase = iota
	phHeaders
	phBodyCL
	phChunkSize
	phChunkData
	phChunkCRLF
	phTrailer
)

// Parser is a streaming, stateful HTTP/1.x request parser. Feed raw bytes in
// arbitrarily sized chunks; completed requests are returned as soon as they
// are fully framed. Once an error is returned the parser is dead.
type Parser struct {
	maxBody  int64
	consumed int    // absolute bytes already consumed (error offsets)
	buf      []byte // unconsumed tail
	phase    phase
	done     []*Request // all requests completed in this stream

	// per-request state
	cur        *Request
	clSeen     bool
	clValue    int64
	clCount    int
	clLineOffs []int // absolute offset of every Content-Length line
	teSeen     bool
	teLineOff  int
	bodyRemain int64 // CL bytes still expected
	chunkLeft  int64 // bytes left in current chunk
	bodyLen    int   // decoded body bytes accumulated

	err *Error
}

// NewParser creates a streaming parser.
func NewParser(opts ...Option) *Parser {
	p := &Parser{}
	for _, o := range opts {
		o(p)
	}
	return p
}

// Requests returns all requests completed so far (pipelined requests appear
// in wire order).
func (p *Parser) Requests() []*Request { return p.done }

// Err returns the fatal parser error, if any.
func (p *Parser) Err() *Error { return p.err }

func (p *Parser) fail(rel int, code ErrorCode, msg string) *Error {
	e := &Error{Code: code, Offset: p.consumed + rel, Message: msg}
	p.err = e
	p.buf = nil
	return e
}

func (p *Parser) consume(n int) {
	p.buf = p.buf[n:]
	p.consumed += n
}

// findLine locates the next CRLF in b.
//
// Returns end = index just after the LF for a complete CRLF-terminated line,
// bareLF = index of an LF not preceded by CR (a hard error wherever it
// appears). Both are -1 when no complete line is buffered.
func findLine(b []byte) (end, bareLF int) {
	for i := 0; i < len(b); i++ {
		if b[i] == '\n' {
			if i == 0 || b[i-1] != '\r' {
				return -1, i
			}
			return i + 1, -1
		}
	}
	return -1, -1
}

// Feed appends chunk to the parser and returns any requests that became
// complete during this call. Splitting the byte stream at arbitrary
// boundaries never changes the final result.
func (p *Parser) Feed(chunk []byte) ([]*Request, *Error) {
	if p.err != nil {
		return nil, p.err
	}
	p.buf = append(p.buf, chunk...)

	var completed []*Request
	for {
		switch p.phase {

		case phStartLine:
			end, bare := findLine(p.buf)
			if bare >= 0 {
				return completed, p.fail(bare, ErrBareLF, "line ending must be CRLF, bare LF found")
			}
			if end < 0 {
				return completed, nil
			}
			line := p.buf[:end-2]
			if badAt, code, msg := checkRequestLine(line); badAt >= 0 {
				return completed, p.fail(badAt, code, msg)
			}
			parts := splitRequestLine(line)
			p.cur = &Request{
				Method:      string(parts.method),
				Target:      string(parts.target),
				Version:     string(parts.version),
				Headers:     []Header{},
				StartOffset: p.consumed,
			}
			p.clSeen, p.teSeen = false, false
			p.clCount, p.clValue, p.bodyLen = 0, 0, 0
			p.clLineOffs = p.clLineOffs[:0]
			p.consume(end)
			p.phase = phHeaders

		case phHeaders:
			end, bare := findLine(p.buf)
			if bare >= 0 {
				return completed, p.fail(bare, ErrBareLF, "line ending must be CRLF, bare LF found")
			}
			if end < 0 {
				return completed, nil
			}
			line := p.buf[:end-2]
			lineStartAbs := p.consumed

			if len(line) == 0 {
				// Empty line terminates the header block.
				p.consume(end)
				r, err := p.beginBody()
				if err != nil {
					return completed, err
				}
				if r != nil {
					completed = append(completed, r)
				}
				continue
			}

			if line[0] == ' ' || line[0] == '\t' {
				return completed, p.fail(0, ErrFoldedHeader,
					"obsolete header folding (leading whitespace) is not accepted")
			}
			colon := bytes.IndexByte(line, ':')
			if colon <= 0 {
				return completed, p.fail(0, ErrInvalidHeader,
					"header line missing ':' or field name empty")
			}
			name := line[:colon]
			for i := 0; i < len(name); i++ {
				if !isTokenChar(name[i]) {
					return completed, p.fail(i, ErrInvalidHeader,
						"illegal byte in header field name")
				}
			}
			rawValue := line[colon+1:]
			valStart, value := trimOWS(rawValue)
			for i := 0; i < len(value); i++ {
				if !isFieldValueChar(value[i]) {
					return completed, p.fail(colon+1+valStart+i, ErrInvalidFieldValue,
						"illegal control byte in header field value")
				}
			}

			lname := strings.ToLower(string(name))
			if lname == "content-length" {
				v, badRel, ok := parseDecimalCL(rawValue)
				if !ok {
					return completed, p.fail(colon+1+badRel, ErrInvalidContentLength,
						"Content-Length must be 1*DIGIT with no whitespace, signs or hex")
				}
				if p.clCount == 0 {
					p.clValue = v
				} else if v != p.clValue {
					return completed, p.failAtAbs(lineStartAbs, ErrContentLengthConflict,
						"multiple Content-Length header fields with different values")
				}
				p.clCount++
				p.clSeen = true
				p.clLineOffs = append(p.clLineOffs, lineStartAbs)
			} else if lname == "transfer-encoding" {
				p.teSeen = true
				p.teLineOff = lineStartAbs
				// Value validity is enforced when the header block ends so that
				// the error offset is the TE line regardless of line order.
			}

			p.cur.Headers = append(p.cur.Headers, Header{
				Name:  string(name),
				Value: string(value),
			})
			p.consume(end)

		case phBodyCL:
			if int64(len(p.buf)) >= p.bodyRemain {
				p.cur.Body = append(p.cur.Body, p.buf[:p.bodyRemain]...)
				p.bodyLen += int(p.bodyRemain)
				p.consume(int(p.bodyRemain))
				p.bodyRemain = 0
				if r := p.finishRequest(); r != nil {
					completed = append(completed, r)
				}
				continue
			}
			p.cur.Body = append(p.cur.Body, p.buf...)
			p.bodyRemain -= int64(len(p.buf))
			p.bodyLen += len(p.buf)
			p.consume(len(p.buf))
			return completed, nil

		case phChunkSize:
			end, bare := findLine(p.buf)
			if bare >= 0 {
				return completed, p.fail(bare, ErrBareLF, "line ending must be CRLF, bare LF found")
			}
			if end < 0 {
				return completed, nil
			}
			line := p.buf[:end-2]
			sizePart := line
			if semi := bytes.IndexByte(line, ';'); semi >= 0 {
				sizePart = line[:semi] // chunk extensions accepted and ignored
			}
			sizePart = bytes.TrimSpace(sizePart)
			sz, err := strconv.ParseInt(string(sizePart), 16, 64)
			if err != nil || len(sizePart) == 0 {
				return completed, p.fail(0, ErrInvalidChunkSize,
					"chunk-size must be 1*HEXDIG without 0x prefix or signs")
			}
			if p.maxBody > 0 && int64(p.bodyLen)+sz > p.maxBody {
				return completed, p.fail(0, ErrBodyTooLarge,
					"decoded chunked body would exceed configured limit")
			}
			p.consume(end)
			if sz == 0 {
				p.phase = phTrailer
				continue
			}
			p.chunkLeft = sz
			p.phase = phChunkData

		case phChunkData:
			take := p.chunkLeft
			if int64(len(p.buf)) < take {
				take = int64(len(p.buf))
			}
			p.cur.Body = append(p.cur.Body, p.buf[:take]...)
			p.chunkLeft -= take
			p.bodyLen += int(take)
			p.consume(int(take))
			if p.chunkLeft > 0 {
				return completed, nil
			}
			p.phase = phChunkCRLF

		case phChunkCRLF:
			if len(p.buf) == 0 {
				return completed, nil
			}
			if p.buf[0] != '\r' {
				return completed, p.fail(0, ErrChunkCRLF, "chunk data must be followed by CRLF")
			}
			if len(p.buf) < 2 {
				return completed, nil
			}
			if p.buf[1] != '\n' {
				return completed, p.fail(1, ErrChunkCRLF, "chunk data must be followed by CRLF")
			}
			p.consume(2)
			p.phase = phChunkSize

		case phTrailer:
			end, bare := findLine(p.buf)
			if bare >= 0 {
				return completed, p.fail(bare, ErrBareLF, "line ending must be CRLF, bare LF found")
			}
			if end < 0 {
				return completed, nil
			}
			line := p.buf[:end-2]
			if len(line) == 0 {
				p.consume(end)
				if r := p.finishRequest(); r != nil {
					completed = append(completed, r)
				}
				continue
			}
			if line[0] == ' ' || line[0] == '\t' {
				return completed, p.fail(0, ErrFoldedHeader,
					"obsolete header folding is not accepted in trailers")
			}
			colon := bytes.IndexByte(line, ':')
			if colon <= 0 {
				return completed, p.fail(0, ErrInvalidHeader,
					"malformed trailer line")
			}
			name := line[:colon]
			for i := 0; i < len(name); i++ {
				if !isTokenChar(name[i]) {
					return completed, p.fail(i, ErrInvalidHeader,
						"illegal byte in trailer field name")
				}
			}
			_, value := trimOWS(line[colon+1:])
			for i := 0; i < len(value); i++ {
				if !isFieldValueChar(value[i]) {
					return completed, p.fail(colon+1+i, ErrInvalidFieldValue,
						"illegal control byte in trailer field value")
				}
			}
			p.cur.TrailingHeader = append(p.cur.TrailingHeader, Header{
				Name:  string(name),
				Value: string(value),
			})
			p.consume(end)
		}
	}
}

func (p *Parser) failAtAbs(abs int, code ErrorCode, msg string) *Error {
	e := &Error{Code: code, Offset: abs, Message: msg}
	p.err = e
	p.buf = nil
	return e
}

// beginBody validates the finished header block and moves into the body
// phase. It implements the request-smuggling defenses:
//   - Transfer-Encoding and Content-Length together are rejected;
//   - the supported TE subset is exactly the single coding "chunked";
//   - conflicting Content-Length values are rejected.
//
// For a bodyless request it finishes the request immediately and returns it.
func (p *Parser) beginBody() (*Request, *Error) {
	if p.teSeen {
		teVal := ""
		for _, h := range p.cur.Headers {
			if strings.EqualFold(h.Name, "transfer-encoding") {
				if teVal != "" {
					return nil, p.failAtAbs(p.teLineOff, ErrUnsupportedTransferEncoding,
						"only a single Transfer-Encoding: chunked field is supported")
				}
				teVal = h.Value
			}
		}
		codings := strings.Split(teVal, ",")
		if len(codings) != 1 || !strings.EqualFold(strings.TrimSpace(codings[0]), "chunked") {
			return nil, p.failAtAbs(p.teLineOff, ErrUnsupportedTransferEncoding,
				`supported Transfer-Encoding subset is exactly "chunked"`)
		}
		if p.clSeen {
			return nil, p.failAtAbs(p.teLineOff, ErrCLAndChunked,
				"Content-Length and Transfer-Encoding: chunked must not appear together")
		}
		p.cur.Frame = FrameChunked
		p.phase = phChunkSize
		return nil, nil
	}

	if p.clSeen {
		p.cur.Frame = FrameContentLength
		p.cur.ContentLength = p.clValue
		if p.maxBody > 0 && p.clValue > p.maxBody {
			return nil, p.fail(0, ErrBodyTooLarge,
				"declared Content-Length exceeds configured body limit")
		}
		p.bodyRemain = p.clValue
		p.phase = phBodyCL
		return nil, nil
	}

	p.cur.Frame = FrameNone
	return p.finishRequest(), nil
}

func (p *Parser) finishRequest() *Request {
	p.cur.BodyLength = len(p.cur.Body)
	p.cur.RawLength = p.consumed - p.cur.StartOffset
	p.done = append(p.done, p.cur)
	r := p.cur
	p.cur = nil
	p.phase = phStartLine
	return r
}

// Finish marks the end of the byte stream. It reports an Incomplete-family
// error when bytes of a message are still expected, otherwise nil.
func (p *Parser) Finish() *Error {
	if p.err != nil {
		return p.err
	}
	if p.phase == phStartLine && len(p.buf) == 0 {
		return nil
	}
	off := p.consumed + len(p.buf)
	switch p.phase {
	case phBodyCL:
		return p.failAtAbs(off, ErrBodyTruncated, "stream ended before Content-Length body was complete")
	case phChunkData, phChunkCRLF:
		return p.failAtAbs(off, ErrChunkDataTruncated, "stream ended in the middle of a chunk")
	case phChunkSize, phTrailer:
		return p.failAtAbs(off, ErrChunkDataTruncated, "stream ended before chunked body terminated")
	default:
		return p.failAtAbs(off, ErrIncomplete, "stream ended in the middle of request line or headers")
	}
}

// Parse parses one complete offline byte stream (which may contain a
// pipeline of several requests). It is equivalent to feeding the whole
// input to a new Parser in one call and then Finish.
func Parse(input []byte, opts ...Option) ([]*Request, *Error) {
	p := NewParser(opts...)
	reqs, err := p.Feed(input)
	if err != nil {
		return reqs, err
	}
	if err := p.Finish(); err != nil {
		return p.Requests(), err
	}
	return p.Requests(), nil
}

// ---- character classes and small line helpers (RFC 7230 token / field-value) ----

func isTokenChar(c byte) bool {
	switch {
	case c >= 'a' && c <= 'z':
		return true
	case c >= 'A' && c <= 'Z':
		return true
	case c >= '0' && c <= '9':
		return true
	}
	switch c {
	case '!', '#', '$', '%', '&', '\'', '*', '+', '-', '.', '^', '_', '`', '|', '~':
		return true
	}
	return false
}

// isFieldValueChar accepts HTAB, SP, VCHAR (0x21-0x7E) and obs-text
// (0x80-0xFF); every other byte is a disallowed control character.
func isFieldValueChar(c byte) bool {
	if c == '\t' || c == ' ' {
		return true
	}
	if c >= 0x21 && c <= 0x7E {
		return true
	}
	if c >= 0x80 {
		return true
	}
	return false
}

// trimOWS strips optional whitespace (SP/HTAB) from both ends and returns
// the start offset of the trimmed value within raw.
func trimOWS(raw []byte) (start int, trimmed []byte) {
	s, e := 0, len(raw)
	for s < e && (raw[s] == ' ' || raw[s] == '\t') {
		s++
	}
	for e > s && (raw[e-1] == ' ' || raw[e-1] == '\t') {
		e--
	}
	return s, raw[s:e]
}

// parseDecimalCL validates a Content-Length field value. Only 1*DIGIT is
// accepted (no signs, hex, underscores or interior whitespace; leading OWS
// is tolerated per header parsing). badRel is the offset relative to raw of
// the first offending byte.
func parseDecimalCL(raw []byte) (val int64, badRel int, ok bool) {
	s, digits := trimOWS(raw)
	if len(digits) == 0 {
		return 0, s, false
	}
	for i := 0; i < len(digits); i++ {
		if digits[i] < '0' || digits[i] > '9' {
			return 0, s + i, false
		}
	}
	v, err := strconv.ParseInt(string(digits), 10, 64)
	if err != nil {
		return 0, s, false
	}
	return v, 0, true
}

type reqLineParts struct {
	method  []byte
	target  []byte
	version []byte
}

// splitRequestLine splits "METHOD SP TARGET SP HTTP/1.x". Caller must have
// validated with checkRequestLine first.
func splitRequestLine(line []byte) reqLineParts {
	sp1 := bytes.IndexByte(line, ' ')
	rest := line[sp1+1:]
	sp2 := bytes.IndexByte(rest, ' ')
	return reqLineParts{
		method:  line[:sp1],
		target:  rest[:sp2],
		version: rest[sp2+1:],
	}
}

// checkRequestLine validates the request line and returns the offset of the
// first bad byte (relative to line) with an error code.
func checkRequestLine(line []byte) (badAt int, code ErrorCode, msg string) {
	sp1 := bytes.IndexByte(line, ' ')
	if sp1 < 0 {
		return len(line), ErrInvalidRequestLine, "request line must be: METHOD SP TARGET SP HTTP-version"
	}
	method := line[:sp1]
	if len(method) == 0 {
		return 0, ErrInvalidRequestLine, "empty request method"
	}
	for i := 0; i < len(method); i++ {
		if !isTokenChar(method[i]) {
			return i, ErrInvalidRequestLine, "illegal byte in request method"
		}
	}
	if sp1+1 >= len(line) {
		return len(line), ErrInvalidRequestLine, "missing request target"
	}
	i := sp1 + 1
	for ; i < len(line) && line[i] != ' '; i++ {
		if line[i] < 0x21 || line[i] > 0x7E {
			return i, ErrInvalidRequestLine, "illegal byte in request target"
		}
	}
	if i == sp1+1 {
		return sp1 + 1, ErrInvalidRequestLine, "empty request target"
	}
	if i >= len(line) {
		return len(line), ErrInvalidRequestLine, "missing SP before HTTP version"
	}
	version := line[i+1:]
	if string(version) != "HTTP/1.1" && string(version) != "HTTP/1.0" {
		return i + 1, ErrUnsupportedVersion, "only HTTP/1.1 and HTTP/1.0 request lines are supported"
	}
	return -1, "", ""
}
