package mqtt

import (
	"bytes"
	"encoding/binary"
	"io"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// ---- test harness: a tiny raw MQTT 3.1.1 client over TCP ----

type testClient struct {
	t   *testing.T
	raw net.Conn
}

func dial(t *testing.T, addr, clientID string, clean bool, keepAlive uint16) *testClient {
	t.Helper()
	raw := mustDial(t, addr)
	c := &testClient{t: t, raw: raw}
	c.sendConnect(clientID, clean, keepAlive, false, 4, "MQTT")
	return c
}

// connectPacket builds a CONNECT. will=true sets the Will flag.
func connectPacket(clientID string, clean bool, keepAlive uint16, will bool, level byte, proto string) []byte {
	var flags byte
	if clean {
		flags |= 0x02
	}
	if will {
		flags |= 0x04 // Will QoS 0
	}
	body := []byte{}
	body = putString(body, proto)
	body = append(body, level, flags, byte(keepAlive>>8), byte(keepAlive))
	body = putString(body, clientID)
	if will {
		body = putString(body, "will/topic")
		body = append(body, 0, 4)
		body = append(body, 'b', 'y', 'e', '!')
	}
	out := []byte{TypeCONNECT << 4}
	out = encodeRemainingLength(out, len(body))
	return append(out, body...)
}

// sendConnect returns the Session Present flag.
func (c *testClient) sendConnect(id string, clean bool, ka uint16, will bool, level byte, proto string) bool {
	c.t.Helper()
	if _, err := c.raw.Write(connectPacket(id, clean, ka, will, level, proto)); err != nil {
		c.t.Fatalf("write connect: %v", err)
	}
	first, body := c.readRaw()
	if first != 0x20 || len(body) != 2 {
		c.t.Fatalf("expected CONNACK, got first=%02x body=% x", first, body)
	}
	if body[1] != rcAccepted {
		c.t.Fatalf("CONNACK rejected: code=%d", body[1])
	}
	return body[0] == 1
}

func (c *testClient) sendWillConnect(id string) byte {
	c.t.Helper()
	if _, err := c.raw.Write(connectPacket(id, true, 0, true, 4, "MQTT")); err != nil {
		c.t.Fatalf("write: %v", err)
	}
	_, body := c.readRaw()
	return body[1]
}

func (c *testClient) subscribe(pid uint16, qos byte, filters ...string) []byte {
	c.t.Helper()
	body := []byte{byte(pid >> 8), byte(pid)}
	for _, f := range filters {
		body = putString(body, f)
		body = append(body, qos)
	}
	out := []byte{0x82} // SUBSCRIBE, reserved bits 0010
	out = encodeRemainingLength(out, len(body))
	c.write(append(out, body...))
	_, sb := c.readRaw() // SUBACK
	if len(sb) < 3 || binary.BigEndian.Uint16(sb[:2]) != pid {
		c.t.Fatalf("bad SUBACK: % x", sb)
	}
	return sb[2:]
}

func (c *testClient) unsubscribe(pid uint16, filters ...string) {
	body := []byte{byte(pid >> 8), byte(pid)}
	for _, f := range filters {
		body = putString(body, f)
	}
	out := []byte{0xA2}
	out = encodeRemainingLength(out, len(body))
	c.write(append(out, body...))
	_, ub := c.readRaw()
	if len(ub) != 2 || binary.BigEndian.Uint16(ub) != pid {
		c.t.Fatalf("bad UNSUBACK: % x", ub)
	}
}

func (c *testClient) publish(dup bool, qos byte, pid uint16, topic string, payload []byte) {
	c.write(publishPacket(dup, qos, false, topic, pid, payload))
}

func (c *testClient) puback(pid uint16) { c.write(pubackPacket(pid)) }
func (c *testClient) ping()             { c.write([]byte{0xC0, 0x00}) }
func (c *testClient) disconnect()       { c.write([]byte{0xE0, 0x00}); _ = c.raw.Close() }
func (c *testClient) close()            { _ = c.raw.Close() }

func (c *testClient) write(b []byte) {
	c.t.Helper()
	if _, err := c.raw.Write(b); err != nil {
		c.t.Fatalf("write: %v", err)
	}
}

// readRaw reads one MQTT packet, returning the first fixed-header byte and body.
func (c *testClient) readRaw() (byte, []byte) {
	c.t.Helper()
	_ = c.raw.SetReadDeadline(time.Now().Add(3 * time.Second))
	hdr := make([]byte, 1)
	if _, err := io.ReadFull(c.raw, hdr); err != nil {
		c.t.Fatalf("read header: %v", err)
	}
	// Remaining length.
	var rem int
	mult := 1
	for i := 0; i < 4; i++ {
		b := make([]byte, 1)
		if _, err := io.ReadFull(c.raw, b); err != nil {
			c.t.Fatalf("read remlen: %v", err)
		}
		rem += int(b[0]&0x7f) * mult
		if b[0]&0x80 == 0 {
			break
		}
		mult *= 128
	}
	body := make([]byte, rem)
	if _, err := io.ReadFull(c.raw, body); err != nil {
		c.t.Fatalf("read body: %v", err)
	}
	return hdr[0], body
}

// expectEOF asserts the server has closed the transport.
func (c *testClient) expectEOF() {
	c.t.Helper()
	_ = c.raw.SetReadDeadline(time.Now().Add(3 * time.Second))
	buf := make([]byte, 8)
	n, err := c.raw.Read(buf)
	if err == nil && n > 0 {
		c.t.Fatalf("expected closed connection, got % x", buf[:n])
	}
}

type recvPublish struct {
	first byte
	qos   byte
	dup   bool
	pid   uint16
	topic string
	body  []byte
}

func (c *testClient) recvPublish() recvPublish {
	c.t.Helper()
	first, body := c.readRaw()
	if first>>4 != TypePUBLISH {
		c.t.Fatalf("expected PUBLISH, got type %d (% x)", first>>4, body)
	}
	rp := recvPublish{first: first, qos: (first >> 1) & 3, dup: first&0x08 != 0}
	tl := int(binary.BigEndian.Uint16(body))
	rp.topic = string(body[2 : 2+tl])
	pos := 2 + tl
	if rp.qos > 0 {
		rp.pid = binary.BigEndian.Uint16(body[pos : pos+2])
		pos += 2
	}
	rp.body = append([]byte(nil), body[pos:]...)
	return rp
}

func (c *testClient) recvPUBACK() uint16 {
	first, body := c.readRaw()
	if first>>4 != TypePUBACK {
		c.t.Fatalf("expected PUBACK, got %d", first>>4)
	}
	return binary.BigEndian.Uint16(body)
}

// ---- broker lifecycle ----

func startBroker(t *testing.T) (*Broker, string, string) {
	t.Helper()
	dir := t.TempDir()
	return startBrokerInDir(t, dir)
}

func startBrokerInDir(t *testing.T, dir string) (*Broker, string, string) {
	t.Helper()
	b, err := NewBroker(dir, nil)
	if err != nil {
		t.Fatal(err)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = b.Serve(ln) }()
	t.Cleanup(func() { _ = b.Close() })
	return b, ln.Addr().String(), dir
}

func waitOffline(t *testing.T, b *Broker, id string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		for _, s := range b.Sessions() {
			if s.ClientID == id && !s.Online {
				return
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("session %s never went offline", id)
}

func sessionInfo(b *Broker, id string) SessionInfo {
	for _, s := range b.Sessions() {
		if s.ClientID == id {
			return s
		}
	}
	return SessionInfo{}
}

// waitFor polls cond against the session view until it holds or times out,
// bridging the asynchronous read loop in assertions.
func waitFor(t *testing.T, b *Broker, id string, cond func(SessionInfo) bool, what string) SessionInfo {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for {
		info := sessionInfo(b, id)
		if cond(info) {
			return info
		}
		if time.Now().After(deadline) {
			t.Fatalf("session %s: timed out waiting for %s (last: %+v)", id, what, info)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// ---- tests ----

func TestConnectSubscribeAndSessionPresent(t *testing.T) {
	b, addr, _ := startBroker(t)

	sub := dial(t, addr, "persist1", false, 0)
	codes := sub.subscribe(1, 1, "a/b")
	if !bytes.Equal(codes, []byte{1}) {
		t.Fatalf("granted qos = %v, want [1]", codes)
	}
	sub.close()
	waitOffline(t, b, "persist1")

	pub := dial(t, addr, "pub1", true, 0)
	pub.publish(false, 1, 100, "a/b", []byte("hi"))
	if got := pub.recvPUBACK(); got != 100 {
		t.Fatalf("puback pid=%d", got)
	}
	pub.disconnect()

	// Reconnect: subscription survives, SP=1; the missed QoS 1 message is
	// queued and delivered on resume.
	sub2 := &testClient{t: t, raw: mustDial(t, addr)}
	if sp := sub2.sendConnect("persist1", false, 0, false, 4, "MQTT"); !sp {
		t.Fatal("expected session present=true on reconnect")
	}
	m := sub2.recvPublish()
	if m.dup || m.pid == 0 || m.topic != "a/b" || string(m.body) != "hi" {
		t.Fatalf("unexpected resumed publish: %+v", m)
	}
	sub2.puback(m.pid)
	sub2.disconnect()
}

func TestAckLossRedeliveryWithDUP(t *testing.T) {
	b, addr, _ := startBroker(t)

	sub := dial(t, addr, "ackloss", false, 0)
	sub.subscribe(1, 1, "evt/x")

	pub := dial(t, addr, "pub", true, 0)
	pub.publish(false, 1, 5001, "evt/x", []byte("payload-1"))
	if pid := pub.recvPUBACK(); pid != 5001 {
		t.Fatalf("puback=%d", pid)
	}

	first := sub.recvPublish()
	if first.dup || first.qos != 1 || first.pid == 0 {
		t.Fatalf("first delivery should be DUP=0: %+v", first)
	}
	// Simulate PUBACK loss: the subscriber got the message but never acks,
	// then the connection drops.
	sub.close()
	waitFor(t, b, "ackloss", func(s SessionInfo) bool { return !s.Online && s.InflightCount == 1 }, "offline inflight=1")

	// Reconnect: the broker must redeliver with the SAME packet ID and DUP=1.
	sub2 := &testClient{t: t, raw: mustDial(t, addr)}
	if sp := sub2.sendConnect("ackloss", false, 0, false, 4, "MQTT"); !sp {
		t.Fatal("session present should be true")
	}
	second := sub2.recvPublish()
	if !second.dup {
		t.Fatal("redelivery must set DUP=1")
	}
	if second.pid != first.pid {
		t.Fatalf("redelivery pid=%d, want same as first %d", second.pid, first.pid)
	}
	if string(second.body) != "payload-1" {
		t.Fatalf("payload changed: %q", second.body)
	}
	sub2.puback(second.pid)
	// No more packets should be queued.
	sub2.ping()
	if f, _ := sub2.readRaw(); f != (TypePINGRESP << 4) {
		t.Fatalf("expected PINGRESP, got %02x", f)
	}
	waitFor(t, b, "ackloss", func(s SessionInfo) bool { return s.InflightCount == 0 }, "inflight=0")
	sub2.disconnect()
}

func TestRedeliverySurvivesBrokerRestart(t *testing.T) {
	b, addr, dir := startBroker(t)

	sub := dial(t, addr, "restart1", false, 0)
	sub.subscribe(7, 1, "d/c")
	pub := dial(t, addr, "pub", true, 0)
	pub.publish(false, 1, 900, "d/c", []byte("durable"))
	pub.recvPUBACK()
	first := sub.recvPublish()
	sub.close() // no PUBACK
	waitOffline(t, b, "restart1")
	if err := b.Close(); err != nil {
		t.Fatal(err)
	}

	// Brand-new process: state is loaded from the JSON snapshot.
	b2, addr2, _ := startBrokerInDir(t, dir)
	sub3 := &testClient{t: t, raw: mustDial(t, addr2)}
	if sp := sub3.sendConnect("restart1", false, 0, false, 4, "MQTT"); !sp {
		t.Fatal("SP=true expected after restart")
	}
	m := sub3.recvPublish()
	if !m.dup || m.pid != first.pid || string(m.body) != "durable" {
		t.Fatalf("restart redelivery wrong: %+v (want pid %d)", m, first.pid)
	}
	sub3.puback(m.pid)
	sub3.disconnect()
	waitFor(t, b2, "restart1", func(s SessionInfo) bool { return s.InflightCount == 0 }, "inflight=0")
}

func TestSamePacketIDDifferentSessionsIndependent(t *testing.T) {
	b, addr, _ := startBroker(t)

	a := dial(t, addr, "sessA", false, 0)
	a.subscribe(1, 1, "shared")
	bb := dial(t, addr, "sessB", false, 0)
	bb.subscribe(1, 1, "shared")

	// One QoS 1 publication: each session gets its OWN packet ID namespace,
	// so both sessions legitimately receive packet ID 1.
	if n, err := b.Publish("shared", 1, []byte("m")); err != nil || n != 2 {
		t.Fatalf("publish matched=%d err=%v, want 2", n, err)
	}
	ma := a.recvPublish()
	mb := bb.recvPublish()
	if ma.pid != 1 || mb.pid != 1 {
		t.Fatalf("each session should allocate pid 1 independently: a=%d b=%d", ma.pid, mb.pid)
	}
	// A acks pid 1; B does not. B's pid-1 inflight state must be unaffected.
	a.puback(1)
	bb.close()
	waitFor(t, b, "sessB", func(s SessionInfo) bool { return !s.Online && s.InflightCount == 1 }, "sessB offline inflight=1")
	waitFor(t, b, "sessA", func(s SessionInfo) bool { return s.InflightCount == 0 }, "sessA inflight=0")
	// B reconnects and still receives ITS pid 1 redelivery.
	b2 := &testClient{t: t, raw: mustDial(t, addr)}
	b2.sendConnect("sessB", false, 0, false, 4, "MQTT")
	mb2 := b2.recvPublish()
	if !mb2.dup || mb2.pid != 1 {
		t.Fatalf("sessB redelivery: %+v", mb2)
	}
	b2.puback(1)
	a.disconnect()
	b2.disconnect()
}

func TestInboundQoS1DuplicateDelivery(t *testing.T) {
	_, addr, _ := startBroker(t)
	sub := dial(t, addr, "subd", false, 0)
	sub.subscribe(1, 1, "dup/in")
	pub := dial(t, addr, "pubd", true, 0)

	// Original send and a DUP=1 resend (publisher never got the first PUBACK,
	// in this test we just issue both).
	pub.publish(false, 1, 333, "dup/in", []byte("x"))
	if pid := pub.recvPUBACK(); pid != 333 {
		t.Fatalf("puback %d", pid)
	}
	pub.publish(true, 1, 333, "dup/in", []byte("x"))
	if pid := pub.recvPUBACK(); pid != 333 {
		t.Fatalf("duplicate puback %d", pid)
	}
	// At-least-once: the subscriber observes both copies and must dedupe.
	m1 := sub.recvPublish()
	m2 := sub.recvPublish()
	if string(m1.body) != "x" || string(m2.body) != "x" {
		t.Fatal("payload mismatch")
	}
	pub.disconnect()
	sub.disconnect()
}

func TestUnsupportedPacketsCloseConnection(t *testing.T) {
	_, addr, _ := startBroker(t)

	// QoS 2 PUBLISH closes the connection.
	c := dial(t, addr, "q2", true, 0)
	c.publish(false, 2, 1, "t", []byte("x"))
	c.expectEOF()

	// PUBREL (QoS 2 handshake packet) closes it too.
	c = dial(t, addr, "q2b", true, 0)
	c.write([]byte{0x62, 0x02, 0x00, 0x01})
	c.expectEOF()

	// Will flag: explicit CONNACK rc=5 then close.
	raw := mustDial(t, addr)
	c = &testClient{t: t, raw: raw}
	if rc := c.sendWillConnect("willc"); rc != rcNotAuthorized {
		t.Fatalf("will connect rc=%d, want %d", rc, rcNotAuthorized)
	}
	c.expectEOF()

	// MQTT 3.1 protocol name: rc=1 then close.
	raw = mustDial(t, addr)
	c = &testClient{t: t, raw: raw}
	c.write(connectPacket("oldproto", true, 0, false, 3, "MQIsdp"))
	_, body := c.readRaw()
	if body[1] != rcBadProto {
		t.Fatalf("old protocol rc=%d, want %d", body[1], rcBadProto)
	}
	c.expectEOF()
}

func TestWildcardQoS0AndUnsubscribe(t *testing.T) {
	_, addr, _ := startBroker(t)
	sub := dial(t, addr, "wild", false, 0)
	if codes := sub.subscribe(1, 1, "a/+"); len(codes) != 1 || codes[0] != 1 {
		t.Fatalf("suback codes %v", codes)
	}
	pub := dial(t, addr, "pubw", true, 0)
	pub.publish(false, 0, 0, "a/b", []byte("fire"))
	m := sub.recvPublish()
	if m.qos != 0 || m.pid != 0 || string(m.body) != "fire" {
		t.Fatalf("qos0 delivery wrong: %+v", m)
	}

	sub.unsubscribe(2, "a/+")
	pub.publish(false, 1, 11, "a/b", []byte("nobody"))
	pub.recvPUBACK() // broker still accepts; zero subscribers matched
	pub.ping()
	if f, _ := pub.readRaw(); f != TypePINGRESP<<4 {
		t.Fatalf("expected pingresp, got %02x", f)
	}
	pub.disconnect()
	sub.disconnect()
}

func TestOfflineQueueFreshDelivery(t *testing.T) {
	b, addr, _ := startBroker(t)
	sub := dial(t, addr, "off", false, 0)
	sub.subscribe(1, 1, "q/+")
	sub.disconnect()
	waitOffline(t, b, "off")

	if _, err := b.Publish("q/1", 1, []byte("one")); err != nil {
		t.Fatal(err)
	}
	if _, err := b.Publish("q/2", 1, []byte("two")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, b, "off", func(s SessionInfo) bool { return s.QueuedCount == 2 }, "queued=2")

	back := dial(t, addr, "off", false, 0) // fresh connect, SP false
	m1 := back.recvPublish()
	m2 := back.recvPublish()
	if m1.dup || m2.dup || m1.pid != 1 || m2.pid != 2 {
		t.Fatalf("queued messages must be fresh DUP=0 with new pids: %+v %+v", m1, m2)
	}
	if string(m1.body) != "one" || string(m2.body) != "two" {
		t.Fatal("order/content mismatch")
	}
	back.puback(1)
	back.puback(2)
	back.disconnect()
}

func TestKeepAliveTimeout(t *testing.T) {
	_, addr, _ := startBroker(t)
	raw := mustDial(t, addr)
	c := &testClient{t: t, raw: raw}
	c.sendConnect("ka", true, 1, false, 4, "MQTT") // 1s keepalive -> 1.5s timeout
	start := time.Now()
	c.expectEOF()
	if d := time.Since(start); d < 1400*time.Millisecond || d > 4*time.Second {
		t.Fatalf("keepalive disconnect after %v, want ~1.5s", d)
	}
}

func TestTakeoverKicksOldConnection(t *testing.T) {
	_, addr, _ := startBroker(t)
	first := dial(t, addr, "take", false, 0)
	first.subscribe(1, 1, "t")

	// Second connection with the same Client ID takes over (SP=1) and the
	// server closes the first transport (§3.1.4-2).
	second := &testClient{t: t, raw: mustDial(t, addr)}
	if sp := second.sendConnect("take", false, 0, false, 4, "MQTT"); !sp {
		t.Fatal("expected SP=true on takeover")
	}
	first.expectEOF()
	second.disconnect()
}

func TestStateFileIsJSON(t *testing.T) {
	b, addr, dir := startBroker(t)
	sub := dial(t, addr, "json1", false, 0)
	sub.subscribe(1, 1, "j/x")
	pub := dial(t, addr, "jp", true, 0)
	pub.publish(false, 1, 5, "j/x", []byte("z"))
	pub.recvPUBACK()
	sub.recvPublish()
	sub.close()
	waitOffline(t, b, "json1")

	data, err := os.ReadFile(filepath.Join(dir, "mqttstate.json"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(data, []byte(`"j/x"`)) || !bytes.Contains(data, []byte(`"json1"`)) {
		t.Fatalf("state file missing expected content:\n%s", data)
	}
}

func mustDial(t *testing.T, addr string) net.Conn {
	t.Helper()
	raw, err := net.DialTimeout("tcp", addr, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}
