package socks5_test

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"socks5loop/internal/httpapi"
	"socks5loop/internal/socks5"
)

const (
	testUser = "alice"
	testPass = "s3cret!"
)

func testServer(t *testing.T, cfg socks5.Config) (*socks5.Server, net.Listener) {
	t.Helper()
	allow, err := socks5.ParseAllowList(socks5.DefaultAllowCIDRs, socks5.DefaultAllowHosts)
	if err != nil {
		t.Fatal(err)
	}
	cfg.Username = testUser
	cfg.Password = testPass
	cfg.Allow = allow
	if cfg.HandshakeTimeout == 0 {
		cfg.HandshakeTimeout = 2 * time.Second
	}
	cfg.Logger = slog.New(slog.NewTextHandler(io.Discard, nil))
	srv, err := socks5.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	ln, err := srv.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = ln.Close() })
	return srv, ln
}

// slowWriter fragments every Write into single bytes with a delay, simulating
// a client that delivers the handshake one byte at a time.
type slowWriter struct {
	c     net.Conn
	delay time.Duration
}

func (w slowWriter) write(p []byte) error {
	for _, b := range p {
		if _, err := w.c.Write([]byte{b}); err != nil {
			return err
		}
		time.Sleep(w.delay)
	}
	return nil
}

// socksConnect performs a full SOCKS5 handshake and CONNECT. host may be an
// IPv4/IPv6 literal or a domain. When slow is non-nil the negotiation bytes
// are sent through it one at a time.
func socksConnect(t *testing.T, proxy, targetHost string, targetPort uint16, user, pass string, slowDelay time.Duration) net.Conn {
	t.Helper()
	c, err := net.Dial("tcp", proxy)
	if err != nil {
		t.Fatalf("dial proxy: %v", err)
	}

	var send func([]byte) error
	if slowDelay > 0 {
		sw := slowWriter{c: c, delay: slowDelay}
		send = sw.write
	} else {
		send = func(p []byte) error { _, e := c.Write(p); return e }
	}

	// Method negotiation: offer NO AUTH and USERNAME/PASSWORD.
	if err := send([]byte{socks5.Ver5, 2, socks5.MethodNoAuth, socks5.MethodUserPass}); err != nil {
		t.Fatalf("send methods: %v", err)
	}
	reply := make([]byte, 2)
	if _, err := io.ReadFull(c, reply); err != nil {
		t.Fatalf("read method reply: %v", err)
	}
	if reply[0] != socks5.Ver5 || reply[1] != socks5.MethodUserPass {
		t.Fatalf("server did not select user/pass auth: %v", reply)
	}

	// Username/password sub-negotiation.
	auth := []byte{socks5.UserPassVer, byte(len(user))}
	auth = append(auth, user...)
	auth = append(auth, byte(len(pass)))
	auth = append(auth, pass...)
	if err := send(auth); err != nil {
		t.Fatalf("send auth: %v", err)
	}
	if _, err := io.ReadFull(c, reply); err != nil {
		t.Fatalf("read auth reply: %v", err)
	}
	if reply[0] != socks5.UserPassVer {
		t.Fatalf("bad auth reply version: %v", reply)
	}
	if reply[1] != socks5.AuthSuccess {
		t.Fatalf("auth failed (status %d)", reply[1])
	}

	// CONNECT request.
	req := []byte{socks5.Ver5, socks5.CmdConnect, 0x00}
	if ip := net.ParseIP(targetHost); ip != nil {
		if v4 := ip.To4(); v4 != nil {
			req = append(req, socks5.ATypIPv4)
			req = append(req, v4...)
		} else {
			req = append(req, socks5.ATypIPv6)
			req = append(req, ip.To16()...)
		}
	} else {
		req = append(req, socks5.ATypDomain, byte(len(targetHost)))
		req = append(req, targetHost...)
	}
	port := make([]byte, 2)
	binary.BigEndian.PutUint16(port, targetPort)
	req = append(req, port...)
	if err := send(req); err != nil {
		t.Fatalf("send connect: %v", err)
	}

	readConnectReply(t, c)
	return c
}

func readConnectReply(t *testing.T, c net.Conn) {
	t.Helper()
	br := bufio.NewReader(c)
	head := make([]byte, 4)
	if _, err := io.ReadFull(br, head); err != nil {
		t.Fatalf("read reply head: %v", err)
	}
	if head[0] != socks5.Ver5 {
		t.Fatalf("bad reply version %d", head[0])
	}
	if head[1] != socks5.RepSuccess {
		t.Fatalf("CONNECT rejected, reply code 0x%02x", head[1])
	}
	var skip int
	switch head[3] {
	case socks5.ATypIPv4:
		skip = 4 + 2
	case socks5.ATypIPv6:
		skip = 16 + 2
	case socks5.ATypDomain:
		l, err := br.ReadByte()
		if err != nil {
			t.Fatal(err)
		}
		skip = int(l) + 2
	default:
		t.Fatalf("bad atyp in reply: %d", head[3])
	}
	if _, err := io.ReadFull(br, make([]byte, skip)); err != nil {
		t.Fatalf("drain bnd: %v", err)
	}
}

// readRawReplyCode returns the REP code of a CONNECT reply without requiring
// success, so negative tests can assert the exact code.
func readRawReplyCode(t *testing.T, c net.Conn) byte {
	t.Helper()
	br := bufio.NewReader(c)
	head := make([]byte, 4)
	if _, err := io.ReadFull(br, head); err != nil {
		t.Fatalf("read reply head: %v", err)
	}
	return head[1]
}

func startEcho(t *testing.T, network, addr string) net.Listener {
	t.Helper()
	ln, err := net.Listen(network, addr)
	if err != nil {
		if network == "tcp6" {
			t.Skipf("IPv6 not available: %v", err)
		}
		t.Fatal(err)
	}
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				_, _ = io.Copy(c, c) // echo until EOF, then close
				_ = c.Close()
			}(c)
		}
	}()
	t.Cleanup(func() { _ = ln.Close() })
	return ln
}

// halfCloseEcho only writes the data back *after* seeing the client's EOF,
// which is what proves bidirectional half-close works through the proxy.
func halfCloseEcho(t *testing.T) net.Listener {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				data, _ := io.ReadAll(c) // unblocks when proxy half-closes
				_, _ = c.Write(data)
				_ = c.Close()
			}(c)
		}
	}()
	t.Cleanup(func() { _ = ln.Close() })
	return ln
}

func portOf(ln net.Listener) uint16 {
	return uint16(ln.Addr().(*net.TCPAddr).Port)
}

func waitActive(t *testing.T, srv *socks5.Server, want int64, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if srv.Stats().ActiveSessions == want {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("active sessions = %d, want %d (stats %+v)", srv.Stats().ActiveSessions, want, srv.Stats())
}

func TestEndToEndIPv4(t *testing.T) {
	srv, ln := testServer(t, socks5.Config{})
	echo := startEcho(t, "tcp4", "127.0.0.1:0")

	c := socksConnect(t, ln.Addr().String(), "127.0.0.1", portOf(echo), testUser, testPass, 0)
	defer c.Close()

	payload := []byte("hello socks5 over ipv4")
	if _, err := c.Write(payload); err != nil {
		t.Fatal(err)
	}
	got := make([]byte, len(payload))
	if _, err := io.ReadFull(c, got); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("echo mismatch: %q", got)
	}
	if srv.Stats().ConnectOK != 1 {
		t.Fatalf("ConnectOK = %d, want 1", srv.Stats().ConnectOK)
	}
}

func TestEndToEndIPv6(t *testing.T) {
	_, ln := testServer(t, socks5.Config{})
	echo := startEcho(t, "tcp6", "[::1]:0")

	c := socksConnect(t, ln.Addr().String(), "::1", portOf(echo), testUser, testPass, 0)
	defer c.Close()

	payload := []byte("hello socks5 over ipv6")
	if _, err := c.Write(payload); err != nil {
		t.Fatal(err)
	}
	got := make([]byte, len(payload))
	if _, err := io.ReadFull(c, got); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("echo mismatch: %q", got)
	}
}

func TestEndToEndDomainLocalhost(t *testing.T) {
	_, ln := testServer(t, socks5.Config{})
	echo := startEcho(t, "tcp4", "127.0.0.1:0")

	// ATYP=DOMAIN with "localhost"; the allowlist host rule must admit it.
	c := socksConnect(t, ln.Addr().String(), "localhost", portOf(echo), testUser, testPass, 0)
	defer c.Close()

	if _, err := c.Write([]byte("domain")); err != nil {
		t.Fatal(err)
	}
	got := make([]byte, 6)
	if _, err := io.ReadFull(c, got); err != nil {
		t.Fatal(err)
	}
	if string(got) != "domain" {
		t.Fatalf("echo mismatch: %q", got)
	}
}

func TestHalfPacketHandshake(t *testing.T) {
	_, ln := testServer(t, socks5.Config{})
	echo := startEcho(t, "tcp4", "127.0.0.1:0")

	// One byte every 2ms: methods + auth + CONNECT arrive in ~70 tiny
	// segments, interleaved with the server's replies.
	c := socksConnect(t, ln.Addr().String(), "127.0.0.1", portOf(echo), testUser, testPass, 2*time.Millisecond)
	defer c.Close()

	payload := []byte("after-fragmented-handshake")
	if _, err := c.Write(payload); err != nil {
		t.Fatal(err)
	}
	got := make([]byte, len(payload))
	if _, err := io.ReadFull(c, got); err != nil {
		t.Fatalf("echo after fragmented handshake: %v", err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("mismatch %q", got)
	}
}

func TestTruncatedHandshakeTimesOut(t *testing.T) {
	srv, ln := testServer(t, socks5.Config{HandshakeTimeout: 200 * time.Millisecond})

	c, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	waitActive(t, srv, 1, time.Second)

	// Send only the version byte and go silent.
	if _, err := c.Write([]byte{socks5.Ver5}); err != nil {
		t.Fatal(err)
	}

	// The server must close the idle half-handshake and release its resources.
	_ = c.SetReadDeadline(time.Now().Add(2 * time.Second))
	buf := make([]byte, 8)
	if n, err := c.Read(buf); err != io.EOF || n != 0 {
		t.Fatalf("expected server-side close, got n=%d err=%v", n, err)
	}
	waitActive(t, srv, 0, 2*time.Second)
}

func TestUnsupportedCommand(t *testing.T) {
	srv, ln := testServer(t, socks5.Config{})

	c, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()

	// Full auth, then CMD=BIND (0x02) to 127.0.0.1:9.
	c.Write([]byte{socks5.Ver5, 1, socks5.MethodUserPass})
	io.ReadFull(c, make([]byte, 2))
	auth := []byte{socks5.UserPassVer, byte(len(testUser))}
	auth = append(auth, testUser...)
	auth = append(auth, byte(len(testPass)))
	auth = append(auth, testPass...)
	c.Write(auth)
	io.ReadFull(c, make([]byte, 2))
	c.Write([]byte{
		socks5.Ver5, socks5.CmdBind, 0x00,
		socks5.ATypIPv4, 127, 0, 0, 1, 0, 9,
	})

	if code := readRawReplyCode(t, c); code != socks5.RepCmdNotSupported {
		t.Fatalf("reply code = 0x%02x, want 0x07", code)
	}
	if srv.Stats().Rejected == 0 {
		t.Fatal("rejected counter not incremented")
	}
}

func TestAuthFailure(t *testing.T) {
	_, ln := testServer(t, socks5.Config{})

	c, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	c.Write([]byte{socks5.Ver5, 1, socks5.MethodUserPass})
	if _, err := io.ReadFull(c, make([]byte, 2)); err != nil {
		t.Fatal(err)
	}
	auth := []byte{socks5.UserPassVer, byte(len(testUser))}
	auth = append(auth, testUser...)
	auth = append(auth, byte(len("wrong")))
	auth = append(auth, "wrong"...)
	c.Write(auth)

	reply := make([]byte, 2)
	if _, err := io.ReadFull(c, reply); err != nil {
		t.Fatal(err)
	}
	if reply[1] != socks5.AuthFail {
		t.Fatalf("auth status = 0x%02x, want 0x01", reply[1])
	}

	// After failure the server closes the connection; no request is accepted.
	_ = c.SetReadDeadline(time.Now().Add(time.Second))
	if n, err := c.Read(make([]byte, 4)); err != io.EOF || n != 0 {
		t.Fatalf("expected close after auth failure, n=%d err=%v", n, err)
	}

	// A failed session must not be able to retry plaintext commands either;
	// the closed socket proves it.
}

func TestNoAcceptableMethod(t *testing.T) {
	_, ln := testServer(t, socks5.Config{})
	c, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	c.Write([]byte{socks5.Ver5, 1, socks5.MethodNoAuth})
	reply := make([]byte, 2)
	if _, err := io.ReadFull(c, reply); err != nil {
		t.Fatal(err)
	}
	if reply[0] != socks5.Ver5 || reply[1] != socks5.MethodNone {
		t.Fatalf("want 05 FF, got %v", reply)
	}
}

func TestDestinationNotAllowed(t *testing.T) {
	srv, ln := testServer(t, socks5.Config{})
	c, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	c.Write([]byte{socks5.Ver5, 1, socks5.MethodUserPass})
	io.ReadFull(c, make([]byte, 2))
	auth := []byte{socks5.UserPassVer, byte(len(testUser))}
	auth = append(auth, testUser...)
	auth = append(auth, byte(len(testPass)))
	auth = append(auth, testPass...)
	c.Write(auth)
	io.ReadFull(c, make([]byte, 2))

	// CONNECT 8.8.8.8:53 (outside the local allowlist): REP 0x02.
	c.Write([]byte{socks5.Ver5, socks5.CmdConnect, 0x00, socks5.ATypIPv4, 8, 8, 8, 8, 0, 53})
	if code := readRawReplyCode(t, c); code != socks5.RepNotAllowed {
		t.Fatalf("reply code = 0x%02x, want 0x02", code)
	}
	if srv.Stats().Rejected == 0 {
		t.Fatal("allowlist rejection not counted")
	}
}

func TestConnectionRefusedReply(t *testing.T) {
	// Find a closed local port.
	probe, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	closedPort := uint16(probe.Addr().(*net.TCPAddr).Port)
	_ = probe.Close()

	_, ln := testServer(t, socks5.Config{})
	c, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	c.Write([]byte{socks5.Ver5, 1, socks5.MethodUserPass})
	io.ReadFull(c, make([]byte, 2))
	auth := []byte{socks5.UserPassVer, byte(len(testUser))}
	auth = append(auth, testUser...)
	auth = append(auth, byte(len(testPass)))
	auth = append(auth, testPass...)
	c.Write(auth)
	io.ReadFull(c, make([]byte, 2))
	c.Write([]byte{socks5.Ver5, socks5.CmdConnect, 0x00, socks5.ATypIPv4, 127, 0, 0, 1, byte(closedPort >> 8), byte(closedPort)})

	if code := readRawReplyCode(t, c); code != socks5.RepConnRefused {
		t.Fatalf("reply code = 0x%02x, want 0x05", code)
	}
}

func TestBidirectionalHalfClose(t *testing.T) {
	_, ln := testServer(t, socks5.Config{})
	echo := halfCloseEcho(t)

	c := socksConnect(t, ln.Addr().String(), "127.0.0.1", portOf(echo), testUser, testPass, 0)
	defer c.Close()

	payload := bytes.Repeat([]byte("half-close-"), 1000) // 11 KB
	if _, err := c.Write(payload); err != nil {
		t.Fatal(err)
	}
	// Shut OUR write side only. The proxy must propagate this as FIN to the
	// target while keeping the target->client direction open.
	if err := c.(*net.TCPConn).CloseWrite(); err != nil {
		t.Fatal(err)
	}

	// The echo server sends the body only after observing EOF, so receiving
	// the full payload proves the reverse direction survived the half-close.
	got, err := io.ReadAll(c)
	if err != nil {
		t.Fatalf("reading after half-close: %v", err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("half-close echo length = %d, want %d", len(got), len(payload))
	}
}

func TestSlowReaderBackpressureAndRelease(t *testing.T) {
	srv, ln := testServer(t, socks5.Config{})

	// Target: stream 8 MiB then signal completion once the kernel accepted it
	// all (which can only happen once the client finally drains).
	target, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = target.Close() })
	// 64 MiB: with stock Linux autotuning limits (tcp_rmem max 6 MiB +
	// tcp_wmem max 4 MiB per socket) the whole target->proxy->client path
	// holds under ~12 MiB while nobody reads, so the counter must plateau far
	// below the payload regardless of buffer sizes.
	const total = 64 << 20
	sent := make(chan struct{})
	go func() {
		c, err := target.Accept()
		if err != nil {
			return
		}
		blob := make([]byte, total)
		for i := range blob {
			blob[i] = byte(i % 251)
		}
		_, _ = c.Write(blob) // blocks once sender + receiver buffers fill
		close(sent)
		_ = c.Close()
	}()

	c := socksConnect(t, ln.Addr().String(), "127.0.0.1", portOf(target), testUser, testPass, 0)
	t.Cleanup(func() { _ = c.Close() })
	waitActive(t, srv, 1, time.Second)

	// Backpressure window: while the client reads nothing, every buffer on
	// the target->proxy->client path fills and the proxy's io.Copy blocks in
	// Write. Wait for the counter to go non-zero and then plateau (no growth
	// across two quiet windows) below the combined in-flight capacity, which
	// under stock Linux limits (tcp_rmem max 6 MiB + tcp_wmem max 4 MiB per
	// socket) is well under 12 MiB, far below the 64 MiB body. Polling rather
	// than sleeping makes this independent of scheduler timing.
	deadline := time.Now().Add(5 * time.Second)
	var stalled int64 = -1
	quietWindows := 0
	for {
		now := srv.Stats().TargetToClient
		if now >= 12<<20 {
			t.Fatalf("no backpressure: proxy pumped %d bytes to an unreading client", now)
		}
		if stalled >= 0 && now == stalled {
			quietWindows++
		} else {
			quietWindows = 0
		}
		stalled = now

		// Three consecutive 100ms windows with no growth and data already in
		// flight => the pipeline is full and backpressure is holding.
		if stalled > 0 && quietWindows >= 2 {
			time.Sleep(200 * time.Millisecond)
			if later := srv.Stats().TargetToClient; later > stalled+64<<10 {
				t.Fatalf("bytes kept flowing without a reader: %d -> %d", stalled, later)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("backpressure plateau never stabilized, counter=%d", stalled)
		}
		time.Sleep(100 * time.Millisecond)
	}

	// Drain at full speed now, then verify byte integrity: backpressure
	// throttles but never drops or reorders bytes.
	got := make([]byte, total)
	if _, err := io.ReadFull(c, got); err != nil {
		t.Fatalf("drain: %v", err)
	}
	for i := range got {
		if got[i] != byte(i%251) {
			t.Fatalf("data corruption at %d", i)
		}
	}
	<-sent

	// Both peers closing must release the session and its goroutines.
	_ = c.Close()
	waitActive(t, srv, 0, 3*time.Second)
	if n := srv.Stats().TargetToClient; n < total {
		t.Fatalf("byte counter = %d, want >= %d", n, total)
	}
}

func TestRefusesNonLoopbackBind(t *testing.T) {
	allow, _ := socks5.ParseAllowList(socks5.DefaultAllowCIDRs, socks5.DefaultAllowHosts)
	srv, err := socks5.New(socks5.Config{
		Username: testUser, Password: testPass, Allow: allow,
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	ln, err := srv.Listen("tcp", "0.0.0.1:0")
	if err == nil {
		_ = ln.Close()
		t.Fatal("server must refuse a non-loopback bind")
	}
}

func TestRequiresCredentialsAndAllowList(t *testing.T) {
	if _, err := socks5.New(socks5.Config{Username: "", Password: "x", Allow: &socks5.AllowList{}}); err == nil {
		t.Fatal("empty username must be rejected")
	}
	allow, _ := socks5.ParseAllowList(nil, nil)
	if _, err := socks5.New(socks5.Config{Username: "u", Password: "p", Allow: allow}); err != nil {
		// allowlist without rules is permitted; only nil must fail.
		t.Fatalf("empty-but-non-nil allowlist should be allowed: %v", err)
	}
	if _, err := socks5.New(socks5.Config{Username: "u", Password: "p"}); err == nil {
		t.Fatal("nil allowlist must be rejected")
	}
}

func TestHTTPEndToEndThroughProxy(t *testing.T) {
	_, ln := testServer(t, socks5.Config{})
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "hello-over-socks5")
	}))
	t.Cleanup(origin.Close)
	host, portStr, _ := net.SplitHostPort(origin.Listener.Addr().String())
	var port uint16
	fmt.Sscanf(portStr, "%d", &port)

	c := socksConnect(t, ln.Addr().String(), host, port, testUser, testPass, 0)
	defer c.Close()
	if _, err := c.Write([]byte("GET / HTTP/1.1\r\nHost: origin\r\nConnection: close\r\n\r\n")); err != nil {
		t.Fatal(err)
	}
	resp, err := http.ReadResponse(bufio.NewReader(c), nil)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 || string(body) != "hello-over-socks5" {
		t.Fatalf("got %d %q", resp.StatusCode, body)
	}
}

func TestHTTPControlAPI(t *testing.T) {
	srv, _ := testServer(t, socks5.Config{})
	h := httpapi.Handler(srv, socks5.DefaultAllowCIDRs, socks5.DefaultAllowHosts)
	ts := httptest.NewServer(h)
	t.Cleanup(ts.Close)

	for _, path := range []string{"/healthz", "/stats", "/allowlist"} {
		resp, err := http.Get(ts.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		if resp.StatusCode != 200 {
			t.Fatalf("%s status %d", path, resp.StatusCode)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if !strings.Contains(string(body), "{") {
			t.Fatalf("%s returned non-JSON: %s", path, body)
		}
	}
}

// TestConcurrentSessions makes sure many tunnels multiplex correctly and all
// release.
func TestConcurrentSessions(t *testing.T) {
	srv, ln := testServer(t, socks5.Config{})
	echo := startEcho(t, "tcp4", "127.0.0.1:0")

	var wg sync.WaitGroup
	for i := 0; i < 32; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			c := socksConnect(t, ln.Addr().String(), "127.0.0.1", portOf(echo), testUser, testPass, 0)
			defer c.Close()
			msg := []byte(fmt.Sprintf("session-%d", i))
			c.Write(msg)
			got := make([]byte, len(msg))
			if _, err := io.ReadFull(c, got); err != nil {
				t.Errorf("session %d: %v", i, err)
				return
			}
			if string(got) != string(msg) {
				t.Errorf("session %d mismatch: %q", i, got)
			}
		}(i)
	}
	wg.Wait()
	waitActive(t, srv, 0, 3*time.Second)
	if srv.Stats().ConnectOK != 32 {
		t.Fatalf("ConnectOK = %d, want 32", srv.Stats().ConnectOK)
	}
}
