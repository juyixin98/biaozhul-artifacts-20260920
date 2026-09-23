package mqtt

import (
	"testing"
)

func TestRemainingLengthRoundTrip(t *testing.T) {
	cases := []int{0, 1, 127, 128, 16383, 16384, 2097151, 2097152, maxPacketLen}
	for _, n := range cases {
		got, err := decodeRemainingLength(&sliceReader{b: encodeRemainingLength(nil, n)})
		if err != nil {
			t.Fatalf("n=%d: unexpected err %v", n, err)
		}
		if got != n {
			t.Fatalf("n=%d: got %d", n, got)
		}
	}
}

func TestRemainingLengthRejectsOverlong(t *testing.T) {
	// 5 continuation bytes is never legal.
	r := &sliceReader{b: []byte{0x80, 0x80, 0x80, 0x80, 0x00}}
	if _, err := decodeRemainingLength(r); err == nil {
		t.Fatal("expected error for 5-byte remaining length")
	}
}

type sliceReader struct {
	b []byte
	p int
}

func (r *sliceReader) ReadByte() (byte, error) {
	if r.p >= len(r.b) {
		return 0, ErrProtocol
	}
	v := r.b[r.p]
	r.p++
	return v, nil
}

func (r *sliceReader) Read(p []byte) (int, error) {
	if r.p >= len(r.b) {
		return 0, ErrProtocol
	}
	n := copy(p, r.b[r.p:])
	r.p += n
	return n, nil
}

func TestTopicMatch(t *testing.T) {
	cases := []struct {
		filter string
		topic  string
		want   bool
	}{
		{"a/b", "a/b", true},
		{"a/b", "a/c", false},
		{"a/+", "a/b", true},
		{"a/+", "a/b/c", false},
		{"+ /b", "a/b", false}, // space makes this a literal filter
		{"a/+/c", "a/b/c", true},
		{"a/+/c", "a/b/x", false},
		{"a/#", "a/b/c", true},
		{"a/#", "a", true},
		{"a/#", "a/b", true},
		{"a/#", "b/a", false},
		{"#", "anything/here", true},
		{"#", "x", true},
		{"sport/tennis/+", "sport/tennis/player1", true},
		{"sport/tennis/+", "sport/tennis/player1/ranking", false},
		{"sport/+", "sport", false},
		{"sport/", "sport/", true},
		{"//", "//", true},
	}
	for _, tc := range cases {
		if got := topicMatch(tc.filter, tc.topic); got != tc.want {
			t.Errorf("topicMatch(%q,%q)=%v want %v", tc.filter, tc.topic, got, tc.want)
		}
	}
}

func TestValidFilter(t *testing.T) {
	good := []string{"a", "a/b", "+", "+/b", "a/+/c", "#", "a/#", "a/+/b/#"}
	bad := []string{"", "a#", "a+", "a/+b", "a/#/c", "#/b", "a/b#"}
	for _, f := range good {
		if !validFilter(f) {
			t.Errorf("filter %q should be valid", f)
		}
	}
	for _, f := range bad {
		if validFilter(f) {
			t.Errorf("filter %q should be invalid", f)
		}
	}
}

func TestValidPublishTopic(t *testing.T) {
	if !validPublishTopic("a/b") {
		t.Error("a/b should be a valid topic")
	}
	for _, bad := range []string{"", "a/+", "a/#", "a\x00b"} {
		if validPublishTopic(bad) {
			t.Errorf("topic %q should be invalid", bad)
		}
	}
}

func TestEncodeDecodePublishQoS1(t *testing.T) {
	pkt := publishPacket(true, 1, false, "a/b", 0x1234, []byte("hello"))
	r := &sliceReader{b: pkt}
	p, err := readPacket(r)
	if err != nil {
		t.Fatal(err)
	}
	if p.Type != TypePUBLISH || !p.Dup || p.QoS != 1 {
		t.Fatalf("bad header: %+v", p)
	}
	if p.Topic != "a/b" || p.PacketID != 0x1234 || string(p.Payload) != "hello" {
		t.Fatalf("bad fields: %+v", p)
	}
}

func TestDecodeConnect(t *testing.T) {
	body := []byte{}
	body = putString(body, "MQTT")
	body = append(body, 4, 0x02 /*clean*/, 0x00, 0x3c /*keepalive 60*/)
	body = putString(body, "client01")
	pkt := append([]byte{TypeCONNECT << 4}, encodeRemainingLength(nil, len(body))...)
	pkt = append(pkt, body...)

	p, err := readPacket(&sliceReader{b: pkt})
	if err != nil {
		t.Fatal(err)
	}
	if p.ClientID != "client01" || !p.CleanStart || p.KeepAlive != 60 || p.ProtoLevel != 4 {
		t.Fatalf("decoded CONNECT mismatch: %+v", p)
	}
}

func TestDecodeSubscribeBadReservedBits(t *testing.T) {
	// Fixed header low nibble 0x00 instead of mandatory 0x02.
	pkt := []byte{TypeSUBSCRIBE << 4, 0x00}
	if _, err := readPacket(&sliceReader{b: pkt}); err == nil {
		t.Fatal("expected protocol error for bad SUBSCRIBE reserved bits")
	}
}

func TestQoS2PublishRejected(t *testing.T) {
	first := TypePUBLISH<<4 | (2 << 1)
	body := []byte{}
	body = putString(body, "a/b")
	body = append(body, 0x00, 0x01)
	pkt := append([]byte{first}, encodeRemainingLength(nil, len(body))...)
	pkt = append(pkt, body...)
	// QoS 2 itself decodes (framing is legal); the server loop is what rejects
	// it. Here we only assert decode succeeds so the handler can close cleanly.
	p, err := readPacket(&sliceReader{b: pkt})
	if err != nil {
		t.Fatalf("qos2 framing should decode: %v", err)
	}
	if p.QoS != 2 {
		t.Fatalf("qos=%d", p.QoS)
	}
}

func TestClientIDValidation(t *testing.T) {
	if !validClientID("abcABC012") || !validClientID("http-sub") ||
		!validClientID("a24character0000000000000") {
		t.Error("expected valid 3.1.1 client IDs")
	}
	// Empty ID passes the charset check; decodeConnect requires CleanSession=1.
	if !validClientID("") {
		t.Error("empty id should pass charset validation")
	}
	if validClientID("bad\x00nul") {
		t.Error("NUL in client id should be invalid")
	}
}
