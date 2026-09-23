package broker_test

import (
	"bufio"
	"net"
	"testing"
	"time"

	"mqttsub/internal/mqtt"
)

// connectRaw sends a fully specified CONNECT (used for will scenarios).
func connectRaw(t *testing.T, addr string, cp *mqtt.ConnectPacket) (net.Conn, *mqtt.Frame) {
	t.Helper()
	nc, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := nc.Write(mqtt.EncodeConnect(cp)); err != nil {
		t.Fatal(err)
	}
	nc.SetReadDeadline(time.Now().Add(2 * time.Second))
	fr, err := mqtt.ReadFrame(bufio.NewReader(nc))
	if err != nil {
		t.Fatal(err)
	}
	return nc, fr
}

// A client that disconnects abnormally (no DISCONNECT) triggers its will;
// a clean DISCONNECT must not.
func TestWillMessage(t *testing.T) {
	h := startBroker(t, 0)

	monitor := dialMQTT(t, h.addr(), "will-monitor", true)
	monitor.subscribe("status/+", 1, 1)

	// Abnormal case: will should fire.
	w1, _ := connectRaw(t, h.addr(), &mqtt.ConnectPacket{
		ClientID: "dying-1", CleanSession: true,
		WillFlag: true, WillTopic: "status/dying-1", WillMsg: []byte("offline"), WillQoS: 1,
	})
	w1.Close() // hard close, no DISCONNECT
	p := monitor.recvPublish()
	if p.Topic != "status/dying-1" || string(p.Payload) != "offline" {
		t.Fatalf("bad will message: %+v", p)
	}
	monitor.send(mqtt.EncodePuback(p.PacketID))

	// Clean case: will must NOT fire.
	w2nc, connack := connectRaw(t, h.addr(), &mqtt.ConnectPacket{
		ClientID: "dying-2", CleanSession: true,
		WillFlag: true, WillTopic: "status/dying-2", WillMsg: []byte("offline"), WillQoS: 1,
	})
	if connack.Body[1] != 0 {
		t.Fatalf("connect rejected %d", connack.Body[1])
	}
	w2nc.Write(mqtt.PacketDisconnect)
	w2nc.Close()

	if extra := monitor.recvMaybe(300 * time.Millisecond); extra != nil {
		t.Fatalf("will fired after clean DISCONNECT: %v", extra)
	}
	monitor.closeClean()
}

// Connecting a second time with the same client id takes over the session
// and closes the old connection without publishing its will.
func TestTakeoverClosesOldWithoutWill(t *testing.T) {
	h := startBroker(t, 0)
	monitor := dialMQTT(t, h.addr(), "takeover-monitor", true)
	monitor.subscribe("tw/+", 1, 1)

	old, _ := connectRaw(t, h.addr(), &mqtt.ConnectPacket{
		ClientID: "tw-client", CleanSession: false,
		WillFlag: true, WillTopic: "tw/client", WillMsg: []byte("bye"), WillQoS: 1,
	})

	// New connection with the same id kicks the old one.
	newConn := dialMQTT(t, h.addr(), "tw-client", false)
	if !newConn.sessionPresent {
		t.Fatal("takeover of durable session should report sessionPresent")
	}

	// Old socket must be closed by the server.
	old.SetReadDeadline(time.Now().Add(2 * time.Second))
	buf := make([]byte, 4)
	if _, err := old.Read(buf); err == nil {
		t.Fatal("old connection should have been closed on takeover")
	}
	old.Close()

	// Will must not fire on takeover.
	if extra := monitor.recvMaybe(300 * time.Millisecond); extra != nil {
		t.Fatalf("will fired on takeover: %v", extra)
	}
	newConn.closeClean()
	monitor.closeClean()
}

// While a connected subscriber withholds PUBACK, the live retry timer
// resends the message with DUP=1 without any reconnect.
func TestLiveRetryDup(t *testing.T) {
	h := startBroker(t, 250*time.Millisecond)
	s := dialMQTT(t, h.addr(), "holdout", false)
	s.subscribe("rt", 1, 1)

	if n, _ := h.b.Publish("rt", 1, []byte("z")); n != 1 {
		t.Fatalf("matched=%d", n)
	}
	first := s.recvPublish()
	if first.Dup {
		t.Fatal("first delivery DUP must be false")
	}
	// Withhold PUBACK; expect a DUP=1 retry within ~2 intervals.
	second := s.recvPublish()
	if !second.Dup || second.PacketID != first.PacketID {
		t.Fatalf("expected DUP retry with same pid, got %+v", second)
	}
	// Ack now and ensure it stays settled (no third copy).
	s.send(mqtt.EncodePuback(second.PacketID))
	if extra := s.recvMaybe(500 * time.Millisecond); extra != nil {
		t.Fatalf("received a copy after PUBACK: %v", extra)
	}
	s.closeClean()
}

// A SUBSCRIBE requesting QoS2 is granted at most QoS1 (failure would be
// 0x80; downgrade is 1).
func TestSubscribeQoS2Downgrade(t *testing.T) {
	h := startBroker(t, 0)
	nc, err := net.Dial("tcp", h.addr())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	nc.Write(mqtt.EncodeConnect(&mqtt.ConnectPacket{ClientID: "q2sub", CleanSession: true}))
	nc.SetReadDeadline(time.Now().Add(2 * time.Second))
	br := bufio.NewReader(nc)
	if _, err := mqtt.ReadFrame(br); err != nil {
		t.Fatal(err)
	}
	nc.Write(mqtt.EncodeSubscribe(1, []mqtt.Subscription{{Filter: "d", MaxQoS: 2}}))
	fr, err := mqtt.ReadFrame(br)
	if err != nil {
		t.Fatal(err)
	}
	if fr.Type() != mqtt.TypeSUBACK || fr.Body[2] != 1 {
		t.Fatalf("expected granted QoS1, got type=%d body=%v", fr.Type(), fr.Body)
	}
}

// PINGREQ is answered with PINGRESP.
func TestPing(t *testing.T) {
	h := startBroker(t, 0)
	s := dialMQTT(t, h.addr(), "pinger", true)
	s.send(mqtt.PacketPingReq)
	fr := s.recv()
	if fr.Type() != mqtt.TypePINGRESP {
		t.Fatalf("want PINGRESP got %d", fr.Type())
	}
	s.closeClean()
}
