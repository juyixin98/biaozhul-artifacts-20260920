package transport_test

import (
	"testing"
	"time"

	"udpreliable/internal/clock"
	"udpreliable/internal/proto"
	"udpreliable/internal/transport"
)

func memEndpoints() (sender, receiver transport.Endpoint) {
	ab := make(chan []byte, 64)
	ba := make(chan []byte, 64)
	done := make(chan struct{})
	sender = transport.Endpoint{
		Send:    func(b []byte) error { ab <- b; return nil },
		Packets: ba,
		Done:    done,
	}
	receiver = transport.Endpoint{
		Send:    func(b []byte) error { ba <- b; return nil },
		Packets: ab,
		Done:    done,
	}
	return sender, receiver
}

func mustRecv(t *testing.T, ep transport.Endpoint) proto.Packet {
	t.Helper()
	select {
	case b := <-ep.Packets:
		p, err := proto.Unmarshal(b)
		if err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		return p
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for packet")
		return proto.Packet{}
	}
}

// TestSenderRetransmitsAfterTimeout drives the sender with a manual clock:
// with no ACKs arriving, every in-flight packet must be retransmitted once
// the RTO elapses.
func TestSenderRetransmitsAfterTimeout(t *testing.T) {
	clk := clock.NewManual(time.Now())
	senderEP, receiverEP := memEndpoints()

	data := make([]byte, 4*512)
	for i := range data {
		data[i] = byte(i)
	}
	cfg := transport.Config{
		Gen:    0xC0FFEE,
		Window: 4,
		Chunk:  512,
		RTO:    time.Second,
		Clock:  clk,
	}

	type result struct {
		st  transport.SendStats
		err error
	}
	resCh := make(chan result, 1)
	go func() {
		st, _, err := transport.RunSender(senderEP, cfg, data)
		resCh <- result{st, err}
	}()

	// First transmission of the whole window.
	var first []uint32
	for i := 0; i < 4; i++ {
		p := mustRecv(t, receiverEP)
		if p.Type != proto.TypeData {
			t.Fatalf("packet %d: type %d, want Data", i, p.Type)
		}
		first = append(first, p.Seq)
	}

	// No ACKs; let the RTO elapse. The whole window must be retransmitted.
	clk.Advance(2 * time.Second)
	for i := 0; i < 4; i++ {
		p := mustRecv(t, receiverEP)
		if p.Type != proto.TypeData || p.Seq != first[i] {
			t.Fatalf("retransmit %d: got type=%d seq=%d, want Data seq=%d", i, p.Type, p.Seq, first[i])
		}
	}

	// ACK everything; expect the FIN handshake.
	ack := proto.Packet{Gen: cfg.Gen, Type: proto.TypeAck, Seq: cfg.InitialSeq + 4}
	if err := receiverEP.Send(ack.Marshal()); err != nil {
		t.Fatal(err)
	}
	fin := mustRecv(t, receiverEP)
	if fin.Type != proto.TypeFin {
		t.Fatalf("after full ACK got type %d, want Fin", fin.Type)
	}
	if len(fin.Payload) != 32 {
		t.Fatalf("FIN payload = %d bytes, want 32-byte SHA-256", len(fin.Payload))
	}
	done := proto.Packet{Gen: cfg.Gen, Type: proto.TypeDone, Flags: proto.FlagOK}
	if err := receiverEP.Send(done.Marshal()); err != nil {
		t.Fatal(err)
	}
	// Sender answers Done with DoneAck (sent twice).
	for i := 0; i < 2; i++ {
		p := mustRecv(t, receiverEP)
		if p.Type != proto.TypeDoneAck {
			t.Fatalf("final packet %d: type %d, want DoneAck", i, p.Type)
		}
	}

	select {
	case r := <-resCh:
		if r.err != nil {
			t.Fatalf("sender: %v", r.err)
		}
		if r.st.Retransmits != 4 {
			t.Fatalf("retransmits = %d, want 4", r.st.Retransmits)
		}
		if r.st.MaxInFlight != 4 {
			t.Fatalf("maxInFlight = %d, want 4", r.st.MaxInFlight)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("sender did not finish")
	}
}

// TestReceiverDropsStaleGeneration checks that packets from another
// connection generation are ignored and counted.
func TestReceiverDropsStaleGeneration(t *testing.T) {
	senderEP, receiverEP := memEndpoints()
	cfg := transport.Config{Gen: 100, Window: 4, Chunk: 512, RTO: 50 * time.Millisecond}

	resCh := make(chan transport.RecvStats, 1)
	go func() {
		st, _, _ := transport.RunReceiver(receiverEP, cfg)
		resCh <- st
	}()

	// Stale packet: wrong generation, would fill chunk 0 if accepted.
	stale := proto.Packet{Gen: 999, Type: proto.TypeData, Seq: 0, Payload: []byte("stale")}
	if err := senderEP.Send(stale.Marshal()); err != nil {
		t.Fatal(err)
	}
	// Legitimate FIN for an empty transfer completes the receiver; the
	// payload is the SHA-256 of the empty file so the receiver reports OK.
	fin := proto.Packet{Gen: 100, Type: proto.TypeFin, Seq: 0, Payload: emptySHA256()}
	if err := senderEP.Send(fin.Marshal()); err != nil {
		t.Fatal(err)
	}
	done := mustRecv(t, senderEP)
	if done.Type != proto.TypeDone {
		t.Fatalf("got type %d, want Done", done.Type)
	}
	if done.Flags&proto.FlagOK == 0 {
		t.Fatal("empty-file hash should have matched")
	}
	if err := senderEP.Send(proto.Packet{Gen: 100, Type: proto.TypeDoneAck}.Marshal()); err != nil {
		t.Fatal(err)
	}

	select {
	case st := <-resCh:
		if st.StaleDrop != 1 {
			t.Fatalf("staleDrop = %d, want 1", st.StaleDrop)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("receiver did not finish")
	}
}

func emptySHA256() []byte {
	// SHA-256 of the empty string, precomputed.
	return []byte{
		0xe3, 0xb0, 0xc4, 0x42, 0x98, 0xfc, 0x1c, 0x14,
		0x9a, 0xfb, 0xf4, 0xc8, 0x99, 0x6f, 0xb9, 0x24,
		0x27, 0xae, 0x41, 0xe4, 0x64, 0x9b, 0x93, 0x4c,
		0xa4, 0x95, 0x99, 0x1b, 0x78, 0x52, 0xb8, 0x55,
	}
}
