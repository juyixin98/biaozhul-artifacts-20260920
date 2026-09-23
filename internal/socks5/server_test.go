package socks5

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"testing"
	"time"
)

const (
	testUser = "u"
	testPass = "p"
)

// startServer launches a Server on 127.0.0.1:0 and returns it with its address.
func startServer(t *testing.T, cfg Config) (*Server, string) {
	t.Helper()
	if cfg.Username == "" {
		cfg.Username = testUser
		cfg.Password = testPass
	}
	s := NewServer(cfg)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go s.Serve(ln)
	t.Cleanup(func() { ln.Close() })
	return s, ln.Addr().String()
}

// startEcho launches a TCP echo server that, upon read-EOF, half-closes its
// write side but keeps the connection open until the client closes.
func startEcho(t *testing.T, network, addr string) string {
	t.Helper()
	ln, err := net.Listen(network, addr)
	if err != nil {
		t.Fatalf("echo listen %s %s: %v", network, addr, err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				io.Copy(c, c) // echoes; on client half-close, copy ends and we close
			}(c)
		}
	}()
	return ln.Addr().String()
}

func dial(t *testing.T, addr string) net.Conn {
	t.Helper()
	c, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	return c
}

func mustRead(t *testing.T, c net.Conn, n int) []byte {
	t.Helper()
	c.SetReadDeadline(time.Now().Add(5 * time.Second))
	buf := make([]byte, n)
	if _, err := io.ReadFull(c, buf); err != nil {
		t.Fatalf("read %d bytes: %v", n, err)
	}
	return buf
}

// greet performs method negotiation.
func greet(t *testing.T, c net.Conn, methods ...byte) byte {
	t.Helper()
	msg := append([]byte{0x05, byte(len(methods))}, methods...)
	if _, err := c.Write(msg); err != nil {
		t.Fatal(err)
	}
	resp := mustRead(t, c, 2)
	if resp[0] != 0x05 {
		t.Fatalf("bad greeting response version %d", resp[0])
	}
	return resp[1]
}

// auth performs RFC 1929 auth and returns the status byte.
func auth(t *testing.T, c net.Conn, user, pass string) byte {
	t.Helper()
	msg := []byte{0x01, byte(len(user))}
	msg = append(msg, user...)
	msg = append(msg, byte(len(pass)))
	msg = append(msg, pass...)
	if _, err := c.Write(msg); err != nil {
		t.Fatal(err)
	}
	resp := mustRead(t, c, 2)
	if resp[0] != 0x01 {
		t.Fatalf("bad auth response version %d", resp[0])
	}
	return resp[1]
}

// request sends a CONNECT request and returns the reply code byte.
func request(t *testing.T, c net.Conn, cmd byte, atyp byte, addr []byte, port uint16) byte {
	t.Helper()
	msg := []byte{0x05, cmd, 0x00, atyp}
	msg = append(msg, addr...)
	var p [2]byte
	binary.BigEndian.PutUint16(p[:], port)
	msg = append(msg, p[:]...)
	if _, err := c.Write(msg); err != nil {
		t.Fatal(err)
	}
	resp := mustRead(t, c, 4)
	if resp[0] != 0x05 {
		t.Fatalf("bad reply version %d", resp[0])
	}
	// consume BND.ADDR/BND.PORT
	var alen int
	switch resp[3] {
	case atypIPv4:
		alen = 4
	case atypIPv6:
		alen = 16
	case atypDomain:
		l := mustRead(t, c, 1)
		alen = int(l[0])
	default:
		t.Fatalf("bad reply atyp %d", resp[3])
	}
	mustRead(t, c, alen+2)
	return resp[1]
}

func splitHostPort(t *testing.T, addr string) (string, uint16) {
	t.Helper()
	h, ps, err := net.SplitHostPort(addr)
	if err != nil {
		t.Fatal(err)
	}
	var p uint16
	fmt.Sscanf(ps, "%d", &p)
	return h, p
}

func handshakeOK(t *testing.T, c net.Conn) {
	t.Helper()
	if m := greet(t, c, methodNoAuth, methodUserPass); m != methodUserPass {
		t.Fatalf("method = %#x, want %#x", m, methodUserPass)
	}
	if st := auth(t, c, testUser, testPass); st != authStatusSuccess {
		t.Fatalf("auth status = %#x, want success", st)
	}
}

func TestConnectIPv4(t *testing.T) {
	echo := startEcho(t, "tcp4", "127.0.0.1:0")
	_, addr := startServer(t, Config{})

	c := dial(t, addr)
	handshakeOK(t, c)
	host, port := splitHostPort(t, echo)
	if rep := request(t, c, cmdConnect, atypIPv4, net.ParseIP(host).To4(), port); rep != repSucceeded {
		t.Fatalf("reply = %#x, want success", rep)
	}
	c.SetReadDeadline(time.Time{})
	if _, err := c.Write([]byte("ping")); err != nil {
		t.Fatal(err)
	}
	if got := mustRead(t, c, 4); string(got) != "ping" {
		t.Fatalf("echo = %q, want %q", got, "ping")
	}
}

func TestConnectIPv6(t *testing.T) {
	probe, err := net.Listen("tcp6", "[::1]:0")
	if err != nil {
		t.Skipf("IPv6 loopback unavailable: %v", err)
	}
	probe.Close()
	echo := startEcho(t, "tcp6", "[::1]:0")
	_, addr := startServer(t, Config{})

	c := dial(t, addr)
	handshakeOK(t, c)
	host, port := splitHostPort(t, echo)
	if rep := request(t, c, cmdConnect, atypIPv6, net.ParseIP(host).To16(), port); rep != repSucceeded {
		t.Fatalf("reply = %#x, want success", rep)
	}
	c.SetReadDeadline(time.Time{})
	c.Write([]byte("v6"))
	if got := mustRead(t, c, 2); string(got) != "v6" {
		t.Fatalf("echo = %q", got)
	}
}

func TestConnectDomain(t *testing.T) {
	echo := startEcho(t, "tcp4", "127.0.0.1:0")
	_, addr := startServer(t, Config{})

	c := dial(t, addr)
	handshakeOK(t, c)
	_, port := splitHostPort(t, echo)
	if rep := request(t, c, cmdConnect, atypDomain, append([]byte{byte(len("localhost"))}, "localhost"...), port); rep != repSucceeded {
		t.Fatalf("reply = %#x, want success", rep)
	}
	c.SetReadDeadline(time.Time{})
	c.Write([]byte("dom"))
	if got := mustRead(t, c, 3); string(got) != "dom" {
		t.Fatalf("echo = %q", got)
	}
}

// TestHalfPacketHandshake feeds the greeting, auth and request one byte at a
// time with small delays; the server must tolerate fragmented packets.
func TestHalfPacketHandshake(t *testing.T) {
	echo := startEcho(t, "tcp4", "127.0.0.1:0")
	_, addr := startServer(t, Config{HandshakeTimeout: 5 * time.Second})

	c := dial(t, addr)
	writeSlow := func(b []byte) {
		t.Helper()
		for _, one := range b {
			if _, err := c.Write([]byte{one}); err != nil {
				t.Fatal(err)
			}
			time.Sleep(5 * time.Millisecond)
		}
	}

	writeSlow([]byte{0x05, 0x01, methodUserPass})
	if resp := mustRead(t, c, 2); resp[1] != methodUserPass {
		t.Fatalf("method = %#x", resp[1])
	}
	authMsg := []byte{0x01, 1, 'u', 1, 'p'}
	writeSlow(authMsg)
	if resp := mustRead(t, c, 2); resp[1] != authStatusSuccess {
		t.Fatalf("auth status = %#x", resp[1])
	}
	host, port := splitHostPort(t, echo)
	req := []byte{0x05, cmdConnect, 0x00, atypIPv4}
	req = append(req, net.ParseIP(host).To4()...)
	var p [2]byte
	binary.BigEndian.PutUint16(p[:], port)
	req = append(req, p[:]...)
	writeSlow(req)
	resp := mustRead(t, c, 10) // IPv4 reply
	if resp[1] != repSucceeded {
		t.Fatalf("reply = %#x, want success", resp[1])
	}
}

// TestHandshakeTimeout verifies a client that stalls mid-greeting is dropped
// once the handshake deadline passes, freeing the connection.
func TestHandshakeTimeout(t *testing.T) {
	s, addr := startServer(t, Config{HandshakeTimeout: 200 * time.Millisecond})

	c := dial(t, addr)
	if _, err := c.Write([]byte{0x05}); err != nil { // incomplete greeting
		t.Fatal(err)
	}
	c.SetReadDeadline(time.Now().Add(3 * time.Second))
	_, err := c.Read(make([]byte, 1))
	if err == nil {
		t.Fatal("expected server to close stalled connection")
	}
	waitFor(t, time.Second, func() bool { return s.Stats().ActiveConns == 0 })
	if got := s.Stats().HandshakeFails; got != 1 {
		t.Fatalf("handshake failures = %d, want 1", got)
	}
}

func TestNoAcceptableMethods(t *testing.T) {
	_, addr := startServer(t, Config{})
	c := dial(t, addr)
	if m := greet(t, c, methodNoAuth); m != methodNoAcceptable {
		t.Fatalf("method = %#x, want %#x (no acceptable)", m, methodNoAcceptable)
	}
	// server must close after rejecting
	c.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, err := c.Read(make([]byte, 1)); err == nil {
		t.Fatal("expected connection close after method rejection")
	}
}

func TestAuthFailure(t *testing.T) {
	s, addr := startServer(t, Config{})
	c := dial(t, addr)
	if m := greet(t, c, methodUserPass); m != methodUserPass {
		t.Fatalf("method = %#x", m)
	}
	if st := auth(t, c, testUser, "wrong"); st != authStatusFailure {
		t.Fatalf("auth status = %#x, want failure", st)
	}
	c.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, err := c.Read(make([]byte, 1)); err == nil {
		t.Fatal("expected connection close after auth failure")
	}
	waitFor(t, time.Second, func() bool { return s.Stats().AuthFails == 1 })
}

func TestUnsupportedCommand(t *testing.T) {
	_, addr := startServer(t, Config{})
	c := dial(t, addr)
	handshakeOK(t, c)
	// BIND (0x02) is not supported.
	if rep := request(t, c, 0x02, atypIPv4, net.ParseIP("127.0.0.1").To4(), 80); rep != repCmdNotSupported {
		t.Fatalf("reply = %#x, want %#x (command not supported)", rep, repCmdNotSupported)
	}
}

func TestUnsupportedAddressType(t *testing.T) {
	_, addr := startServer(t, Config{})
	c := dial(t, addr)
	handshakeOK(t, c)
	// ATYP 0x05 does not exist; server replies 0x08 and closes.
	msg := []byte{0x05, cmdConnect, 0x00, 0x05, 0, 0}
	c.Write(msg)
	resp := mustRead(t, c, 10)
	if resp[1] != repAtypNotSupported {
		t.Fatalf("reply = %#x, want %#x (atyp not supported)", resp[1], repAtypNotSupported)
	}
}

func TestWhitelistRejectsNonLoopback(t *testing.T) {
	s, addr := startServer(t, Config{})

	// Each rejected request ends the connection, so use one conn per case.
	c := dial(t, addr)
	handshakeOK(t, c)
	if rep := request(t, c, cmdConnect, atypIPv4, net.ParseIP("8.8.8.8").To4(), 53); rep != repNotAllowed {
		t.Fatalf("reply = %#x, want %#x (not allowed)", rep, repNotAllowed)
	}

	c2 := dial(t, addr)
	handshakeOK(t, c2)
	if rep := request(t, c2, cmdConnect, atypDomain, append([]byte{byte(len("example.com"))}, "example.com"...), 80); rep != repNotAllowed {
		t.Fatalf("domain reply = %#x, want %#x (not allowed)", rep, repNotAllowed)
	}
	waitFor(t, time.Second, func() bool { return s.Stats().Rejected == 2 })
}

func TestWhitelistPortRestriction(t *testing.T) {
	echo := startEcho(t, "tcp4", "127.0.0.1:0")
	_, port := splitHostPort(t, echo)
	_, addr := startServer(t, Config{AllowPorts: map[uint16]bool{port: true}})

	// allowed port succeeds
	c := dial(t, addr)
	handshakeOK(t, c)
	if rep := request(t, c, cmdConnect, atypIPv4, net.ParseIP("127.0.0.1").To4(), port); rep != repSucceeded {
		t.Fatalf("reply = %#x, want success", rep)
	}

	// other port rejected even on loopback
	c2 := dial(t, addr)
	handshakeOK(t, c2)
	if rep := request(t, c2, cmdConnect, atypIPv4, net.ParseIP("127.0.0.1").To4(), 1); rep != repNotAllowed {
		t.Fatalf("reply = %#x, want not allowed", rep)
	}
}

func TestDialRefused(t *testing.T) {
	_, addr := startServer(t, Config{})
	c := dial(t, addr)
	handshakeOK(t, c)
	// Port 1 on loopback should refuse.
	if rep := request(t, c, cmdConnect, atypIPv4, net.ParseIP("127.0.0.1").To4(), 1); rep != repConnRefused {
		t.Fatalf("reply = %#x, want %#x (connection refused)", rep, repConnRefused)
	}
}

// TestHalfClose verifies both half-close directions:
//  1. client half-closes -> target sees EOF, its final bytes still reach the client
//  2. target half-closes -> client sees EOF while it can still send
func TestHalfClose(t *testing.T) {
	// Custom target: read until EOF, then send a trailer and half-close its
	// write side, keeping the read side open until the client goes away.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c *net.TCPConn) {
				defer c.Close()
				io.Copy(io.Discard, c) // until client half-close
				c.Write([]byte("bye"))
				c.CloseWrite() // half-close towards client
				// keep reading (client may still send) until EOF/close
				io.Copy(io.Discard, c)
			}(c.(*net.TCPConn))
		}
	}()
	_, addr := startServer(t, Config{})

	c := dial(t, addr)
	handshakeOK(t, c)
	host, port := splitHostPort(t, ln.Addr().String())
	if rep := request(t, c, cmdConnect, atypIPv4, net.ParseIP(host).To4(), port); rep != repSucceeded {
		t.Fatalf("reply = %#x", rep)
	}
	c.SetReadDeadline(time.Time{})

	tc := c.(*net.TCPConn)
	if _, err := tc.Write([]byte("hello")); err != nil {
		t.Fatal(err)
	}
	if err := tc.CloseWrite(); err != nil { // client half-close
		t.Fatal(err)
	}
	// The trailer written by the target AFTER it saw our EOF must still arrive.
	if got := mustRead(t, c, 3); string(got) != "bye" {
		t.Fatalf("trailer = %q, want %q", got, "bye")
	}
	// Target then half-closed too: client must observe EOF.
	if _, err := c.Read(make([]byte, 1)); err != io.EOF {
		t.Fatalf("read after target half-close = %v, want EOF", err)
	}
}

// TestSlowReaderBackpressure streams a large payload through the proxy to a
// client that reads slowly, verifying (a) all data arrives intact and
// (b) the proxy does not buffer the whole payload at once (memory stays
// bounded — enforced structurally by fixed-size io.Copy buffers), and
// (c) both connections are released afterwards.
func TestSlowReaderBackpressure(t *testing.T) {
	const total = 8 << 20 // 8 MiB

	// Target that sends `total` bytes then half-closes.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				chunk := bytes.Repeat([]byte("x"), 64*1024)
				for sent := 0; sent < total; sent += len(chunk) {
					if _, err := c.Write(chunk); err != nil {
						return
					}
				}
			}(c)
		}
	}()
	s, addr := startServer(t, Config{})

	c := dial(t, addr)
	handshakeOK(t, c)
	host, port := splitHostPort(t, ln.Addr().String())
	if rep := request(t, c, cmdConnect, atypIPv4, net.ParseIP(host).To4(), port); rep != repSucceeded {
		t.Fatalf("reply = %#x", rep)
	}
	c.SetReadDeadline(time.Now().Add(30 * time.Second))

	// Read slowly, in small pieces.
	got := 0
	buf := make([]byte, 4096)
	for got < total {
		n, err := c.Read(buf)
		got += n
		if err != nil {
			t.Fatalf("read at %d/%d: %v", got, total, err)
		}
		if got%(1<<20) < 4096 {
			time.Sleep(5 * time.Millisecond) // simulate a slow consumer
		}
	}
	if got != total {
		t.Fatalf("received %d bytes, want %d", got, total)
	}
	c.Close()

	// Both sides must be released once the relay ends.
	waitFor(t, 2*time.Second, func() bool { return s.Stats().ActiveConns == 0 })
	if st := s.Stats(); st.Relayed != 1 {
		t.Fatalf("relayed = %d, want 1", st.Relayed)
	}
}

// TestConnectionResourceRelease opens and closes many connections (some
// completing the handshake, some not) and verifies the active counter
// returns to zero — i.e. no leaked goroutines/connections.
func TestConnectionResourceRelease(t *testing.T) {
	echo := startEcho(t, "tcp4", "127.0.0.1:0")
	s, addr := startServer(t, Config{})
	_, port := splitHostPort(t, echo)

	for i := 0; i < 20; i++ {
		c, err := net.Dial("tcp", addr)
		if err != nil {
			t.Fatal(err)
		}
		if i%2 == 0 {
			// full handshake + connect
			greet(t, c, methodUserPass)
			auth(t, c, testUser, testPass)
			if rep := request(t, c, cmdConnect, atypIPv4, net.ParseIP("127.0.0.1").To4(), port); rep != repSucceeded {
				t.Fatalf("iter %d: reply = %#x", i, rep)
			}
		} else {
			// abort mid-handshake
			c.Write([]byte{0x05, 0x01, methodUserPass})
			mustRead(t, c, 2)
		}
		c.Close()
	}
	waitFor(t, 2*time.Second, func() bool { return s.Stats().ActiveConns == 0 })
	st := s.Stats()
	if st.TotalConns != 20 {
		t.Fatalf("total = %d, want 20", st.TotalConns)
	}
	if st.ActiveConns != 0 {
		t.Fatalf("active = %d, want 0", st.ActiveConns)
	}
}

func TestServeRejectsNonLoopback(t *testing.T) {
	s := NewServer(Config{Username: "u", Password: "p"})
	ln, err := net.Listen("tcp", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	if err := s.Serve(ln); err == nil {
		t.Fatal("expected error serving on non-loopback address")
	}
}

func waitFor(t *testing.T, d time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("condition not met within", d)
}
