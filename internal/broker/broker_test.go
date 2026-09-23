package broker_test

import (
	"bufio"
	"net"
	"path/filepath"
	"testing"
	"time"

	"mqttsub/internal/broker"
	"mqttsub/internal/mqtt"
)

// harness starts a broker bound to random ports with persistence in t.TempDir.
type harness struct {
	t   *testing.T
	b   *broker.Broker
	ln  net.Listener
	dir string
}

func startBroker(t *testing.T, retry time.Duration) *harness {
	t.Helper()
	dir := t.TempDir()
	b, err := broker.New(broker.Config{
		StorePath:     filepath.Join(dir, "sessions.json"),
		RetryInterval: retry,
	})
	if err != nil {
		t.Fatalf("broker new: %v", err)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() {
		_ = ln.Close()
		_ = b.Shutdown()
	})
	go func() {
		for {
			nc, err := ln.Accept()
			if err != nil {
				return
			}
			go b.Serve(nc)
		}
	}()
	return &harness{t: t, b: b, ln: ln, dir: dir}
}

func (h *harness) addr() string { return h.ln.Addr().String() }

// mqttClient is a raw test client with explicit control over every byte.
type mqttClient struct {
	t              *testing.T
	nc             net.Conn
	br             *bufio.Reader
	sessionPresent bool
}

func dialMQTT(t *testing.T, addr, id string, clean bool) *mqttClient {
	t.Helper()
	return dialMQTTKeepAlive(t, addr, id, clean, 0)
}

func dialMQTTKeepAlive(t *testing.T, addr, id string, clean bool, ka uint16) *mqttClient {
	t.Helper()
	nc, err := net.DialTimeout("tcp", addr, 2*time.Second)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	c := &mqttClient{t: t, nc: nc, br: bufio.NewReader(nc)}
	c.send(mqtt.EncodeConnect(&mqtt.ConnectPacket{
		ClientID: id, CleanSession: clean, KeepAlive: ka,
	}))
	fr := c.recv()
	if fr.Type() != mqtt.TypeCONNACK || len(fr.Body) != 2 {
		t.Fatalf("bad connack: type=%d body=%v", fr.Type(), fr.Body)
	}
	if fr.Body[1] != 0 {
		nc.Close()
		t.Fatalf("connect rejected code=%d", fr.Body[1])
	}
	c.sessionPresent = fr.Body[0] == 1
	return c
}

func (c *mqttClient) send(p []byte) {
	c.t.Helper()
	if _, err := c.nc.Write(p); err != nil {
		c.t.Fatalf("write: %v", err)
	}
}

func (c *mqttClient) recv() *mqtt.Frame {
	c.t.Helper()
	_ = c.nc.SetReadDeadline(time.Now().Add(3 * time.Second))
	fr, err := mqtt.ReadFrame(c.br)
	if err != nil {
		c.t.Fatalf("read frame: %v", err)
	}
	return fr
}

// recvMaybe returns a frame or nil on timeout/EOF, without failing the test.
func (c *mqttClient) recvMaybe(d time.Duration) *mqtt.Frame {
	_ = c.nc.SetReadDeadline(time.Now().Add(d))
	fr, err := mqtt.ReadFrame(c.br)
	if err != nil {
		return nil
	}
	return fr
}

func (c *mqttClient) subscribe(filter string, qos byte, pid uint16) {
	c.send(mqtt.EncodeSubscribe(pid, []mqtt.Subscription{{Filter: filter, MaxQoS: qos}}))
	fr := c.recv()
	if fr.Type() != mqtt.TypeSUBACK {
		c.t.Fatalf("want SUBACK got %d", fr.Type())
	}
}

func (c *mqttClient) recvPublish() *mqtt.PublishPacket {
	for {
		fr := c.recv()
		if fr.Type() == mqtt.TypePINGRESP {
			continue
		}
		if fr.Type() != mqtt.TypePUBLISH {
			c.t.Fatalf("want PUBLISH got type %d", fr.Type())
		}
		p, err := mqtt.DecodePublish(fr.Flags(), fr.Body)
		if err != nil {
			c.t.Fatalf("decode publish: %v", err)
		}
		return p
	}
}

func (c *mqttClient) closeClean() {
	c.send(mqtt.PacketDisconnect)
	// Wait for the server to close its side so cleanup (session deletion)
	// is guaranteed to have happened before the test proceeds.
	_ = c.nc.SetReadDeadline(time.Now().Add(2 * time.Second))
	_, _ = c.nc.Read(make([]byte, 1))
	_ = c.nc.Close()
}

func (c *mqttClient) crash() { _ = c.nc.Close() }

// ---- Tests ---------------------------------------------------------------

// The headline acceptance case: PUBACK lost, subscriber crashes, reconnects
// durable, and receives the message again with DUP=1 and the same pid.
func TestAckLossReconnectDup(t *testing.T) {
	h := startBroker(t, 0) // no live retry; redelivery must happen via reconnect

	sub := dialMQTT(t, h.addr(), "durable-sub", false)
	sub.subscribe("evt/x", 1, 1)

	if n, err := h.b.Publish("evt/x", 1, []byte("pay-1")); err != nil || n != 1 {
		t.Fatalf("publish matched=%d err=%v", n, err)
	}
	first := sub.recvPublish()
	if first.Dup || first.PacketID == 0 || string(first.Payload) != "pay-1" {
		t.Fatalf("bad first delivery: %+v", first)
	}
	// Withhold PUBACK, then crash.
	sub.crash()
	waitOffline(t, h.b, "durable-sub")

	// Reconnect durable: sessionPresent must be true.
	sub2 := dialMQTT(t, h.addr(), "durable-sub", false)
	if !sub2.sessionPresent {
		t.Fatal("expected sessionPresent=true on durable resume")
	}
	redelivered := sub2.recvPublish()
	if !redelivered.Dup {
		t.Fatal("redelivered message must have DUP=1")
	}
	if redelivered.PacketID != first.PacketID {
		t.Fatalf("packet id must be stable across redelivery: %d != %d",
			redelivered.PacketID, first.PacketID)
	}
	if string(redelivered.Payload) != "pay-1" {
		t.Fatalf("payload mismatch: %q", redelivered.Payload)
	}
	// Acknowledge now; inflight must clear.
	sub2.send(mqtt.EncodePuback(redelivered.PacketID))
	waitUntil(t, time.Second, func() bool {
		info, ok := h.b.Session("durable-sub")
		return ok && len(info.Inflight) == 0
	})
	sub2.closeClean()
}

// waitUntil polls cond until it is true or the deadline elapses.
func waitUntil(t *testing.T, d time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("condition not met within " + d.String())
}

// waitOffline blocks until the broker observes clientID as disconnected.
func waitOffline(t *testing.T, b *broker.Broker, clientID string) {
	t.Helper()
	waitUntil(t, time.Second, func() bool {
		info, ok := b.Session(clientID)
		return ok && !info.Online
	})
}

// Two independent sessions receiving the same topic allocate packet ids
// independently: both can legitimately use pid 1 at the same time.
func TestSamePacketIDDifferentSessions(t *testing.T) {
	h := startBroker(t, 0)
	s1 := dialMQTT(t, h.addr(), "sess-one", false)
	s2 := dialMQTT(t, h.addr(), "sess-two", false)
	s1.subscribe("t", 1, 1)
	s2.subscribe("t", 1, 1)

	if n, _ := h.b.Publish("t", 1, []byte("x")); n != 2 {
		t.Fatalf("matched=%d want 2", n)
	}
	p1 := s1.recvPublish()
	p2 := s2.recvPublish()
	if p1.PacketID != 1 || p2.PacketID != 1 {
		t.Fatalf("per-session ids should both start at 1: %d %d", p1.PacketID, p2.PacketID)
	}
	// Acking on one session must not clear the other's inflight.
	s1.send(mqtt.EncodePuback(p1.PacketID))
	waitUntil(t, time.Second, func() bool {
		i1, _ := h.b.Session("sess-one")
		i2, _ := h.b.Session("sess-two")
		return len(i1.Inflight) == 0 && len(i2.Inflight) == 1
	})
	s2.send(mqtt.EncodePuback(p2.PacketID))
	s1.closeClean()
	s2.closeClean()
}

// Messages published while a durable subscriber is offline are queued and
// delivered on reconnect, QoS1, with fresh packet ids and DUP=0.
func TestOfflineDurableQueue(t *testing.T) {
	h := startBroker(t, 0)
	s := dialMQTT(t, h.addr(), "offline", false)
	s.subscribe("q/#", 1, 1)
	s.crash() // no DISCONNECT: durable session retained
	waitOffline(t, h.b, "offline")

	if n, _ := h.b.Publish("q/1", 1, []byte("m1")); n != 1 {
		t.Fatalf("offline queue matched=%d", n)
	}
	if n, _ := h.b.Publish("q/2", 1, []byte("m2")); n != 1 {
		t.Fatalf("offline queue matched=%d", n)
	}

	s2 := dialMQTT(t, h.addr(), "offline", false)
	if !s2.sessionPresent {
		t.Fatal("durable session should be present")
	}
	got1 := s2.recvPublish()
	got2 := s2.recvPublish()
	if string(got1.Payload) != "m1" || string(got2.Payload) != "m2" {
		t.Fatalf("FIFO order broken: %q %q", got1.Payload, got2.Payload)
	}
	if got1.Dup || got2.Dup {
		t.Fatal("first-ever delivery must not be DUP")
	}
	if got1.PacketID == got2.PacketID {
		t.Fatal("distinct messages need distinct packet ids")
	}
	s2.closeClean()
}

// Clean sessions are not retained and receive nothing queued while offline.
func TestCleanSessionNotRetained(t *testing.T) {
	h := startBroker(t, 0)
	s := dialMQTT(t, h.addr(), "temp", true)
	s.subscribe("c", 1, 1)
	s.closeClean()

	if n, _ := h.b.Publish("c", 1, []byte("lost")); n != 0 {
		t.Fatalf("message matched a deleted clean session: %d", n)
	}
	if _, ok := h.b.Session("temp"); ok {
		t.Fatal("clean session should be gone after disconnect")
	}
}

// A redelivered INBOUND QoS1 PUBLISH (DUP=1, pid already seen) must get a
// repeat PUBACK but must not be re-distributed to subscribers.
func TestInboundDuplicateNotRefanned(t *testing.T) {
	h := startBroker(t, 0)
	// Publisher and a subscriber.
	sub := dialMQTT(t, h.addr(), "fan-sub", false)
	sub.subscribe("in", 1, 1)

	pub := dialMQTT(t, h.addr(), "fan-pub", true)
	mk := func(dup bool) []byte {
		return mqtt.EncodePublish(&mqtt.PublishPacket{
			Dup: dup, QoS: 1, Topic: "in", Payload: []byte("same"), PacketID: 42,
		})
	}
	pub.send(mk(false))
	ack := pub.recv()
	if ack.Type() != mqtt.TypePUBACK {
		t.Fatalf("want PUBACK got %d", ack.Type())
	}
	first := sub.recvPublish()
	sub.send(mqtt.EncodePuback(first.PacketID))

	// Publisher never saw the PUBACK (pretend) and resends DUP=1.
	pub.send(mk(true))
	ack2 := pub.recv()
	if ack2.Type() != mqtt.TypePUBACK {
		t.Fatalf("duplicate must still get PUBACK, got %d", ack2.Type())
	}
	// Subscriber must NOT receive a second copy.
	if extra := sub.recvMaybe(300 * time.Millisecond); extra != nil {
		t.Fatalf("duplicate inbound was re-distributed: %v", extra)
	}
	pub.closeClean()
	sub.closeClean()
}

// After receiving PUBACK, a publisher may reuse the same packet id for a
// brand-new message with DUP=0 (MQTT-2.3.1-3). It must be delivered, not
// mistaken for a redelivery.
func TestInboundPacketIDReuseAfterAck(t *testing.T) {
	h := startBroker(t, 0)
	sub := dialMQTT(t, h.addr(), "reuse-sub", false)
	sub.subscribe("rin", 1, 1)
	pub := dialMQTT(t, h.addr(), "reuse-pub", true)

	send := func(payload string, dup bool) {
		pub.send(mqtt.EncodePublish(&mqtt.PublishPacket{
			Dup: dup, QoS: 1, Topic: "rin", Payload: []byte(payload), PacketID: 7,
		}))
		if ack := pub.recv(); ack.Type() != mqtt.TypePUBACK {
			t.Fatalf("want PUBACK got %d", ack.Type())
		}
	}

	send("first", false)
	first := sub.recvPublish()
	sub.send(mqtt.EncodePuback(first.PacketID))

	send("second", false) // same pid 7, DUP=0 -> new message
	second := sub.recvPublish()
	if string(second.Payload) != "second" {
		t.Fatalf("reused-id new message not delivered: got %q", second.Payload)
	}
	if second.Dup {
		t.Fatal("DUP=0 reuse must be delivered as fresh (DUP=0)")
	}
	sub.send(mqtt.EncodePuback(second.PacketID))

	// And a true redelivery (DUP=1) is still suppressed.
	pub.send(mqtt.EncodePublish(&mqtt.PublishPacket{
		Dup: true, QoS: 1, Topic: "rin", Payload: []byte("second"), PacketID: 7,
	}))
	if ack := pub.recv(); ack.Type() != mqtt.TypePUBACK {
		t.Fatalf("redelivery must still be PUBACKed, got %d", ack.Type())
	}
	if extra := sub.recvMaybe(300 * time.Millisecond); extra != nil {
		t.Fatalf("DUP=1 redelivery must not re-fan-out: %v", extra)
	}
	pub.closeClean()
	sub.closeClean()
}

// QoS 2 packets and reserved types close the connection with no response.
func TestUnsupportedPacketsClose(t *testing.T) {
	h := startBroker(t, 0)
	cases := map[string][]byte{
		"PUBREC":  mqtt.EncodeAck(mqtt.TypePUBREC, 1),
		"PUBREL":  mqtt.EncodeAck(mqtt.TypePUBREL, 1),
		"PUBCOMP": mqtt.EncodeAck(mqtt.TypePUBCOMP, 1),
	}
	for name, pkt := range cases {
		nc, err := net.Dial("tcp", h.addr())
		if err != nil {
			t.Fatalf("%s dial: %v", name, err)
		}
		br := bufio.NewReader(nc)
		// CONNECT first so the connection is fully established.
		nc.Write(mqtt.EncodeConnect(&mqtt.ConnectPacket{ClientID: "bad-" + name, CleanSession: true}))
		if fr, err := mqtt.ReadFrame(br); err != nil || fr.Type() != mqtt.TypeCONNACK {
			t.Fatalf("%s connack: %v", name, err)
		}
		nc.Write(pkt)
		// Server must close; any subsequent read yields EOF/reset.
		nc.SetReadDeadline(time.Now().Add(2 * time.Second))
		buf := make([]byte, 16)
		if _, err := nc.Read(buf); err == nil {
			t.Fatalf("%s: connection not closed after unsupported packet", name)
		}
		nc.Close()
	}
}

// A non-CONNECT first packet must close the connection.
func TestFirstPacketNotConnect(t *testing.T) {
	h := startBroker(t, 0)
	nc, err := net.Dial("tcp", h.addr())
	if err != nil {
		t.Fatal(err)
	}
	nc.Write(mqtt.PacketPingReq)
	nc.SetReadDeadline(time.Now().Add(2 * time.Second))
	buf := make([]byte, 8)
	if _, err := nc.Read(buf); err == nil {
		t.Fatal("expected close when first packet is PINGREQ")
	}
	nc.Close()
}

// Wildcards and QoS downgrade: subscription at QoS0 receives published
// QoS1 downgraded to QoS0.
func TestQoSDowngrade(t *testing.T) {
	h := startBroker(t, 0)
	s := dialMQTT(t, h.addr(), "low", false)
	s.subscribe("w/+", 0, 1)
	if n, _ := h.b.Publish("w/x", 1, []byte("d")); n != 1 {
		t.Fatalf("matched=%d", n)
	}
	p := s.recvPublish()
	if p.QoS != 0 {
		t.Fatalf("expected downgrade to QoS0, got QoS%d", p.QoS)
	}
	s.closeClean()
}

// Persistence across a full broker restart: unacked inflight and offline
// queued messages survive, and reconnect yields DUP=1.
func TestRestartPersistence(t *testing.T) {
	h := startBroker(t, 0)
	s := dialMQTT(t, h.addr(), "restart-me", false)
	s.subscribe("p", 1, 1)
	if n, _ := h.b.Publish("p", 1, []byte("v1")); n != 1 {
		t.Fatalf("matched=%d", n)
	}
	first := s.recvPublish()
	s.crash() // unacked
	// Shutdown performs a final synchronous flush, so no sleep needed.
	_ = first
	if err := h.b.Shutdown(); err != nil {
		t.Fatalf("shutdown: %v", err)
	}

	// Reopen the same store with a new broker on a fresh listener.
	b2, err := broker.New(broker.Config{StorePath: h.dir + "/sessions.json"})
	if err != nil {
		t.Fatalf("reload: %v", err)
	}
	ln2, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln2.Close()
	defer b2.Shutdown()
	go func() {
		for {
			nc, err := ln2.Accept()
			if err != nil {
				return
			}
			go b2.Serve(nc)
		}
	}()

	nc, err := net.Dial("tcp", ln2.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	br := bufio.NewReader(nc)
	nc.Write(mqtt.EncodeConnect(&mqtt.ConnectPacket{ClientID: "restart-me", CleanSession: false}))
	fr, _ := mqtt.ReadFrame(br)
	if fr.Type() != mqtt.TypeCONNACK || fr.Body[0] != 1 {
		t.Fatalf("sessionPresent not restored: %v", fr.Body)
	}
	pf, err := mqtt.ReadFrame(br)
	if err != nil || pf.Type() != mqtt.TypePUBLISH {
		t.Fatalf("expected redelivered PUBLISH after restart: %v", err)
	}
	p, _ := mqtt.DecodePublish(pf.Flags(), pf.Body)
	if !p.Dup || p.PacketID != first.PacketID || string(p.Payload) != "v1" {
		t.Fatalf("bad redelivery after restart: %+v", p)
	}
}
