// Package socks5 implements a minimal SOCKS5 CONNECT proxy server
// (RFC 1928 + RFC 1929 username/password authentication) restricted to
// loopback listeners and a local test whitelist of targets.
package socks5

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// Protocol constants (RFC 1928 / RFC 1929).
const (
	version5 = 0x05

	cmdConnect = 0x01

	atypIPv4   = 0x01
	atypDomain = 0x03
	atypIPv6   = 0x04

	methodNoAuth       = 0x00
	methodUserPass     = 0x02
	methodNoAcceptable = 0xFF

	authVersionUserPass = 0x01
	authStatusSuccess   = 0x00
	authStatusFailure   = 0x01

	repSucceeded          = 0x00
	repGeneralFailure     = 0x01
	repNotAllowed         = 0x02
	repNetworkUnreachable = 0x03
	repHostUnreachable    = 0x04
	repConnRefused        = 0x05
	repCmdNotSupported    = 0x07
	repAtypNotSupported   = 0x08
)

// Config controls a Server.
type Config struct {
	// Username and Password are the only accepted credentials.
	Username string
	Password string

	// HandshakeTimeout bounds the whole greeting+auth+request phase,
	// protecting against clients that stall mid-packet.
	HandshakeTimeout time.Duration

	// DialTimeout bounds connecting to the requested target.
	DialTimeout time.Duration

	// AllowPorts, when non-empty, restricts target ports. Nil/empty means
	// any port is allowed (hosts are still whitelist-checked).
	AllowPorts map[uint16]bool

	// ExtraHosts are additional allowed target hostnames (exact match,
	// case-insensitive). Loopback IPs and "localhost" are always allowed.
	ExtraHosts []string

	Logger *log.Logger
}

// Stats is a point-in-time snapshot of server counters.
type Stats struct {
	ActiveConns    int64 `json:"active_connections"`
	TotalConns     int64 `json:"total_connections"`
	HandshakeFails int64 `json:"handshake_failures"`
	AuthFails      int64 `json:"auth_failures"`
	DialFails      int64 `json:"dial_failures"`
	Rejected       int64 `json:"rejected_targets"`
	Relayed        int64 `json:"relayed_connections"`
}

// Server is a SOCKS5 CONNECT proxy.
type Server struct {
	cfg Config

	active    atomic.Int64
	total     atomic.Int64
	hsFails   atomic.Int64
	authFails atomic.Int64
	dialFails atomic.Int64
	rejected  atomic.Int64
	relayed   atomic.Int64
}

// NewServer returns a Server with defaults filled in.
func NewServer(cfg Config) *Server {
	if cfg.HandshakeTimeout <= 0 {
		cfg.HandshakeTimeout = 10 * time.Second
	}
	if cfg.DialTimeout <= 0 {
		cfg.DialTimeout = 5 * time.Second
	}
	if cfg.Logger == nil {
		cfg.Logger = log.Default()
	}
	return &Server{cfg: cfg}
}

// Stats returns a snapshot of the server counters.
func (s *Server) Stats() Stats {
	return Stats{
		ActiveConns:    s.active.Load(),
		TotalConns:     s.total.Load(),
		HandshakeFails: s.hsFails.Load(),
		AuthFails:      s.authFails.Load(),
		DialFails:      s.dialFails.Load(),
		Rejected:       s.rejected.Load(),
		Relayed:        s.relayed.Load(),
	}
}

// Serve accepts connections on ln until it is closed. ln must be bound to a
// loopback address; Serve returns an error otherwise.
func (s *Server) Serve(ln net.Listener) error {
	if !isLoopbackAddr(ln.Addr()) {
		return fmt.Errorf("socks5: refusing to listen on non-loopback address %s", ln.Addr())
	}
	s.logf("listening on %s", ln.Addr())
	for {
		conn, err := ln.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return nil
			}
			return err
		}
		s.total.Add(1)
		s.active.Add(1)
		go func() {
			defer s.active.Add(-1)
			s.handleConn(conn)
		}()
	}
}

func isLoopbackAddr(addr net.Addr) bool {
	host, _, err := net.SplitHostPort(addr.String())
	if err != nil {
		return false
	}
	if ip := net.ParseIP(host); ip != nil {
		return ip.IsLoopback()
	}
	return strings.EqualFold(host, "localhost")
}

func (s *Server) logf(format string, args ...any) {
	s.cfg.Logger.Printf("socks5: "+format, args...)
}

func (s *Server) handleConn(conn net.Conn) {
	defer conn.Close()
	remote := conn.RemoteAddr().String()

	// Bound the entire handshake so a client that stalls mid-packet
	// (半包) cannot hold the connection forever.
	if err := conn.SetReadDeadline(time.Now().Add(s.cfg.HandshakeTimeout)); err != nil {
		return
	}

	if err := s.negotiate(conn); err != nil {
		s.hsFails.Add(1)
		s.logf("%s: method negotiation: %v", remote, err)
		return
	}
	if err := s.authenticate(conn); err != nil {
		s.authFails.Add(1)
		s.logf("%s: auth: %v", remote, err)
		return
	}
	host, port, err := s.readRequest(conn)
	if err != nil {
		s.hsFails.Add(1)
		s.logf("%s: request: %v", remote, err)
		return
	}

	if !s.allowed(host, port) {
		s.rejected.Add(1)
		s.logf("%s: target %s:%d not in whitelist", remote, host, port)
		s.sendReply(conn, repNotAllowed, nil)
		return
	}

	target, err := s.dial(host, port)
	if err != nil {
		s.dialFails.Add(1)
		s.logf("%s: dial %s:%d: %v", remote, host, port, err)
		s.sendReply(conn, replyCodeFor(err), nil)
		return
	}
	defer target.Close()

	if err := s.sendReply(conn, repSucceeded, target.LocalAddr()); err != nil {
		s.hsFails.Add(1)
		return
	}
	s.relayed.Add(1)
	s.logf("%s: connected to %s:%d", remote, host, port)

	// Handshake done: clear the deadline and relay until both sides close.
	conn.SetReadDeadline(time.Time{})
	relay(conn, target)
}

// negotiate reads the client greeting and selects username/password auth.
// It tolerates fragmented ("half") packets via io.ReadFull.
func (s *Server) negotiate(conn net.Conn) error {
	var head [2]byte
	if _, err := io.ReadFull(conn, head[:]); err != nil {
		return fmt.Errorf("read greeting: %w", err)
	}
	if head[0] != version5 {
		return fmt.Errorf("bad version %d", head[0])
	}
	nmethods := int(head[1])
	if nmethods == 0 {
		return errors.New("no methods offered")
	}
	methods := make([]byte, nmethods)
	if _, err := io.ReadFull(conn, methods); err != nil {
		return fmt.Errorf("read methods: %w", err)
	}
	for _, m := range methods {
		if m == methodUserPass {
			_, err := conn.Write([]byte{version5, methodUserPass})
			return err
		}
	}
	conn.Write([]byte{version5, methodNoAcceptable})
	return errors.New("client does not offer username/password auth")
}

// authenticate performs RFC 1929 username/password verification.
func (s *Server) authenticate(conn net.Conn) error {
	var head [2]byte
	if _, err := io.ReadFull(conn, head[:]); err != nil {
		return fmt.Errorf("read auth header: %w", err)
	}
	if head[0] != authVersionUserPass {
		return fmt.Errorf("bad auth version %d", head[0])
	}
	ulen := int(head[1])
	if ulen == 0 {
		return errors.New("empty username")
	}
	ubuf := make([]byte, ulen)
	if _, err := io.ReadFull(conn, ubuf); err != nil {
		return fmt.Errorf("read username: %w", err)
	}
	var plen [1]byte
	if _, err := io.ReadFull(conn, plen[:]); err != nil {
		return fmt.Errorf("read password length: %w", err)
	}
	pbuf := make([]byte, int(plen[0]))
	if _, err := io.ReadFull(conn, pbuf); err != nil {
		return fmt.Errorf("read password: %w", err)
	}
	if string(ubuf) == s.cfg.Username && string(pbuf) == s.cfg.Password {
		_, err := conn.Write([]byte{authVersionUserPass, authStatusSuccess})
		return err
	}
	conn.Write([]byte{authVersionUserPass, authStatusFailure})
	return errors.New("invalid credentials")
}

// readRequest parses the CONNECT request and returns the target host/port.
// Unsupported commands and address types are answered with the proper
// reply code before returning an error.
func (s *Server) readRequest(conn net.Conn) (string, uint16, error) {
	var head [4]byte
	if _, err := io.ReadFull(conn, head[:]); err != nil {
		return "", 0, fmt.Errorf("read request header: %w", err)
	}
	if head[0] != version5 {
		return "", 0, fmt.Errorf("bad version %d", head[0])
	}
	if head[1] != cmdConnect {
		s.sendReply(conn, repCmdNotSupported, nil)
		return "", 0, fmt.Errorf("unsupported command %d", head[1])
	}

	var host string
	switch head[3] {
	case atypIPv4:
		var b [4]byte
		if _, err := io.ReadFull(conn, b[:]); err != nil {
			return "", 0, fmt.Errorf("read ipv4: %w", err)
		}
		host = net.IP(b[:]).String()
	case atypIPv6:
		var b [16]byte
		if _, err := io.ReadFull(conn, b[:]); err != nil {
			return "", 0, fmt.Errorf("read ipv6: %w", err)
		}
		host = net.IP(b[:]).String()
	case atypDomain:
		var l [1]byte
		if _, err := io.ReadFull(conn, l[:]); err != nil {
			return "", 0, fmt.Errorf("read domain length: %w", err)
		}
		if l[0] == 0 {
			return "", 0, errors.New("empty domain")
		}
		d := make([]byte, int(l[0]))
		if _, err := io.ReadFull(conn, d); err != nil {
			return "", 0, fmt.Errorf("read domain: %w", err)
		}
		host = string(d)
	default:
		s.sendReply(conn, repAtypNotSupported, nil)
		return "", 0, fmt.Errorf("unsupported address type %d", head[3])
	}

	var pbuf [2]byte
	if _, err := io.ReadFull(conn, pbuf[:]); err != nil {
		return "", 0, fmt.Errorf("read port: %w", err)
	}
	return host, binary.BigEndian.Uint16(pbuf[:]), nil
}

// allowed reports whether host:port is in the local test whitelist:
// loopback IPs, "localhost", configured extra hosts, and (if configured)
// allowed ports only.
func (s *Server) allowed(host string, port uint16) bool {
	if len(s.cfg.AllowPorts) > 0 && !s.cfg.AllowPorts[port] {
		return false
	}
	if ip := net.ParseIP(host); ip != nil {
		return ip.IsLoopback()
	}
	if strings.EqualFold(host, "localhost") {
		return true
	}
	for _, h := range s.cfg.ExtraHosts {
		if strings.EqualFold(host, h) {
			return true
		}
	}
	return false
}

func (s *Server) dial(host string, port uint16) (net.Conn, error) {
	d := net.Dialer{Timeout: s.cfg.DialTimeout}
	return d.Dial("tcp", net.JoinHostPort(host, fmt.Sprint(port)))
}

// sendReply writes a SOCKS5 reply. bound, if non-nil, is used for
// BND.ADDR/BND.PORT; otherwise 0.0.0.0:0 is sent.
func (s *Server) sendReply(conn net.Conn, rep byte, bound net.Addr) error {
	addr := "0.0.0.0"
	port := 0
	if ta, ok := bound.(*net.TCPAddr); ok && ta.IP != nil {
		addr = ta.IP.String()
		port = ta.Port
	}
	msg := []byte{version5, rep, 0x00, atypIPv4, 0, 0, 0, 0, 0, 0}
	if ip := net.ParseIP(addr); ip != nil {
		if v4 := ip.To4(); v4 != nil {
			copy(msg[4:8], v4)
		} else {
			msg = []byte{version5, rep, 0x00, atypIPv6}
			msg = append(msg, ip.To16()...)
			msg = append(msg, 0, 0)
		}
	}
	binary.BigEndian.PutUint16(msg[len(msg)-2:], uint16(port))
	_, err := conn.Write(msg)
	return err
}

// replyCodeFor maps dial errors to SOCKS5 reply codes.
func replyCodeFor(err error) byte {
	var nerr net.Error
	if errors.As(err, &nerr) && nerr.Timeout() {
		return repHostUnreachable
	}
	if errors.Is(err, syscall.ECONNREFUSED) {
		return repConnRefused
	}
	if errors.Is(err, syscall.EHOSTUNREACH) {
		return repHostUnreachable
	}
	if errors.Is(err, syscall.ENETUNREACH) {
		return repNetworkUnreachable
	}
	return repGeneralFailure
}

// relay copies data in both directions, propagating half-closes: when one
// side sends EOF, the write side of the peer is closed while the reverse
// direction keeps flowing, and both connections are fully released once
// both directions finish.
func relay(a, b net.Conn) {
	ta, aok := a.(*net.TCPConn)
	tb, bok := b.(*net.TCPConn)
	if aok && bok {
		relayTCP(ta, tb)
		return
	}
	// Fallback for non-TCP conns (e.g. in tests): no half-close support.
	done := make(chan struct{}, 2)
	go func() { io.Copy(a, b); done <- struct{}{} }()
	go func() { io.Copy(b, a); done <- struct{}{} }()
	<-done
	a.Close()
	b.Close()
	<-done
}

func relayTCP(a, b *net.TCPConn) {
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		io.Copy(a, b) // b -> a
		a.CloseWrite()
		b.CloseRead()
	}()
	go func() {
		defer wg.Done()
		io.Copy(b, a) // a -> b
		b.CloseWrite()
		a.CloseRead()
	}()
	wg.Wait()
}
