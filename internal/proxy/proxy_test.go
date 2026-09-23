package proxy

import (
	"encoding/binary"
	"errors"
	"net"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"dnscomp-proxy/internal/cache"
	"dnscomp-proxy/internal/dnsmsg"
)

// scriptedConn replays one scripted datagram (or times out when resp is nil).
type scriptedConn struct {
	query    []byte
	resp     []byte // nil => block until deadline
	deadline time.Time
}

func (c *scriptedConn) Read(b []byte) (int, error) {
	if c.resp == nil {
		// Simulate a deadline hit without sleeping long.
		return 0, &netError{timeout: true}
	}
	n := copy(b, c.resp)
	return n, nil
}
func (c *scriptedConn) Write(b []byte) (int, error) {
	c.query = append([]byte(nil), b...)
	return len(b), nil
}
func (c *scriptedConn) Close() error                       { return nil }
func (c *scriptedConn) LocalAddr() net.Addr                { return dummyAddr{} }
func (c *scriptedConn) RemoteAddr() net.Addr               { return dummyAddr{} }
func (c *scriptedConn) SetDeadline(t time.Time) error      { c.deadline = t; return nil }
func (c *scriptedConn) SetReadDeadline(t time.Time) error  { return nil }
func (c *scriptedConn) SetWriteDeadline(t time.Time) error { return nil }

type dummyAddr struct{}

func (dummyAddr) Network() string { return "udp" }
func (dummyAddr) String() string  { return "127.0.0.1:0" }

type netError struct{ timeout bool }

func (e *netError) Error() string   { return "i/o timeout" }
func (e *netError) Timeout() bool   { return e.timeout }
func (e *netError) Temporary() bool { return true }

// staticConn serves one fixed datagram.
type staticConn struct{ data []byte }

func (c *staticConn) Read(b []byte) (int, error)       { return copy(b, c.data), nil }
func (c *staticConn) Write(b []byte) (int, error)      { return len(b), nil }
func (c *staticConn) Close() error                     { return nil }
func (c *staticConn) LocalAddr() net.Addr              { return dummyAddr{} }
func (c *staticConn) RemoteAddr() net.Addr             { return dummyAddr{} }
func (c *staticConn) SetDeadline(time.Time) error      { return nil }
func (c *staticConn) SetReadDeadline(time.Time) error  { return nil }
func (c *staticConn) SetWriteDeadline(time.Time) error { return nil }

// newTestProxy wires a proxy whose next response times out; tests install
// their own dialer to script responses.
func newTestProxy(t *testing.T, _ []byte) (*Proxy, *cache.Cache, *scriptedConn) {
	t.Helper()
	c := cache.New()
	p, err := New(Config{UpstreamAddr: "127.0.0.1:1053", Timeout: 50 * time.Millisecond}, c)
	if err != nil {
		t.Fatal(err)
	}
	sc := &scriptedConn{}
	p.SetDialer(func() (net.Conn, error) { return sc, nil })
	return p, c, sc
}

// queryID extracts the ID the proxy sent.
func queryID(b []byte) uint16 { return binary.BigEndian.Uint16(b[0:2]) }

// respA builds a response to the captured query.
func respA(id uint16, name string, ttl uint32, ip4 ...byte) []byte {
	q, _ := dnsmsg.BuildQuery(id, name, dnsmsg.TypeA)
	// Replace header with a response header.
	hdr := make([]byte, dnsmsg.HeaderLen)
	binary.BigEndian.PutUint16(hdr[0:2], id)
	binary.BigEndian.PutUint16(hdr[2:4], 0x8180)
	binary.BigEndian.PutUint16(hdr[4:6], 1)
	binary.BigEndian.PutUint16(hdr[6:8], uint16(len(ip4)/4))
	b := append(hdr, q[dnsmsg.HeaderLen:]...)
	for i := 0; i < len(ip4); i += 4 {
		b = append(b, 0xc0, 0x0c)
		b = append(b, 0, 1, 0, 1)
		b = append(b, byte(ttl>>24), byte(ttl>>16), byte(ttl>>8), byte(ttl))
		b = append(b, 0, 4)
		b = append(b, ip4[i:i+4]...)
	}
	return b
}

func flagsHeader(id uint16, flags uint16, qd, an, ns, ar int, tail []byte) []byte {
	h := make([]byte, dnsmsg.HeaderLen)
	binary.BigEndian.PutUint16(h[0:2], id)
	binary.BigEndian.PutUint16(h[2:4], flags)
	binary.BigEndian.PutUint16(h[4:6], uint16(qd))
	binary.BigEndian.PutUint16(h[6:8], uint16(an))
	binary.BigEndian.PutUint16(h[8:10], uint16(ns))
	binary.BigEndian.PutUint16(h[10:12], uint16(ar))
	return append(h, tail...)
}

// --- Happy path -------------------------------------------------------------

func TestResolveSuccessAndCacheHit(t *testing.T) {
	p, c, _ := newTestProxy(t, nil)
	var calls int32
	p.SetDialer(func() (net.Conn, error) {
		atomic.AddInt32(&calls, 1)
		// Response must match the random ID the proxy chose, so build it
		// lazily: use a conn that reads the query first.
		return &idTrackingConn{build: func(q []byte) []byte {
			return respA(queryID(q), "example.com", 30, 192, 0, 2, 10)
		}}, nil
	})

	r1, err := p.Resolve("Example.COM.", dnsmsg.TypeA)
	if err != nil {
		t.Fatal(err)
	}
	if r1.Source != "upstream" || len(r1.Answers) != 1 || r1.Answers[0].IP != "192.0.2.10" {
		t.Fatalf("first resolve wrong: %+v", r1)
	}
	r2, err := p.Resolve("example.com", dnsmsg.TypeA)
	if err != nil {
		t.Fatal(err)
	}
	if r2.Source != "cache" {
		t.Fatalf("second resolve source=%q, want cache", r2.Source)
	}
	if calls != 1 {
		t.Fatalf("expected exactly 1 upstream call, got %d", calls)
	}
	if c.Len() != 1 {
		t.Fatalf("cache len=%d", c.Len())
	}
}

// idTrackingConn records the query and serves a dynamically built response.
type idTrackingConn struct {
	query []byte
	build func(q []byte) []byte
	sent  bool
}

func (c *idTrackingConn) Read(b []byte) (int, error) {
	if c.sent {
		return 0, &netError{timeout: true}
	}
	c.sent = true
	resp := c.build(c.query)
	return copy(b, resp), nil
}
func (c *idTrackingConn) Write(b []byte) (int, error) {
	c.query = append([]byte(nil), b...)
	return len(b), nil
}
func (c *idTrackingConn) Close() error                     { return nil }
func (c *idTrackingConn) LocalAddr() net.Addr              { return dummyAddr{} }
func (c *idTrackingConn) RemoteAddr() net.Addr             { return dummyAddr{} }
func (c *idTrackingConn) SetDeadline(time.Time) error      { return nil }
func (c *idTrackingConn) SetReadDeadline(time.Time) error  { return nil }
func (c *idTrackingConn) SetWriteDeadline(time.Time) error { return nil }

// --- Rejected responses -----------------------------------------------------

func TestIDMismatchRejectedAndNotCached(t *testing.T) {
	p, c, _ := newTestProxy(t, nil)
	p.SetDialer(func() (net.Conn, error) {
		return &idTrackingConn{build: func(q []byte) []byte {
			return respA(queryID(q)^0xffff, "example.com", 30, 1, 2, 3, 4) // guaranteed mismatch
		}}, nil
	})
	_, err := p.Resolve("example.com", dnsmsg.TypeA)
	if !errors.Is(err, ErrBadResponse) || !strings.Contains(err.Error(), "id mismatch") {
		t.Fatalf("want id mismatch error, got %v", err)
	}
	if c.Len() != 0 {
		t.Fatal("ID-mismatch response must not be cached")
	}
}

func TestTruncatedResponseRejected(t *testing.T) {
	p, c, _ := newTestProxy(t, nil)
	p.SetDialer(func() (net.Conn, error) {
		return &idTrackingConn{build: func(q []byte) []byte {
			full := respA(queryID(q), "example.com", 30, 1, 2, 3, 4)
			return full[:len(full)-6] // cut mid-answer
		}}, nil
	})
	_, err := p.Resolve("example.com", dnsmsg.TypeA)
	if !errors.Is(err, ErrBadResponse) {
		t.Fatalf("want bad response, got %v", err)
	}
	if c.Len() != 0 {
		t.Fatal("truncated response must not be cached")
	}
}

func TestSubHeaderDatagramRejected(t *testing.T) {
	p, c, _ := newTestProxy(t, nil)
	p.SetDialer(func() (net.Conn, error) {
		return &staticConn{data: []byte{0, 0, 0}}, nil
	})
	_, err := p.Resolve("example.com", dnsmsg.TypeA)
	if !errors.Is(err, ErrBadResponse) {
		t.Fatalf("want bad response, got %v", err)
	}
	if c.Len() != 0 {
		t.Fatal("short datagram must not be cached")
	}
}

func TestTCBitRejected(t *testing.T) {
	p, c, _ := newTestProxy(t, nil)
	p.SetDialer(func() (net.Conn, error) {
		return &idTrackingConn{build: func(q []byte) []byte {
			return flagsHeader(queryID(q), 0x8380, 1, 0, 0, 0, q[dnsmsg.HeaderLen:])
		}}, nil
	})
	_, err := p.Resolve("tc.example", dnsmsg.TypeA)
	if !errors.Is(err, ErrBadResponse) || !strings.Contains(err.Error(), "TC") {
		t.Fatalf("want TC error, got %v", err)
	}
	if c.Len() != 0 {
		t.Fatal("truncated-flag response must not be cached")
	}
}

func TestNXDOMAINRejectedAndNotCached(t *testing.T) {
	p, c, _ := newTestProxy(t, nil)
	p.SetDialer(func() (net.Conn, error) {
		return &idTrackingConn{build: func(q []byte) []byte {
			return flagsHeader(queryID(q), 0x8183, 1, 0, 0, 0, q[dnsmsg.HeaderLen:])
		}}, nil
	})
	_, err := p.Resolve("nx.example", dnsmsg.TypeA)
	if !errors.Is(err, ErrBadResponse) || !strings.Contains(err.Error(), "NXDOMAIN") {
		t.Fatalf("want NXDOMAIN error, got %v", err)
	}
	if c.Len() != 0 {
		t.Fatal("NXDOMAIN must not be cached")
	}
}

func TestNoAnswersRejected(t *testing.T) {
	p, c, _ := newTestProxy(t, nil)
	p.SetDialer(func() (net.Conn, error) {
		return &idTrackingConn{build: func(q []byte) []byte {
			return flagsHeader(queryID(q), 0x8180, 1, 0, 0, 0, q[dnsmsg.HeaderLen:])
		}}, nil
	})
	_, err := p.Resolve("noans.example", dnsmsg.TypeA)
	if !errors.Is(err, ErrBadResponse) || !strings.Contains(err.Error(), "empty") {
		t.Fatalf("want empty-answer error, got %v", err)
	}
	if c.Len() != 0 {
		t.Fatal("NOERROR/empty must not be cached")
	}
}

func TestCNAMEAnswerRejected(t *testing.T) {
	// A CNAME (type 5) answer to an A query must be rejected, not silently
	// ignored or cached.
	p, c, _ := newTestProxy(t, nil)
	p.SetDialer(func() (net.Conn, error) {
		return &idTrackingConn{build: func(q []byte) []byte {
			b := flagsHeader(queryID(q), 0x8180, 1, 1, 0, 0, q[dnsmsg.HeaderLen:])
			tgt, _ := dnsmsg.EncodeName("target.example")
			b = append(b, 0xc0, 0x0c) // owner -> question
			b = append(b, 0, 5, 0, 1) // type CNAME, class IN
			b = append(b, 0, 0, 0, 30)
			b = append(b, 0, byte(len(tgt)))
			b = append(b, tgt...)
			return b
		}}, nil
	})
	_, err := p.Resolve("cname.example", dnsmsg.TypeA)
	if !errors.Is(err, ErrBadResponse) {
		t.Fatalf("want CNAME rejection, got %v", err)
	}
	if c.Len() != 0 {
		t.Fatal("CNAME response must not be cached")
	}
}

func TestQuestionEchoMismatchRejected(t *testing.T) {
	// Response echoes a different name but a matching ID.
	p, c, _ := newTestProxy(t, nil)
	p.SetDialer(func() (net.Conn, error) {
		return &idTrackingConn{build: func(q []byte) []byte {
			return respA(queryID(q), "other.example", 30, 1, 2, 3, 4)
		}}, nil
	})
	_, err := p.Resolve("example.com", dnsmsg.TypeA)
	if !errors.Is(err, ErrBadResponse) || !strings.Contains(err.Error(), "question echo mismatch") {
		t.Fatalf("want echo-mismatch error, got %v", err)
	}
	if c.Len() != 0 {
		t.Fatal("mismatched echo must not be cached")
	}
}

// --- TTL boundaries ---------------------------------------------------------

func TestTTLZeroServedButNotCached(t *testing.T) {
	p, c, _ := newTestProxy(t, nil)
	p.SetDialer(func() (net.Conn, error) {
		return &idTrackingConn{build: func(q []byte) []byte {
			return respA(queryID(q), "tql0.example", 0, 192, 0, 2, 10)
		}}, nil
	})
	r, err := p.Resolve("tql0.example", dnsmsg.TypeA)
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Answers) != 1 || r.Answers[0].TTL != 0 {
		t.Fatalf("answer wrong: %+v", r)
	}
	if c.Len() != 0 {
		t.Fatal("TTL=0 must be returned to the caller but not cached")
	}
}

func TestMaxUint32TTLIsCached(t *testing.T) {
	p, c, _ := newTestProxy(t, nil)
	p.SetDialer(func() (net.Conn, error) {
		return &idTrackingConn{build: func(q []byte) []byte {
			return respA(queryID(q), "ttlmax.example", 4294967295, 192, 0, 2, 31)
		}}, nil
	})
	if _, err := p.Resolve("ttlmax.example", dnsmsg.TypeA); err != nil {
		t.Fatal(err)
	}
	if c.Len() != 1 {
		t.Fatalf("max-TTL answer must be cached, len=%d", c.Len())
	}
	snap := c.Snapshot()
	if len(snap) != 1 || snap[0].TTLRemain < 4294967294 {
		t.Fatalf("max-TTL remaining wrong: %+v", snap)
	}
}

func TestMinimumTTLOfMultipleAnswersApplies(t *testing.T) {
	p, c, _ := newTestProxy(t, nil)
	// First answer TTL 600, second answer TTL 5; cache lifetime must be 5.
	p.SetDialer(func() (net.Conn, error) {
		return &idTrackingConn{build: func(q []byte) []byte {
			id := queryID(q)
			b := respA(id, "multi.example", 600, 192, 0, 2, 1, 192, 0, 2, 2)
			ttlOff := len(b) - 16 + 6 // second answer's TTL field
			binary.BigEndian.PutUint32(b[ttlOff:ttlOff+4], 5)
			return b
		}}, nil
	})
	r, err := p.Resolve("multi.example", dnsmsg.TypeA)
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Answers) != 2 {
		t.Fatalf("want 2 answers, got %d", len(r.Answers))
	}
	snap := c.Snapshot()
	if len(snap) != 1 {
		t.Fatalf("snapshot len=%d", len(snap))
	}
	// Remaining TTL is rounded down from a real-time deadline; tolerate the
	// fraction of a second elapsed during the test, but it must come from
	// the 5s answer, never the 600s one.
	if rem := snap[0].TTLRemain; rem > 5 {
		t.Fatalf("cache lifetime must use min TTL (~5s), got %d", rem)
	}
}

// --- Upstream failure -------------------------------------------------------

func TestUpstreamTimeoutIsErrorAndNotCached(t *testing.T) {
	p, c, _ := newTestProxy(t, nil) // nil resp => timeout
	start := time.Now()
	_, err := p.Resolve("slow.example", dnsmsg.TypeA)
	if !errors.Is(err, ErrUpstream) {
		t.Fatalf("want upstream error, got %v", err)
	}
	if time.Since(start) > 2*time.Second {
		t.Fatal("timeout took too long")
	}
	if c.Len() != 0 {
		t.Fatal("timeout must not be cached")
	}
}

func TestInvalidQTypeAndName(t *testing.T) {
	p, _, _ := newTestProxy(t, nil)
	if _, err := p.Resolve("example.com", 15); !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("qtype: want invalid request, got %v", err)
	}
	if _, err := p.Resolve("bad..name", dnsmsg.TypeA); !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("name: want invalid request, got %v", err)
	}
}

func TestBadUpstreamAddress(t *testing.T) {
	if _, err := New(Config{UpstreamAddr: "not-an-address"}, cache.New()); err == nil {
		t.Fatal("expected error for malformed upstream address")
	}
}
