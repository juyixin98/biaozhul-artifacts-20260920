// Package smtpd implements a small, loopback-only SMTP receiving server.
//
// It supports the subset of RFC 5321 needed to receive test mail:
//
//	EHLO/HELO, MAIL FROM, RCPT TO, DATA, RSET, NOOP, QUIT
//
// Commands are rejected with 503 when they arrive out of order, DATA framing
// is strictly CRLF-based with dot-transparency (RFC 5321 §4.5.2), and a
// message is handed to the MailSink only after a properly terminated DATA
// phase — a disconnect, oversized payload or timeout mid-DATA stores nothing.
package smtpd

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/netip"
	"strings"
	"sync"
	"time"
)

// MailSink receives fully accepted messages. Implementations must be safe for
// concurrent use.
type MailSink interface {
	Save(from string, to []string, raw []byte, receivedAt time.Time) (id string, err error)
}

// Config configures a Server.
type Config struct {
	// Addr is the listen address, e.g. "127.0.0.1:2525". Its host must be a
	// loopback IP address; hostnames and non-loopback addresses are rejected.
	Addr string
	// Hostname is advertised in the greeting and EHLO banner.
	Hostname string
	// MaxMessageBytes caps the accepted DATA payload (after dot
	// un-escaping). 0 means default (1 MiB).
	MaxMessageBytes int
	// IdleTimeout caps each read of command or data line. 0 means default.
	IdleTimeout time.Duration
	// Sink receives complete messages. Required.
	Sink MailSink
	// Logger, if nil, uses slog.Default().
	Logger *slog.Logger
}

const (
	defaultMaxMessageBytes = 1 << 20 // 1 MiB
	defaultIdleTimeout     = 2 * time.Minute
	// maxCommandLine bounds a single command line so a talkative client
	// cannot grow server memory without limit.
	maxCommandLine = 4096
	// maxDataLine bounds a single DATA content line (RFC 5321 recommends
	// ~1000, but real test mail can be wider); the total message cap still
	// applies on top.
	maxDataLine = 1 << 20
	// graceClose is how long we hold a connection open after sending a 5xx
	// mid-DATA, so the error reply flushes before the FIN/RST.
	graceClose = 50 * time.Millisecond
	// hardDataCap is an absolute ceiling; a configured MaxMessageBytes above
	// it is clamped down.
	hardDataCap = 64 << 20
)

// Server is a loopback-only SMTP receiver.
type Server struct {
	cfg     Config
	log     *slog.Logger
	ln      net.Listener
	wg      sync.WaitGroup
	connsMu sync.Mutex
	conns   map[net.Conn]struct{}
	closed  chan struct{}

	closeOnce sync.Once
}

// New validates the config and constructs a server.
func New(cfg Config) (*Server, error) {
	if cfg.Sink == nil {
		return nil, errors.New("smtpd: Sink is required")
	}
	if err := ValidateLoopbackAddr(cfg.Addr); err != nil {
		return nil, err
	}
	if cfg.Hostname == "" {
		cfg.Hostname = "loopmail"
	}
	if cfg.MaxMessageBytes <= 0 {
		cfg.MaxMessageBytes = defaultMaxMessageBytes
	}
	if cfg.MaxMessageBytes > hardDataCap {
		cfg.MaxMessageBytes = hardDataCap
	}
	if cfg.IdleTimeout <= 0 {
		cfg.IdleTimeout = defaultIdleTimeout
	}
	log := cfg.Logger
	if log == nil {
		log = slog.Default()
	}
	return &Server{
		cfg:    cfg,
		log:    log,
		conns:  map[net.Conn]struct{}{},
		closed: make(chan struct{}),
	}, nil
}

// ValidateLoopbackAddr ensures addr's host parses as an IP and is loopback.
// "localhost" is intentionally rejected: it can resolve to a non-loopback
// address on misconfigured systems, and the server's safety claim is that it
// is reachable only from the local machine.
func ValidateLoopbackAddr(addr string) error {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return fmt.Errorf("smtpd: address %q must be host:port: %w", addr, err)
	}
	ip, err := netip.ParseAddr(host)
	if err != nil {
		return fmt.Errorf("smtpd: address %q must use a literal loopback IP, got host %q", addr, host)
	}
	if !ip.IsLoopback() {
		return fmt.Errorf("smtpd: refusing to listen on non-loopback address %q (test receiver only)", addr)
	}
	return nil
}

// Listen creates the listening socket. It must be called exactly once before
// Serve. Splitting it out lets callers learn the bound address (e.g. with
// port 0) before accepting connections.
func (s *Server) Listen() error {
	ln, err := net.Listen("tcp", s.cfg.Addr)
	if err != nil {
		return err
	}
	s.ln = ln
	return nil
}

// Serve accepts connections until Shutdown or a hard listener error.
func (s *Server) Serve() error {
	if s.ln == nil {
		return errors.New("smtpd: Serve called before Listen")
	}
	return s.serve(s.ln)
}

// ListenAndServe creates the listener and serves until Shutdown.
func (s *Server) ListenAndServe() error {
	if err := s.Listen(); err != nil {
		return err
	}
	return s.Serve()
}

func (s *Server) serve(ln net.Listener) error {
	for {
		conn, err := ln.Accept()
		if err != nil {
			select {
			case <-s.closed:
				return ErrServerClosed
			default:
			}
			if errors.Is(err, net.ErrClosed) {
				return ErrServerClosed
			}
			return err
		}
		// Defense in depth: even though we only bind a loopback address,
		// refuse any peer whose address is not loopback.
		tcpAddr, ok := conn.RemoteAddr().(*net.TCPAddr)
		if !ok || !tcpAddr.IP.IsLoopback() {
			s.log.Warn("rejecting non-loopback peer", "peer", conn.RemoteAddr())
			conn.Close()
			continue
		}
		s.connsMu.Lock()
		s.conns[conn] = struct{}{}
		s.connsMu.Unlock()
		s.wg.Add(1)
		go func() {
			defer func() {
				s.connsMu.Lock()
				delete(s.conns, conn)
				s.connsMu.Unlock()
				s.wg.Done()
				conn.Close()
			}()
			s.handle(conn)
		}()
	}
}

// Addr returns the actual bound address, useful when listening on :0 in tests.
func (s *Server) Addr() net.Addr {
	if s.ln == nil {
		return nil
	}
	return s.ln.Addr()
}

// Shutdown stops accepting and closes all active connections, waiting for them
// to finish.
func (s *Server) Shutdown(ctx context.Context) error {
	s.closeOnce.Do(func() { close(s.closed) })
	if s.ln != nil {
		s.ln.Close()
	}
	s.connsMu.Lock()
	for c := range s.conns {
		c.Close()
	}
	s.connsMu.Unlock()

	done := make(chan struct{})
	go func() { s.wg.Wait(); close(done) }()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// ErrServerClosed is returned by ListenAndServe after Shutdown.
var ErrServerClosed = errors.New("smtpd: server closed")

// ---------------------------------------------------------------------------
// Per-session state machine
// ---------------------------------------------------------------------------

type sessionState int

const (
	stateGreet sessionState = iota // before EHLO/HELO
	stateMail                      // envelope opened, waiting RCPT
	stateRcpt                      // at least one RCPT accepted
	stateData                      // inside DATA content phase
)

type session struct {
	srv    *Server
	conn   net.Conn
	reader *bufio.Reader
	writer *bufio.Writer

	state    sessionState
	hello    bool // an EHLO/HELO has been accepted on this connection
	mailFrom string
	rcptTo   []string
}

func (s *Server) handle(conn net.Conn) {
	sess := &session{
		srv:    s,
		conn:   conn,
		reader: bufio.NewReaderSize(conn, 64*1024),
		writer: bufio.NewWriter(conn),
		state:  stateGreet,
	}
	sess.reply("220 %s ESMTP loopmail (test receiver; no outbound delivery)", s.cfg.Hostname)

	for {
		line, err := sess.readLine()
		if err != nil {
			if errors.Is(err, errLineTooLong) {
				sess.reply("500 5.5.2 Command line too long")
				continue
			}
			if !isBenignClose(err) {
				s.log.Debug("connection read error", "peer", conn.RemoteAddr(), "err", err)
			}
			return
		}
		verb, args := splitCommand(line)
		if verb == "" {
			sess.reply("500 5.5.2 Command unrecognized")
			continue
		}
		sess.dispatch(verb, args)
		if verb == "QUIT" {
			return
		}
	}
}

func (sess *session) dispatch(verb, args string) {
	switch verb {
	case "EHLO", "HELO":
		sess.cmdHello(verb, args)
	case "MAIL":
		sess.cmdMail(args)
	case "RCPT":
		sess.cmdRcpt(args)
	case "DATA":
		sess.cmdData(args)
	case "RSET":
		sess.cmdRset()
	case "NOOP":
		sess.reply("250 OK")
	case "VRFY":
		// Test receiver: do not reveal anything, but stay polite.
		sess.reply("252 Cannot VRFY user, but will accept message and attempt delivery")
	case "QUIT":
		sess.reply("221 2.0.0 Bye")
	default:
		sess.reply("502 5.5.1 Command not implemented")
	}
}

func (sess *session) cmdHello(verb, args string) {
	args = strings.TrimSpace(args)
	if args == "" {
		sess.reply("501 5.5.2 %s requires a domain/address argument", verb)
		return
	}
	// EHLO/HELO aborts any in-progress transaction and marks the session
	// greeted.
	sess.resetTransaction()
	sess.hello = true
	if verb == "EHLO" {
		sess.replyMultiline(
			[]string{
				fmt.Sprintf("%s at your service", sess.srv.cfg.Hostname),
				"8BITMIME",
				fmt.Sprintf("SIZE %d", sess.srv.cfg.MaxMessageBytes),
			},
		)
		return
	}
	sess.reply("250 %s at your service", sess.srv.cfg.Hostname)
}

func (sess *session) cmdMail(args string) {
	switch sess.state {
	case stateData:
		sess.reply("503 5.5.1 Bad sequence of commands (DATA in progress)")
		return
	}
	if !sess.hello {
		sess.reply("503 5.5.1 Send EHLO/HELO first")
		return
	}
	addr, ok := parseReversePath(args)
	if !ok {
		sess.reply("501 5.5.4 Syntax: MAIL FROM:<address>")
		return
	}
	sess.resetTransaction()
	sess.mailFrom = addr
	sess.state = stateMail
	sess.reply("250 2.1.0 OK")
}

func (sess *session) cmdRcpt(args string) {
	switch sess.state {
	case stateData:
		sess.reply("503 5.5.1 Bad sequence of commands (DATA in progress)")
		return
	case stateGreet:
		sess.reply("503 5.5.1 Need MAIL before RCPT")
		return
	}
	addr, ok := parseForwardPath(args)
	if !ok {
		sess.reply("501 5.5.4 Syntax: RCPT TO:<address>")
		return
	}
	if addr == "" {
		sess.reply("501 5.5.4 RCPT TO requires a non-empty address")
		return
	}
	for _, existing := range sess.rcptTo {
		if strings.EqualFold(existing, addr) {
			sess.reply("250 2.1.5 OK (recipient already listed)")
			return
		}
	}
	sess.rcptTo = append(sess.rcptTo, addr)
	sess.state = stateRcpt
	sess.reply("250 2.1.5 OK")
}

func (sess *session) cmdData(args string) {
	if strings.TrimSpace(args) != "" {
		sess.reply("501 5.5.4 DATA takes no arguments")
		return
	}
	switch sess.state {
	case stateData:
		sess.reply("503 5.5.1 DATA already in progress")
	case stateGreet, stateMail:
		sess.reply("503 5.5.1 Need RCPT before DATA")
	default:
		sess.state = stateData
		sess.reply("354 Start mail input; end with <CRLF>.<CRLF>")
		raw, err := sess.readData()
		if err != nil {
			// Mid-DATA failure (disconnect, timeout, oversize): nothing is
			// stored and no "250 queued" is ever sent.
			sess.srv.log.Info("DATA aborted, message not stored",
				"from", sess.mailFrom, "to", sess.rcptTo, "err", err)
			sentError := false
			switch {
			case errors.Is(err, errTooLarge):
				sess.reply("552 5.3.4 Message size exceeds fixed limit")
				sentError = true
			case errors.Is(err, errLineTooLong):
				sess.reply("500 5.5.2 Line too long")
				sentError = true
			}
			if sentError {
				// The client may still be streaming the rejected body; pause
				// briefly so the buffered error reply flushes before the
				// (voided) connection is closed.
				time.Sleep(graceClose)
			}
			sess.conn.Close()
			return
		}
		id, saveErr := sess.srv.cfg.Sink.Save(sess.mailFrom, sess.rcptTo, raw, time.Now())
		if saveErr != nil {
			sess.srv.log.Error("store save failed", "err", saveErr)
			sess.reply("451 4.3.0 Local error processing message")
			sess.resetTransaction()
			return
		}
		sess.reply("250 2.0.0 OK stored as %s", id)
		sess.resetTransaction()
	}
}

func (sess *session) cmdRset() {
	// RSET clears the current mail transaction but keeps the session
	// greeting state (RFC 5321 §3.3): a new MAIL may follow without
	// another EHLO.
	sess.resetTransaction()
	sess.reply("250 2.0.0 OK")
}

// resetTransaction clears the in-progress envelope (sender, recipients,
// DATA state). It deliberately leaves the greeting flag intact: RFC 5321
// allows a new MAIL after RSET (or a repeated EHLO) without re-greeting.
func (sess *session) resetTransaction() {
	sess.mailFrom = ""
	sess.rcptTo = nil
	sess.state = stateGreet
}

// readData consumes DATA content until the <CRLF>.<CRLF> end marker.
//
// Framing rules (RFC 5321 §4.5.2):
//   - the content is a sequence of CRLF-terminated lines;
//   - a line containing exactly "." ends the content;
//   - a line starting with ".." (i.e. dot-stuffed) has one leading dot
//     removed before it is stored;
//   - stored lines retain their canonical CRLF terminators.
func (sess *session) readData() ([]byte, error) {
	var buf []byte
	size := 0
	for {
		line, err := sess.readLineBounded(maxDataLine)
		if err != nil {
			return nil, err
		}
		// End-of-data marker: a line whose content is exactly one dot.
		// (The marker must follow a CRLF, which readLine strips, so every
		// line it returns is a candidate — including the first.)
		if string(line) == "." {
			return buf, nil
		}
		// RFC 5321 dot transparency: delete one leading dot from content lines.
		if len(line) > 0 && line[0] == '.' {
			line = line[1:]
		}
		size += len(line) + 2
		if size > sess.srv.cfg.MaxMessageBytes {
			return nil, errTooLarge
		}
		buf = append(buf, line...)
		buf = append(buf, '\r', '\n')
	}
}

// readLineBounded is readLine with a caller-supplied per-line byte cap.
func (sess *session) readLineBounded(limit int) ([]byte, error) {
	if sess.srv.cfg.IdleTimeout > 0 {
		_ = sess.conn.SetReadDeadline(time.Now().Add(sess.srv.cfg.IdleTimeout))
	}
	var line []byte
	tooLong := false
	for {
		chunk, err := sess.reader.ReadSlice('\n')
		line = append(line, chunk...)
		if err == nil {
			break
		}
		if errors.Is(err, bufio.ErrBufferFull) {
			if len(line) > limit {
				tooLong = true
			}
			if len(line) > hardDataCap { // absolute cap while draining
				return nil, errLineTooLong
			}
			continue
		}
		return nil, err
	}
	line = bytes.TrimSuffix(line, []byte("\n"))
	line = bytes.TrimSuffix(line, []byte("\r"))
	if tooLong || len(line) > limit {
		return nil, errLineTooLong
	}
	return line, nil
}

// readLine reads one command line, stripping its trailing CRLF (tolerating a
// bare LF). Lines longer than maxCommandLine are drained and reported as
// errLineTooLong so a talkative client cannot grow memory without bound.
func (sess *session) readLine() ([]byte, error) {
	return sess.readLineBounded(maxCommandLine)
}

func (sess *session) reply(format string, args ...any) {
	msg := fmt.Sprintf(format, args...)
	if _, err := fmt.Fprintf(sess.writer, "%s\r\n", msg); err != nil {
		return
	}
	_ = sess.writer.Flush()
}

// replyMultiline sends an RFC-style multiline 250 response: every line but
// the last gets the "250-" continuation prefix, the last gets "250 ".
func (sess *session) replyMultiline(lines []string) {
	for i, m := range lines {
		prefix := "250-"
		if i == len(lines)-1 {
			prefix = "250 "
		}
		if _, err := fmt.Fprintf(sess.writer, "%s%s\r\n", prefix, m); err != nil {
			return
		}
	}
	_ = sess.writer.Flush()
}

// ---------------------------------------------------------------------------
// command parsing helpers
// ---------------------------------------------------------------------------

func splitCommand(line []byte) (verb, args string) {
	s := string(line)
	if i := strings.IndexByte(s, ' '); i >= 0 {
		return strings.ToUpper(strings.TrimSpace(s[:i])), strings.TrimSpace(s[i+1:])
	}
	// Tolerate tabs as the command/argument separator too.
	if i := strings.IndexByte(s, '\t'); i >= 0 {
		return strings.ToUpper(strings.TrimSpace(s[:i])), strings.TrimSpace(s[i+1:])
	}
	return strings.ToUpper(strings.TrimSpace(s)), ""
}

// parseReversePath accepts "FROM:<addr>" plus optional ESMTP parameters
// (e.g. BODY=8BITMIME, SIZE=1234), which are ignored.
func parseReversePath(args string) (string, bool) {
	if !strings.HasPrefix(strings.ToUpper(args), "FROM:") {
		return "", false
	}
	rest := strings.TrimSpace(args[len("FROM:"):])
	return extractAddress(rest)
}

// parseForwardPath accepts "TO:<addr>" plus optional ESMTP parameters.
func parseForwardPath(args string) (string, bool) {
	if !strings.HasPrefix(strings.ToUpper(args), "TO:") {
		return "", false
	}
	rest := strings.TrimSpace(args[len("TO:"):])
	return extractAddress(rest)
}

// extractAddress pulls <...> from the front of a MAIL/RCPT argument and
// ignores any trailing ESMTP parameters.
func extractAddress(s string) (string, bool) {
	open := strings.IndexByte(s, '<')
	if open < 0 {
		// Be liberal with address-only forms without angle brackets.
		field := s
		if i := strings.IndexAny(field, " \t"); i >= 0 {
			field = field[:i]
		}
		return field, field != ""
	}
	closeIdx := strings.IndexByte(s[open:], '>')
	if closeIdx < 0 {
		return "", false
	}
	return s[open+1 : open+closeIdx], true
}

// isBenignClose reports whether the error just means the client went away.
func isBenignClose(err error) bool {
	if errors.Is(err, io.EOF) || errors.Is(err, net.ErrClosed) {
		return true
	}
	return false
}

var (
	errTooLarge    = errors.New("message exceeds size limit")
	errLineTooLong = errors.New("command line too long")
)
