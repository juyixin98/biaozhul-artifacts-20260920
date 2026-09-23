package socks5

import (
	"bufio"
	"context"
	"crypto/subtle"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/netip"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Config configures a Server.
type Config struct {
	Username string
	Password string
	Allow    *AllowList
	// DialContext dials the target. Defaults to net.Dialer.DialContext with a
	// 10s timeout. Tests may override it to shrink socket buffers.
	DialContext func(ctx context.Context, network, address string) (net.Conn, error)
	// HandshakeTimeout bounds the whole negotiation (methods + auth + request).
	HandshakeTimeout time.Duration
	// DialTimeout bounds the connection to the target.
	DialTimeout time.Duration
	Logger      *slog.Logger
}

// Stats are counters exposed at the HTTP /debug/stats endpoint.
type Stats struct {
	Accepts        atomic.Int64
	Active         atomic.Int64
	AuthFailures   atomic.Int64
	Rejected       atomic.Int64
	ConnectOK      atomic.Int64
	DialFailures   atomic.Int64
	ClientToTarget atomic.Int64
	TargetToClient atomic.Int64
}

// Snapshot is a point-in-time copy of Stats.
type Snapshot struct {
	Accepts        int64 `json:"accepts"`
	ActiveSessions int64 `json:"active_sessions"`
	AuthFailures   int64 `json:"auth_failures"`
	Rejected       int64 `json:"rejected_by_allowlist"`
	ConnectOK      int64 `json:"connect_ok"`
	DialFailures   int64 `json:"dial_failures"`
	ClientToTarget int64 `json:"bytes_client_to_target"`
	TargetToClient int64 `json:"bytes_target_to_client"`
}

func (s *Stats) snapshot() Snapshot {
	return Snapshot{
		Accepts:        s.Accepts.Load(),
		ActiveSessions: s.Active.Load(),
		AuthFailures:   s.AuthFailures.Load(),
		Rejected:       s.Rejected.Load(),
		ConnectOK:      s.ConnectOK.Load(),
		DialFailures:   s.DialFailures.Load(),
		ClientToTarget: s.ClientToTarget.Load(),
		TargetToClient: s.TargetToClient.Load(),
	}
}

// Server is the SOCKS5 proxy.
type Server struct {
	cfg   Config
	stats Stats
}

// New validates the configuration and returns a ready server.
func New(cfg Config) (*Server, error) {
	if cfg.Username == "" || cfg.Password == "" {
		return nil, errors.New("username and password are required (no anonymous access)")
	}
	if cfg.Allow == nil {
		return nil, errors.New("destination allowlist is required")
	}
	if cfg.HandshakeTimeout == 0 {
		cfg.HandshakeTimeout = 10 * time.Second
	}
	if cfg.DialTimeout == 0 {
		cfg.DialTimeout = 10 * time.Second
	}
	if cfg.Logger == nil {
		cfg.Logger = slog.Default()
	}
	if cfg.DialContext == nil {
		d := &net.Dialer{Timeout: cfg.DialTimeout}
		cfg.DialContext = d.DialContext
	}
	return &Server{cfg: cfg}, nil
}

// Stats returns a snapshot of the proxy counters.
func (s *Server) Stats() Snapshot { return s.stats.snapshot() }

// Listen creates a TCP listener, refusing anything not bound to loopback.
func (s *Server) Listen(network, addr string) (net.Listener, error) {
	ln, err := net.Listen(network, addr)
	if err != nil {
		return nil, err
	}
	if err := s.assertLoopback(ln); err != nil {
		ln.Close()
		return nil, err
	}
	return ln, nil
}

// assertLoopback verifies the effective listener address resolves to a
// loopback IP. This rejects binds such as 0.0.0.0 even if the caller asked
// for a loopback-looking name.
func (s *Server) assertLoopback(ln net.Listener) error {
	ta, ok := ln.Addr().(*net.TCPAddr)
	if !ok {
		return fmt.Errorf("unsupported listener type %T", ln.Addr())
	}
	addr, ok := netip.AddrFromSlice(ta.IP)
	if !ok {
		return fmt.Errorf("invalid listener IP %s", ta.IP)
	}
	if !addr.Unmap().IsLoopback() {
		return fmt.Errorf("refusing to listen on non-loopback address %s", ta.IP)
	}
	return nil
}

// Serve accepts connections until the listener closes.
func (s *Server) Serve(ln net.Listener) error {
	for {
		conn, err := ln.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return nil
			}
			return err
		}
		s.stats.Accepts.Add(1)
		s.stats.Active.Add(1)
		go func() {
			defer s.stats.Active.Add(-1)
			s.handle(conn)
		}()
	}
}

func (s *Server) handle(conn net.Conn) {
	defer conn.Close()
	// Everything up to the completed CONNECT reply must finish within the
	// handshake budget; a client that sends one byte and goes quiet gets
	// closed and the file descriptor released.
	_ = conn.SetDeadline(time.Now().Add(s.cfg.HandshakeTimeout))

	br := bufio.NewReader(conn)

	if err := s.negotiate(conn, br); err != nil {
		s.cfg.Logger.Debug("handshake failed", "remote", conn.RemoteAddr(), "err", err)
		if errors.Is(err, errAuth) {
			s.stats.AuthFailures.Add(1)
		}
		return
	}

	cmd, dst, err := readRequest(br)
	if err != nil {
		s.cfg.Logger.Debug("bad request", "remote", conn.RemoteAddr(), "err", err)
		return
	}

	// Only CONNECT is supported; check address types first so the reply code
	// follows RFC 1928 §6 semantics.
	switch dst.aType {
	case ATypIPv4, ATypIPv6, ATypDomain:
	default:
		_, _ = conn.Write([]byte{Ver5, RepAddrNotSupported, 0x00, ATypIPv4, 0, 0, 0, 0, 0, 0})
		return
	}
	if cmd != CmdConnect {
		s.stats.Rejected.Add(1)
		replyRequestError(conn, dst.aType, RepCmdNotSupported)
		s.cfg.Logger.Info("unsupported command rejected", "remote", conn.RemoteAddr(), "cmd", cmd)
		return
	}

	checkedHost, err := s.cfg.Allow.CheckResolve(dst.host, net.DefaultResolver)
	if err != nil {
		s.stats.Rejected.Add(1)
		replyRequestError(conn, dst.aType, RepNotAllowed)
		s.cfg.Logger.Info("destination rejected by allowlist",
			"remote", conn.RemoteAddr(), "host", dst.host)
		return
	}

	target, err := s.cfg.DialContext(context.Background(), "tcp", net.JoinHostPort(checkedHost, dst.portString()))
	if err != nil {
		s.stats.DialFailures.Add(1)
		replyRequestError(conn, dst.aType, dialReplyCode(err))
		s.cfg.Logger.Debug("dial failed", "target", dst.host, "err", err)
		return
	}
	defer target.Close()

	if err := replySuccess(conn, target); err != nil {
		return
	}
	s.stats.ConnectOK.Add(1)

	// Handshake is done: clear deadlines so the tunnel can run indefinitely.
	_ = conn.SetDeadline(time.Time{})

	s.bridge(conn, br, target)
}

// negotiate runs method selection and username/password authentication.
func (s *Server) negotiate(conn net.Conn, br *bufio.Reader) error {
	methods, err := readMethods(br)
	if err != nil {
		return err
	}
	offered := false
	for _, m := range methods {
		if m == MethodUserPass {
			offered = true
			break
		}
	}
	if !offered {
		_, _ = conn.Write([]byte{Ver5, MethodNone})
		return errors.New("client did not offer username/password auth")
	}
	if _, err := conn.Write([]byte{Ver5, MethodUserPass}); err != nil {
		return err
	}

	user, pass, err := readAuth(br)
	if err != nil {
		return err
	}
	userOK := subtle.ConstantTimeCompare([]byte(user), []byte(s.cfg.Username)) == 1
	passOK := subtle.ConstantTimeCompare([]byte(pass), []byte(s.cfg.Password)) == 1
	if !(userOK && passOK) {
		_, _ = conn.Write([]byte{UserPassVer, AuthFail})
		return errAuth
	}
	_, err = conn.Write([]byte{UserPassVer, AuthSuccess})
	return err
}

// bridge pumps bytes in both directions. Each direction runs in its own
// goroutine; when one side signals EOF it half-closes the other side
// (CloseWrite), so a peer that only shuts its write half still gets the data
// draining out of the other direction (TCP bidirectional half-close).
//
// Byte counters are updated incrementally (on every successful Write), which
// is what makes backpressure observable from the HTTP /stats endpoint: when a
// client stops reading the target->client counter plateaus at the combined
// socket-buffer capacity instead of jumping once at teardown.
func (s *Server) bridge(client net.Conn, br *bufio.Reader, target net.Conn) {
	var wg sync.WaitGroup
	wg.Add(2)

	go func() {
		defer wg.Done()
		_, _ = io.Copy(&countingWriter{w: target, n: &s.stats.ClientToTarget}, br)
		if tw, ok := target.(closeWriter); ok {
			_ = tw.CloseWrite()
		}
	}()

	go func() {
		defer wg.Done()
		_, _ = io.Copy(&countingWriter{w: client, n: &s.stats.TargetToClient}, target)
		if cw, ok := client.(closeWriter); ok {
			_ = cw.CloseWrite()
		}
	}()

	// When both pumps finish, both halves have observed EOF; the deferred
	// closes in handle() then release both file descriptors.
	wg.Wait()
}

type closeWriter interface {
	CloseWrite() error
}

// countingWriter reports bytes as they are written to the peer; because the
// counter moves with successful Writes, a blocked write shows up as a
// plateau under backpressure.
type countingWriter struct {
	w io.Writer
	n *atomic.Int64
}

func (c *countingWriter) Write(p []byte) (int, error) {
	n, err := c.w.Write(p)
	if n > 0 {
		c.n.Add(int64(n))
	}
	return n, err
}

// replySuccess sends VER,REP=0,RSV plus the BND.ADDR/BND.PORT observed on the
// outbound connection.
func replySuccess(conn net.Conn, target net.Conn) error {
	addr := target.LocalAddr().(*net.TCPAddr)
	ip := addr.IP.To4()
	var rep []byte
	if ip != nil {
		rep = make([]byte, 0, 10)
		rep = append(rep, Ver5, RepSuccess, 0x00, ATypIPv4)
		rep = append(rep, ip...)
	} else {
		rep = make([]byte, 0, 22)
		rep = append(rep, Ver5, RepSuccess, 0x00, ATypIPv6)
		rep = append(rep, addr.IP.To16()...)
	}
	port := make([]byte, 2)
	binary.BigEndian.PutUint16(port, uint16(addr.Port))
	rep = append(rep, port...)
	_, err := conn.Write(rep)
	return err
}

// replyRequestError sends an error reply, reusing the request's address type
// for the placeholder BND.ADDR.
func replyRequestError(conn net.Conn, aType, code byte) {
	var rep []byte
	switch aType {
	case ATypIPv6:
		rep = []byte{Ver5, code, 0x00, ATypIPv6,
			0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}
	case ATypDomain:
		rep = []byte{Ver5, code, 0x00, ATypDomain, 1, 0, 0, 0}
	default:
		rep = []byte{Ver5, code, 0x00, ATypIPv4, 0, 0, 0, 0, 0, 0}
	}
	_, _ = conn.Write(rep)
}

// dialReplyCode maps dial errors to SOCKS5 reply codes (RFC 1928 §6).
func dialReplyCode(err error) byte {
	var se *net.OpError
	if errors.As(err, &se) {
		if se.Timeout() {
			return RepTTLExpired
		}
		if errors.Is(se.Err, errNotAllowed) {
			return RepNotAllowed
		}
		msg := se.Err.Error()
		// The standard library reports refused/reset/unreachable as strings;
		// map the common Linux cases explicitly.
		switch {
		case strings.Contains(msg, "connection refused"):
			return RepConnRefused
		case strings.Contains(msg, "network is unreachable"):
			return RepNetUnreachable
		case strings.Contains(msg, "no route to host") || strings.Contains(msg, "host is down"):
			return RepHostUnreachable
		}
	}
	return RepGeneralFailure
}
