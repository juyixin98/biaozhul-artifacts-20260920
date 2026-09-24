// Package smtpd implements a small loopback-only SMTP receiver.
//
// It supports the command subset EHLO/HELO, MAIL FROM, RCPT TO, DATA, RSET,
// NOOP and QUIT. Commands sent out of the enveloppe order are rejected with
// 503. Message bodies use CRLF framing with dot transparency (RFC 5321
// section 4.5.2). Completed messages are handed to a sink exactly once; a
// connection that drops, or a message that exceeds the size limit, before
// the terminating <CRLF>.<CRLF> never reaches the sink.
package smtpd

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"net"
	"strings"
	"sync"
	"time"

	"smtprecv/internal/store"
)

// Sink receives completed mail transactions. The store satisfies it.
type Sink interface {
	Add(from string, to []string, data string) (*store.Message, error)
}

// Config configures a Server. Zero values are replaced with defaults.
type Config struct {
	ListenAddr     string        // TCP listen address, must be loopback
	Hostname       string        // advertised in greetings / EHLO
	MaxMessageSize int           // maximum accepted body size in bytes
	MaxRecipients  int           // maximum accepted recipients per transaction
	CommandTimeout time.Duration // idle timeout between commands (0 disables)
}

// Server is a loopback-only SMTP receiver.
type Server struct {
	cfg    Config
	ln     net.Listener
	sink   Sink
	wg     sync.WaitGroup
	mu     sync.Mutex
	closed bool
	conns  map[net.Conn]struct{}
}

const (
	defaultMaxMessageSize = 1 << 20 // 1 MiB
	defaultMaxRecipients  = 100
	defaultTimeout        = 2 * time.Minute
	maxCommandLine        = 8192
	maxDataLine           = 1 << 16 // 64 KiB, generous per-line cap while reading DATA
)

// New validates the configuration and creates a server. The listen address
// must resolve to a loopback address: this receiver is a test tool and must
// never be reachable from the network.
func New(cfg Config, sink Sink) (*Server, error) {
	if cfg.ListenAddr == "" {
		cfg.ListenAddr = "127.0.0.1:2525"
	}
	if cfg.Hostname == "" {
		cfg.Hostname = "localhost"
	}
	if cfg.MaxMessageSize <= 0 {
		cfg.MaxMessageSize = defaultMaxMessageSize
	}
	if cfg.MaxRecipients <= 0 {
		cfg.MaxRecipients = defaultMaxRecipients
	}
	if cfg.CommandTimeout <= 0 {
		cfg.CommandTimeout = defaultTimeout
	}
	host, _, err := net.SplitHostPort(cfg.ListenAddr)
	if err != nil {
		return nil, fmt.Errorf("invalid listen address %q: %w", cfg.ListenAddr, err)
	}
	if err := requireLoopback(host); err != nil {
		return nil, err
	}
	return &Server{cfg: cfg, sink: sink, conns: map[net.Conn]struct{}{}}, nil
}

// requireLoopback fails unless host is a loopback address (or a name that
// resolves only to loopback addresses).
func requireLoopback(host string) error {
	if host == "" || host == "localhost" {
		return nil
	}
	ip := net.ParseIP(host)
	if ip != nil {
		if ip.IsLoopback() {
			return nil
		}
		return fmt.Errorf("refusing to listen on non-loopback address %s", host)
	}
	addrs, err := net.LookupIP(host)
	if err != nil {
		return fmt.Errorf("cannot resolve listen host %q: %w", host, err)
	}
	for _, a := range addrs {
		if !a.IsLoopback() {
			return fmt.Errorf("refusing to listen on non-loopback address %s (%s)", host, a)
		}
	}
	return nil
}

// Addr returns the address the server is listening on, nil before Start.
func (s *Server) Addr() net.Addr {
	if s.ln == nil {
		return nil
	}
	return s.ln.Addr()
}

// Start begins accepting connections.
func (s *Server) Start() error {
	ln, err := net.Listen("tcp", s.cfg.ListenAddr)
	if err != nil {
		return err
	}
	s.ln = ln
	s.wg.Add(1)
	go s.acceptLoop()
	return nil
}

func (s *Server) acceptLoop() {
	defer s.wg.Done()
	for {
		c, err := s.ln.Accept()
		if err != nil {
			return
		}
		// Only accept loopback peers, defence in depth on top of bind().
		tcp, ok := c.RemoteAddr().(*net.TCPAddr)
		if !ok || !tcp.IP.IsLoopback() {
			c.Write([]byte("421 non-loopback peers are not accepted\r\n"))
			c.Close()
			continue
		}
		s.trackConn(c, true)
		s.wg.Add(1)
		go func() {
			defer s.wg.Done()
			defer s.trackConn(c, false)
			s.handle(c)
		}()
	}
}

func (s *Server) trackConn(c net.Conn, add bool) {
	s.mu.Lock()
	if add {
		s.conns[c] = struct{}{}
	} else {
		delete(s.conns, c)
	}
	s.mu.Unlock()
}

// Close stops accepting and terminates active connections.
func (s *Server) Close() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	conns := make([]net.Conn, 0, len(s.conns))
	for c := range s.conns {
		conns = append(conns, c)
	}
	s.mu.Unlock()
	var firstErr error
	if s.ln != nil {
		firstErr = s.ln.Close()
	}
	for _, c := range conns {
		c.Close()
	}
	s.wg.Wait()
	return firstErr
}

type sessionState int

const (
	stGreet sessionState = iota // before EHLO/HELO
	stMail                      // envelope opened, before RCPT
	stRcpt                      // at least one RCPT accepted
)

func (s *Server) handle(c net.Conn) {
	defer c.Close()
	r := bufio.NewReaderSize(c, 64*1024)

	c.Write([]byte(fmt.Sprintf("220 %s SMTP test receiver ready\r\n", s.cfg.Hostname)))

	st := stGreet
	var from string
	var rcpts []string

	for {
		if s.cfg.CommandTimeout > 0 {
			c.SetReadDeadline(time.Now().Add(s.cfg.CommandTimeout))
		}
		line, crlf, err := readCommandLine(r, maxCommandLine)
		if err != nil {
			if errors.Is(err, io.EOF) && line == "" {
				return // client went away quietly
			}
			var ne net.Error
			if errors.As(err, &ne) && ne.Timeout() {
				c.Write([]byte("421 idle timeout, closing connection\r\n"))
			}
			if errors.Is(err, errLineTooLong) {
				c.Write([]byte("500 command line too long\r\n"))
				continue
			}
			return
		}
		if !crlf {
			// Command lines MUST be CRLF-framed; a bare LF is a protocol error.
			c.Write([]byte("501 expected CRLF line ending\r\n"))
			continue
		}

		cmd, arg := splitCommand(line)
		switch strings.ToUpper(cmd) {
		case "EHLO", "HELO":
			if s.ehlo(c, cmd, arg) {
				// A successful greeting opens a fresh (empty) envelope.
				st = stMail
				from = ""
				rcpts = nil
			}
		case "MAIL":
			if st == stGreet {
				c.Write([]byte("503 send EHLO/HELO first\r\n"))
				continue
			}
			addr, size, ok := parseMailArg(arg)
			if !ok {
				c.Write([]byte("501 syntax: MAIL FROM:<address> [SIZE=...]\r\n"))
				continue
			}
			if size >= 0 && size > int64(s.cfg.MaxMessageSize) {
				fmt.Fprintf(c, "552 message size %d exceeds limit %d\r\n", size, s.cfg.MaxMessageSize)
				continue
			}
			st = stMail
			from = addr
			rcpts = nil
			c.Write([]byte("250 sender ok\r\n"))
		case "RCPT":
			if st == stGreet {
				c.Write([]byte("503 send EHLO/HELO first\r\n"))
				continue
			}
			if from == "" {
				c.Write([]byte("503 send MAIL first\r\n"))
				continue
			}
			addr, ok := parseRcptArg(arg)
			if !ok {
				c.Write([]byte("501 syntax: RCPT TO:<address>\r\n"))
				continue
			}
			if containsFold(rcpts, addr) {
				c.Write([]byte("250 recipient ok (duplicate ignored)\r\n"))
				continue
			}
			if len(rcpts) >= s.cfg.MaxRecipients {
				c.Write([]byte("452 too many recipients\r\n"))
				continue
			}
			rcpts = append(rcpts, addr)
			st = stRcpt
			c.Write([]byte("250 recipient ok\r\n"))
		case "DATA":
			if st == stGreet {
				c.Write([]byte("503 send EHLO/HELO first\r\n"))
				continue
			}
			if from == "" {
				c.Write([]byte("503 send MAIL first\r\n"))
				continue
			}
			if len(rcpts) == 0 {
				c.Write([]byte("503 send RCPT first\r\n"))
				continue
			}
			c.Write([]byte("354 end data with <CRLF>.<CRLF>\r\n"))
			body, code, err := readData(c, r, s.cfg.MaxMessageSize)
			if err != nil {
				// Connection broke mid-DATA: drop everything, nothing stored.
				return
			}
			if code == 552 {
				// Oversize: transaction is aborted (RFC 5321 3.3).
				st = stMail
				from = ""
				rcpts = nil
				continue
			}
			if _, err := s.sink.Add(from, rcpts, body); err != nil {
				fmt.Fprintf(c, "451 failed to store message: %s\r\n", sanitizeText(err.Error()))
			} else {
				c.Write([]byte("250 message accepted\r\n"))
			}
			st = stMail
			from = ""
			rcpts = nil
		case "RSET":
			st = stMail
			from = ""
			rcpts = nil
			c.Write([]byte("250 reset ok\r\n"))
		case "NOOP":
			c.Write([]byte("250 ok\r\n"))
		case "VRFY", "EXPN":
			c.Write([]byte("502 command not implemented\r\n"))
		case "HELP":
			c.Write([]byte("250-supported commands: EHLO HELO MAIL RCPT DATA RSET NOOP QUIT\r\n"))
			c.Write([]byte("250 this is a receive-only test server; no mail is ever delivered\r\n"))
		case "QUIT":
			c.Write([]byte(fmt.Sprintf("221 %s closing connection\r\n", s.cfg.Hostname)))
			return
		case "":
			c.Write([]byte("500 empty command\r\n"))
		default:
			c.Write([]byte("500 command not recognized\r\n"))
		}
	}
}

// ehlo writes the greeting response and reports whether the command was
// accepted. A rejected greeting leaves the session state unchanged.
func (s *Server) ehlo(c net.Conn, verb, arg string) bool {
	if arg == "" {
		c.Write([]byte("501 missing client identity\r\n"))
		return false
	}
	if strings.EqualFold(verb, "HELO") {
		fmt.Fprintf(c, "250 %s greets %s\r\n", s.cfg.Hostname, arg)
		return true
	}
	var b strings.Builder
	fmt.Fprintf(&b, "250-%s greets %s\r\n", s.cfg.Hostname, arg)
	fmt.Fprintf(&b, "250-SIZE %d\r\n", s.cfg.MaxMessageSize)
	fmt.Fprintf(&b, "250-8BITMIME\r\n")
	fmt.Fprintf(&b, "250 HELP\r\n")
	c.Write([]byte(b.String()))
	return true
}

var errLineTooLong = errors.New("line too long")

// readCommandLine reads one CRLF-terminated command. It returns the line
// without its terminator and whether the terminator was exactly CRLF. A line
// terminated by bare LF is returned with crlf=false so the caller can reject
// it; the reader stays in sync because the terminator is fully consumed.
func readCommandLine(r *bufio.Reader, max int) (string, bool, error) {
	var raw []byte
	tooLong := false
	for {
		chunk, err := r.ReadSlice('\n')
		raw = append(raw, chunk...)
		if errors.Is(err, bufio.ErrBufferFull) {
			// Line spans several internal buffers; keep reading, and remember
			// if it already exceeded the limit without returning early so the
			// stream stays synchronised to exactly one response per line.
			if len(raw) > max {
				tooLong = true
			}
			continue
		}
		if err != nil {
			// Return whatever was read along with EOF so a final partial
			// command can be diagnosed; callers treat EOF-with-data as a drop.
			line := strings.TrimRight(string(raw), "\r\n")
			return line, false, err
		}
		if tooLong || len(raw) > max+2 {
			return "", false, errLineTooLong
		}
		crlf := len(raw) >= 2 && raw[len(raw)-2] == '\r'
		line := string(raw)
		if crlf {
			line = line[:len(line)-2]
		} else {
			line = line[:len(line)-1]
		}
		return line, crlf, nil
	}
}

// readData consumes the DATA body up to <CRLF>.<CRLF> (also accepting a bare
// LF terminator for robustness, like most receivers). It applies dot
// transparency, normalises every line to CRLF, enforces the size limit on
// the decoded body, and returns code 552 for an oversize message. Any other
// error means the connection broke and the (partial) message must be
// discarded.
func readData(c net.Conn, r *bufio.Reader, maxSize int) (string, int, error) {
	var b strings.Builder
	size := 0
	drainBudget := maxSize // bytes still tolerated while draining an oversize body
	oversize := false

	for {
		line, err := readDataLine(r)
		if err != nil {
			return "", 0, err // dropped connection etc.: nothing stored
		}
		if isDataTerminator(line) {
			if oversize {
				fmt.Fprintf(c, "552 message exceeds size limit of %d bytes\r\n", maxSize)
				return "", 552, nil
			}
			// Per RFC 5321 3.3 the CRLF before the terminating dot line
			// terminates the last content line and belongs to the mail data.
			return b.String(), 0, nil
		}

		if !oversize {
			decoded := undoDots(line)
			size += len(decoded) + 2 // content plus its CRLF framing
			if size > maxSize {
				oversize = true
			} else {
				b.Write(decoded)
				b.WriteString("\r\n")
			}
		} else {
			// Keep draining to the terminator so the protocol stays in sync,
			// but give up on an abusive stream.
			drainBudget -= len(line)
			if drainBudget < 0 {
				c.Write([]byte("421 aborting: message far over size limit\r\n"))
				return "", 0, io.ErrUnexpectedEOF
			}
		}
	}
}

// readDataLine reads a physical line from the DATA section and returns it
// without the trailing CRLF (or bare LF). A line longer than maxDataLine is
// discarded up to its terminator and reported with errLineTooLong, so the
// caller never sees a stream desynchronised mid-line.
func readDataLine(r *bufio.Reader) ([]byte, error) {
	var raw []byte
	tooLong := false
	for {
		chunk, err := r.ReadSlice('\n')
		raw = append(raw, chunk...)
		if errors.Is(err, bufio.ErrBufferFull) {
			if len(raw) > maxDataLine {
				tooLong = true
			}
			continue
		}
		if err != nil {
			if errors.Is(err, io.EOF) && len(raw) > 0 {
				return nil, io.ErrUnexpectedEOF
			}
			return nil, err
		}
		if tooLong {
			return nil, fmt.Errorf("%w: data line exceeds %d bytes", errLineTooLong, maxDataLine)
		}
		if len(raw) >= 2 && raw[len(raw)-2] == '\r' {
			return raw[:len(raw)-2], nil
		}
		return raw[:len(raw)-1], nil
	}
}

// isDataTerminator reports whether line (terminator stripped) is the
// end-of-data marker. A bare-LF-terminated "." is accepted for robustness,
// like most receivers do.
func isDataTerminator(line []byte) bool {
	return len(line) == 1 && line[0] == '.'
}

// undoDots applies dot transparency: one leading dot of a line that begins
// with two dots is removed.
func undoDots(line []byte) []byte {
	if len(line) >= 2 && line[0] == '.' && line[1] == '.' {
		return line[1:]
	}
	return line
}

// splitCommand separates a command verb from its argument.
func splitCommand(line string) (verb, arg string) {
	line = strings.TrimRight(line, " \t")
	i := strings.IndexByte(line, ' ')
	if i < 0 {
		return line, ""
	}
	return line[:i], strings.TrimLeft(line[i+1:], " \t")
}

// parseMailArg parses "FROM:<addr>" with optional ESMTP params (SIZE=...).
func parseMailArg(arg string) (addr string, size int64, ok bool) {
	const prefix = "FROM:"
	rest := strings.TrimSpace(arg)
	if len(rest) < len(prefix) || !strings.EqualFold(rest[:len(prefix)], prefix) {
		return "", -1, false
	}
	rest = strings.TrimLeft(rest[len(prefix):], " \t")
	size = -1

	if strings.HasPrefix(rest, "<") {
		end := strings.IndexByte(rest, '>')
		if end < 0 {
			return "", -1, false
		}
		addr = rest[1:end]
		rest = strings.TrimLeft(rest[end+1:], " \t")
	} else {
		fields := strings.Fields(rest)
		if len(fields) == 0 {
			return "", -1, false
		}
		addr = fields[0]
		rest = strings.TrimLeft(rest[len(fields[0]):], " \t")
	}

	for _, p := range strings.Fields(rest) {
		kv := strings.SplitN(p, "=", 2)
		if strings.EqualFold(kv[0], "SIZE") {
			if len(kv) != 2 {
				return "", -1, false
			}
			var v int64
			for _, ch := range kv[1] {
				if ch < '0' || ch > '9' {
					return "", -1, false
				}
				v = v*10 + int64(ch-'0')
			}
			size = v
		}
		// Unknown ESMTP parameters are accepted and ignored.
	}
	return addr, size, true
}

// parseRcptArg parses "TO:<addr>".
func parseRcptArg(arg string) (string, bool) {
	const prefix = "TO:"
	rest := strings.TrimSpace(arg)
	if len(rest) < len(prefix) || !strings.EqualFold(rest[:len(prefix)], prefix) {
		return "", false
	}
	rest = strings.TrimLeft(rest[len(prefix):], " \t")
	if strings.HasPrefix(rest, "<") {
		end := strings.IndexByte(rest, '>')
		if end <= 1 {
			return "", false
		}
		return rest[1:end], true
	}
	fields := strings.Fields(rest)
	if len(fields) == 0 || fields[0] == "" {
		return "", false
	}
	return fields[0], true
}

func containsFold(list []string, v string) bool {
	for _, x := range list {
		if strings.EqualFold(x, v) {
			return true
		}
	}
	return false
}

// sanitizeText keeps injected error text to a single protocol line.
func sanitizeText(s string) string {
	s = strings.ReplaceAll(s, "\r", " ")
	s = strings.ReplaceAll(s, "\n", " ")
	return s
}
