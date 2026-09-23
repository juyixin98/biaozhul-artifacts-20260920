package socks5

import (
	"bufio"
	"bytes"
	"errors"
	"io"
	"testing"
)

// oneByteReader delivers at most one byte per Read call. Parsing through it
// proves the handshake tolerates arbitrarily fragmented ("half-packet")
// client writes.
type oneByteReader struct{ data []byte }

func (r *oneByteReader) Read(p []byte) (int, error) {
	if len(r.data) == 0 {
		return 0, io.EOF
	}
	if len(p) == 0 {
		return 0, nil
	}
	p[0] = r.data[0]
	r.data = r.data[1:]
	return 1, nil
}

func (r *oneByteReader) ReadByte() (byte, error) {
	if len(r.data) == 0 {
		return 0, io.EOF
	}
	b := r.data[0]
	r.data = r.data[1:]
	return b, nil
}

func TestReadMethods(t *testing.T) {
	methods, err := readMethods(bufio.NewReader(bytes.NewReader([]byte{
		Ver5, 3, MethodNoAuth, MethodUserPass, 0x80,
	})))
	if err != nil {
		t.Fatalf("readMethods: %v", err)
	}
	if len(methods) != 3 || methods[1] != MethodUserPass {
		t.Fatalf("unexpected methods %v", methods)
	}
}

func TestReadMethodsOneByteAtATime(t *testing.T) {
	r := &oneByteReader{data: []byte{Ver5, 2, MethodNoAuth, MethodUserPass}}
	methods, err := readMethods(bufio.NewReader(r))
	if err != nil {
		t.Fatalf("fragmented readMethods: %v", err)
	}
	if len(methods) != 2 || methods[0] != MethodNoAuth || methods[1] != MethodUserPass {
		t.Fatalf("unexpected methods %v", methods)
	}
}

func TestReadMethodsRejectsBadVersion(t *testing.T) {
	_, err := readMethods(bufio.NewReader(bytes.NewReader([]byte{0x04, 1, MethodNoAuth})))
	if !errors.Is(err, errProtocol) {
		t.Fatalf("want errProtocol, got %v", err)
	}
}

func TestReadMethodsZeroMethods(t *testing.T) {
	_, err := readMethods(bufio.NewReader(bytes.NewReader([]byte{Ver5, 0})))
	if !errors.Is(err, errProtocol) {
		t.Fatalf("want errProtocol, got %v", err)
	}
}

func TestReadMethodsTruncated(t *testing.T) {
	// NMETHODS says 3 but only one method byte follows: must surface EOF.
	_, err := readMethods(bufio.NewReader(bytes.NewReader([]byte{Ver5, 3, MethodUserPass})))
	if err == nil {
		t.Fatal("expected error for truncated method list")
	}
}

func TestReadAuth(t *testing.T) {
	pkt := []byte{UserPassVer, 4, 'u', 's', 'e', 'r', 3, 'p', 'w', 0xff}
	user, pass, err := readAuth(bufio.NewReader(bytes.NewReader(pkt)))
	if err != nil {
		t.Fatalf("readAuth: %v", err)
	}
	if user != "user" || pass != "pw\xff" {
		t.Fatalf("got %q / %q", user, pass)
	}
}

func TestReadAuthOneByteAtATime(t *testing.T) {
	pkt := []byte{UserPassVer, 4, 'u', 's', 'e', 'r', 3, 'p', 'w', 0xff}
	user, pass, err := readAuth(bufio.NewReader(&oneByteReader{data: pkt}))
	if err != nil {
		t.Fatalf("fragmented readAuth: %v", err)
	}
	if user != "user" || pass != "pw\xff" {
		t.Fatalf("got %q / %q", user, pass)
	}
}

func TestReadAuthBadVersion(t *testing.T) {
	_, _, err := readAuth(bufio.NewReader(bytes.NewReader([]byte{0x05, 1, 'x', 1, 'y'})))
	if !errors.Is(err, errProtocol) {
		t.Fatalf("want errProtocol, got %v", err)
	}
}

func TestReadRequestIPv4(t *testing.T) {
	// VER CMD RSV ATYP DST.ADDR(4) DST.PORT(2)
	pkt := []byte{Ver5, CmdConnect, 0x00, ATypIPv4, 127, 0, 0, 1, 0x1F, 0x90}
	cmd, addr, err := readRequest(bufio.NewReader(bytes.NewReader(pkt)))
	if err != nil {
		t.Fatalf("readRequest: %v", err)
	}
	if cmd != CmdConnect || addr.host != "127.0.0.1" || addr.port != 8080 {
		t.Fatalf("got cmd=%d host=%q port=%d", cmd, addr.host, addr.port)
	}
}

func TestReadRequestIPv6Fragmented(t *testing.T) {
	ip := bytes.Repeat([]byte{0}, 15)
	ip = append(ip, 1) // ::1
	pkt := append([]byte{Ver5, CmdConnect, 0x00, ATypIPv6}, ip...)
	pkt = append(pkt, 0x00, 0x50) // 80
	cmd, addr, err := readRequest(bufio.NewReader(&oneByteReader{data: pkt}))
	if err != nil {
		t.Fatalf("fragmented IPv6 readRequest: %v", err)
	}
	if cmd != CmdConnect || addr.host != "::1" || addr.port != 80 {
		t.Fatalf("got cmd=%d host=%q port=%d", cmd, addr.host, addr.port)
	}
}

func TestReadRequestDomain(t *testing.T) {
	pkt := []byte{Ver5, CmdConnect, 0x00, ATypDomain, 9}
	pkt = append(pkt, []byte("localhost")...)
	pkt = append(pkt, 0x12, 0x34) // 4660
	cmd, addr, err := readRequest(bufio.NewReader(bytes.NewReader(pkt)))
	if err != nil {
		t.Fatalf("readRequest: %v", err)
	}
	if cmd != CmdConnect || addr.host != "localhost" || addr.port != 4660 {
		t.Fatalf("got cmd=%d host=%q port=%d", cmd, addr.host, addr.port)
	}
}

func TestReadRequestBadReserved(t *testing.T) {
	_, _, err := readRequest(bufio.NewReader(bytes.NewReader(
		[]byte{Ver5, CmdConnect, 0x01, ATypIPv4, 127, 0, 0, 1, 0, 80})))
	if !errors.Is(err, errProtocol) {
		t.Fatalf("want errProtocol, got %v", err)
	}
}

func TestReadRequestUnsupportedATyp(t *testing.T) {
	_, _, err := readRequest(bufio.NewReader(bytes.NewReader(
		[]byte{Ver5, CmdConnect, 0x00, 0x09, 0, 0, 0, 0, 0, 80})))
	if !errors.Is(err, errProtocol) {
		t.Fatalf("want errProtocol, got %v", err)
	}
}
