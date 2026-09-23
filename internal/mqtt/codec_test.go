package mqtt

import (
	"bufio"
	"bytes"
	"testing"
)

func TestRemainingLengthRoundTrip(t *testing.T) {
	cases := []int{0, 1, 127, 128, 16383, 16384, 2097151, 2097152, 268435455}
	for _, n := range cases {
		got, err := DecodeRemainingLength(bytes.NewReader(EncodeRemainingLength(n)))
		if err != nil {
			t.Fatalf("n=%d: %v", n, err)
		}
		if got != n {
			t.Fatalf("n=%d round-tripped as %d", n, got)
		}
	}
}

func TestDecodeConnect(t *testing.T) {
	// Clean session, client id, keepalive 60.
	orig := &ConnectPacket{ClientID: "dev-1", CleanSession: true, KeepAlive: 60}
	fr := EncodeConnect(orig)
	// strip fixed header to feed the body decoder
	body := stripFixed(t, fr)
	p, err := DecodeConnect(body)
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if p.ClientID != "dev-1" || !p.CleanSession || p.KeepAlive != 60 {
		t.Fatalf("bad decode: %+v", p)
	}

	// Wrong protocol level -> ConnCodeError code 1.
	bad := bytes.Replace(body, []byte{0x00, 0x04, 'M', 'Q', 'T', 'T', 4}, []byte{0x00, 0x04, 'M', 'Q', 'T', 'T', 3}, 1)
	if bytes.Equal(bad, body) {
		// locate level byte differently: find "MQTT" then next byte
		idx := bytes.Index(body, []byte("MQTT"))
		bad = append([]byte{}, body...)
		bad[idx+4] = 3
	}
	_, err = DecodeConnect(bad)
	var ce *ConnCodeError
	if !asCode(err, &ce) || ce.Code != ConnBadProtocolVersion {
		t.Fatalf("want protocol version rejection, got %v", err)
	}
}

func TestDecodeConnectReservedBit(t *testing.T) {
	p := &ConnectPacket{ClientID: "x", CleanSession: true}
	raw := EncodeConnect(p)
	// CONNECT flags byte: after protocol name (6) + level (1) = index 7
	// relative to body start; find it via the frame: body starts at
	// offset 2 for short packets ("MQTT" + flags encoding).
	body := stripFixed(t, raw)
	idx := bytes.Index(body, []byte("MQTT")) + 5
	body[idx] |= 0x01
	if _, err := DecodeConnect(body); err == nil {
		t.Fatal("reserved bit set must be a protocol error")
	}
}

func TestEmptyClientIDRequiresClean(t *testing.T) {
	p := &ConnectPacket{ClientID: "", CleanSession: false, KeepAlive: 10}
	body := stripFixed(t, EncodeConnect(p))
	_, err := DecodeConnect(body)
	var ce *ConnCodeError
	if !asCode(err, &ce) || ce.Code != ConnIdentifierRejected {
		t.Fatalf("want identifier rejected, got %v", err)
	}
}

func TestPublishRoundTrip(t *testing.T) {
	for qos := byte(0); qos <= 1; qos++ {
		p := &PublishPacket{QoS: qos, Topic: "a/b", Payload: []byte("hi"), PacketID: 7, Dup: qos == 1}
		raw := EncodePublish(p)
		fr, err := ReadFrame(bufio.NewReader(bytes.NewReader(raw)))
		if err != nil {
			t.Fatalf("qos=%d read: %v", qos, err)
		}
		got, err := DecodePublish(fr.Flags(), fr.Body)
		if err != nil {
			t.Fatalf("qos=%d decode: %v", qos, err)
		}
		if got.Topic != "a/b" || string(got.Payload) != "hi" {
			t.Fatalf("qos=%d bad content %+v", qos, got)
		}
		if qos == 1 && (got.PacketID != 7 || !got.Dup) {
			t.Fatalf("qos1 flags lost: %+v", got)
		}
		if qos == 0 && got.PacketID != 0 {
			t.Fatalf("qos0 must have no pid, got %d", got.PacketID)
		}
	}
}

func TestQoS0DupRejected(t *testing.T) {
	p := &PublishPacket{QoS: 0, Topic: "t", Payload: nil, Dup: true}
	_, err := DecodePublish(EncodePublish(p)[0]&0x0F, stripFixed(t, EncodePublish(p)))
	if err == nil {
		t.Fatal("DUP on QoS0 must be a protocol error")
	}
}

func TestSubscribeCodec(t *testing.T) {
	subs := []Subscription{{Filter: "a/+", MaxQoS: 1}, {Filter: "b/#", MaxQoS: 0}}
	raw := EncodeSubscribe(11, subs)
	fr, err := ReadFrame(bufio.NewReader(bytes.NewReader(raw)))
	if err != nil {
		t.Fatal(err)
	}
	pid, got, err := DecodeSubscribe(fr.Flags(), fr.Body)
	if err != nil {
		t.Fatal(err)
	}
	if pid != 11 || len(got) != 2 || got[0].Filter != "a/+" || got[1].MaxQoS != 0 {
		t.Fatalf("bad: pid=%d %+v", pid, got)
	}
}

func TestTopicMatch(t *testing.T) {
	cases := []struct {
		filter, name string
		want         bool
	}{
		{"a/b", "a/b", true},
		{"a/b", "a/c", false},
		{"a/+", "a/b", true},
		{"a/+", "a/b/c", false},
		{"a/+/c", "a/b/c", true},
		{"a/#", "a/b/c", true},
		{"a/#", "a", false}, // §4.7.2: # must consume a level
		{"#", "anything/here", true},
		{"#", "x", true},
		{"x/#", "x/y", true},
		{"+", "one", true},
		{"+", "one/two", false},
	}
	for _, tc := range cases {
		if got := TopicMatch(tc.filter, tc.name); got != tc.want {
			t.Errorf("TopicMatch(%q,%q)=%v want %v", tc.filter, tc.name, got, tc.want)
		}
	}
}

func TestValidFilter(t *testing.T) {
	good := []string{"#", "a/#", "a/+/b", "+", "a/+", "x/y/z"}
	bad := []string{"", "a#", "a#/b", "#/b", "a+/c", "a/+x"}
	for _, f := range good {
		if !ValidFilter(f) {
			t.Errorf("filter %q should be valid", f)
		}
	}
	for _, f := range bad {
		if ValidFilter(f) {
			t.Errorf("filter %q should be invalid", f)
		}
	}
}

// stripFixed removes fixed header byte + remaining length to return body.
func stripFixed(t *testing.T, pkt []byte) []byte {
	t.Helper()
	i := 1
	for {
		b := pkt[i]
		i++
		if b&0x80 == 0 {
			break
		}
	}
	return pkt[i:]
}

func asCode(err error, ce **ConnCodeError) bool {
	if err == nil {
		return false
	}
	if e, ok := err.(*ConnCodeError); ok {
		*ce = e
		return true
	}
	return false
}
