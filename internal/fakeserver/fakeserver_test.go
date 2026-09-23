package fakeserver_test

import (
	"net"
	"testing"
	"time"

	"dnscomp-proxy/internal/dnsmsg"
	"dnscomp-proxy/internal/fakeserver"
)

func exchange(t *testing.T, addr string, qname string, qtype uint16) ([]byte, []byte) {
	t.Helper()
	id, err := dnsmsg.NewID()
	if err != nil {
		t.Fatal(err)
	}
	req, err := dnsmsg.BuildQuery(id, qname, qtype)
	if err != nil {
		t.Fatal(err)
	}
	c, err := net.Dial("udp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if _, err := c.Write(req); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 1500)
	n, err := c.Read(buf)
	if err != nil {
		t.Fatal(err)
	}
	return req, buf[:n]
}

func TestZoneAnswerAndHits(t *testing.T) {
	zone := []fakeserver.RecordConfig{
		{Name: "example.com", Type: dnsmsg.TypeA, IP: net.IPv4(192, 0, 2, 10).To4(), TTL: 30},
	}
	srv, err := fakeserver.New("127.0.0.1:0", zone)
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	go func() { _ = srv.Serve() }()

	_, raw := exchange(t, srv.LocalAddr().String(), "example.com", dnsmsg.TypeA)
	m, _, err := dnsmsg.ParseMessage(raw)
	if err != nil {
		t.Fatalf("zone answer parse: %v", err)
	}
	if !m.QR || m.RCode != 0 || len(m.Answers) != 1 {
		t.Fatalf("zone answer wrong: %+v", m)
	}
	if net.IP(m.Answers[0].IP).String() != "192.0.2.10" || m.Answers[0].TTL != 30 {
		t.Fatalf("rr wrong: %+v", m.Answers[0])
	}

	exchange(t, srv.LocalAddr().String(), "example.com", dnsmsg.TypeA)
	if srv.Hits("EXAMPLE.com.") != 2 {
		t.Fatalf("hits=%d, want 2 (name canonicalized)", srv.Hits("EXAMPLE.com."))
	}
}

func TestUnknownNameIsNXDOMAIN(t *testing.T) {
	srv, err := fakeserver.New("127.0.0.1:0", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	go func() { _ = srv.Serve() }()

	_, raw := exchange(t, srv.LocalAddr().String(), "nope.example", dnsmsg.TypeA)
	m, _, err := dnsmsg.ParseMessage(raw)
	if err != nil {
		t.Fatal(err)
	}
	if m.RCode != dnsmsg.RCodeNXDomain || len(m.Answers) != 0 {
		t.Fatalf("want NXDOMAIN/empty, got %+v", m)
	}
}

func TestAddRecordAtRuntime(t *testing.T) {
	srv, err := fakeserver.New("127.0.0.1:0", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	go func() { _ = srv.Serve() }()

	exchange(t, srv.LocalAddr().String(), "added.example", dnsmsg.TypeA) // NXDOMAIN
	srv.AddRecord(fakeserver.RecordConfig{
		Name: "added.example", Type: dnsmsg.TypeA, IP: net.IPv4(192, 0, 2, 77).To4(), TTL: 9,
	})
	_, raw := exchange(t, srv.LocalAddr().String(), "added.example", dnsmsg.TypeA)
	m, _, err := dnsmsg.ParseMessage(raw)
	if err != nil {
		t.Fatal(err)
	}
	if m.RCode != 0 || len(m.Answers) != 1 || net.IP(m.Answers[0].IP).String() != "192.0.2.77" {
		t.Fatalf("runtime record not served: %+v", m)
	}
}

func TestGarbagePacketDropped(t *testing.T) {
	srv, err := fakeserver.New("127.0.0.1:0", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	go func() { _ = srv.Serve() }()

	c, err := net.Dial("udp", srv.LocalAddr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	c.Write([]byte{0, 1, 2, 3})
	_ = c.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
	buf := make([]byte, 100)
	if _, err := c.Read(buf); err == nil {
		t.Fatal("server must drop garbage instead of answering")
	}
}
