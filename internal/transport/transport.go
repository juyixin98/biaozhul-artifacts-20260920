// Package transport implements the reliable file-transfer protocol:
// a sliding-window sender with cumulative ACKs and timeout retransmission,
// and a receiver with a bounded reorder buffer and end-to-end SHA-256
// verification.
package transport

import (
	"bytes"
	"crypto/sha256"
	"errors"
	"fmt"
	"time"

	"udpreliable/internal/clock"
	"udpreliable/internal/proto"
)

// Endpoint is one end of a packet link. Send transmits one datagram,
// Packets yields inbound datagrams, and Done is closed by the caller to
// shut the endpoint down (both peers unblock with ErrLinkClosed).
type Endpoint struct {
	Send    func([]byte) error
	Packets <-chan []byte
	Done    <-chan struct{}
}

// Config tunes one transfer. The same Config must be given to sender and
// receiver (they are the two ends of one connection).
type Config struct {
	Gen         uint32        // connection generation; mismatched packets are dropped
	Window      int           // sliding-window size in packets (bounds in-flight memory)
	Chunk       int           // payload bytes per Data packet
	RTO         time.Duration // retransmission timeout
	InitialSeq  uint32        // first sequence number (may be near wrap-around)
	MaxFinTries int           // bound on FIN retransmissions (default 512)
	Clock       clock.Clock   // injectable clock (default: real)
}

func (c Config) clock() clock.Clock {
	if c.Clock == nil {
		return clock.Real{}
	}
	return c.Clock
}

// ErrLinkClosed is returned when the endpoint's Done channel closes.
var ErrLinkClosed = errors.New("link closed")

// ErrHashMismatch is returned by the sender when the receiver reports that
// its reassembled file hash does not match the FIN hash.
var ErrHashMismatch = errors.New("receiver hash mismatch")

// SendStats reports sender-side counters. MaxInFlight is the high-water
// mark of unacknowledged packets and is always <= Config.Window.
type SendStats struct {
	DataSent    int // Data packets transmitted, including retransmissions
	Retransmits int // subset of DataSent sent after a timeout
	FinSent     int
	AcksRecv    int
	StaleDrop   int // packets dropped due to a foreign generation
	MaxInFlight int // high-water mark of unacknowledged packets
}

// RecvStats reports receiver-side counters. MaxBuf is the high-water mark
// of buffered out-of-order packets and is always <= Config.Window.
type RecvStats struct {
	DataRecv  int
	DupRecv   int
	AcksSent  int
	FinRecv   int
	DoneSent  int
	StaleDrop int // packets dropped due to a foreign generation
	MaxBuf    int // high-water mark of the reorder buffer
}

// RunSender transmits data over ep and blocks until the receiver confirms
// integrity (Done with FlagOK) or an error occurs. It returns the sender
// stats and the SHA-256 of data.
func RunSender(ep Endpoint, cfg Config, data []byte) (SendStats, []byte, error) {
	var st SendStats
	clk := cfg.clock()
	chunk := cfg.Chunk
	total := uint32((len(data) + chunk - 1) / chunk)
	window := uint32(cfg.Window)

	var sent, acked uint32 // chunk indices; absolute seq = InitialSeq + index
	inflight := map[uint32]time.Time{}

	sendData := func(idx uint32) {
		off := int(idx) * chunk
		end := off + chunk
		if end > len(data) {
			end = len(data)
		}
		p := proto.Packet{Gen: cfg.Gen, Type: proto.TypeData, Seq: cfg.InitialSeq + idx, Payload: data[off:end]}
		_ = ep.Send(p.Marshal())
		st.DataSent++
	}

	var timer clock.Timer
	var timerC <-chan time.Time
	stopTimer := func() {
		if timer != nil {
			timer.Stop()
			timer = nil
			timerC = nil
		}
	}
	defer stopTimer()
	// armTimer schedules the retransmission timeout for the oldest
	// unacknowledged packet.
	armTimer := func() {
		stopTimer()
		if len(inflight) == 0 {
			return
		}
		var oldest time.Time
		first := true
		for _, t := range inflight {
			if first || t.Before(oldest) {
				oldest, first = t, false
			}
		}
		d := oldest.Add(cfg.RTO).Sub(clk.Now())
		if d < 0 {
			d = 0
		}
		timer = clk.NewTimer(d)
		timerC = timer.C()
	}

	for acked < total {
		// Fill the window. sent-acked < window is the memory bound:
		// at most Window chunks are unacknowledged at any time.
		for sent < total && sent-acked < window {
			sendData(sent)
			inflight[cfg.InitialSeq+sent] = clk.Now()
			sent++
			if len(inflight) > st.MaxInFlight {
				st.MaxInFlight = len(inflight)
			}
		}
		armTimer()
		select {
		case <-ep.Done:
			return st, nil, ErrLinkClosed
		case b := <-ep.Packets:
			p, err := proto.Unmarshal(b)
			if err != nil {
				continue
			}
			if p.Gen != cfg.Gen {
				st.StaleDrop++
				continue
			}
			if p.Type != proto.TypeAck {
				continue
			}
			st.AcksRecv++
			idx := p.Seq - cfg.InitialSeq // cumulative: next expected index
			if idx > acked {
				if idx > sent {
					idx = sent // clamp against corrupt/duplicate ACKs
				}
				for i := acked; i < idx; i++ {
					delete(inflight, cfg.InitialSeq+i)
				}
				acked = idx
			}
		case <-timerC:
			// Timeout: retransmit every unacknowledged packet.
			now := clk.Now()
			for i := acked; i < sent; i++ {
				seq := cfg.InitialSeq + i
				if _, ok := inflight[seq]; !ok {
					continue
				}
				sendData(i)
				st.Retransmits++
				inflight[seq] = now
			}
		}
	}

	// All data acknowledged: FIN handshake carrying the whole-file hash.
	sum := sha256.Sum256(data)
	fin := proto.Packet{Gen: cfg.Gen, Type: proto.TypeFin, Seq: cfg.InitialSeq + total, Payload: sum[:]}
	maxTries := cfg.MaxFinTries
	if maxTries <= 0 {
		maxTries = 512
	}
	for tries := 0; tries < maxTries; tries++ {
		_ = ep.Send(fin.Marshal())
		st.FinSent++
		t := clk.NewTimer(cfg.RTO)
		select {
		case <-ep.Done:
			t.Stop()
			return st, nil, ErrLinkClosed
		case b := <-ep.Packets:
			p, err := proto.Unmarshal(b)
			if err == nil && p.Gen != cfg.Gen {
				st.StaleDrop++
			}
			if err == nil && p.Gen == cfg.Gen && p.Type == proto.TypeDone {
				t.Stop()
				if p.Flags&proto.FlagOK == 0 {
					return st, nil, ErrHashMismatch
				}
				ack := proto.Packet{Gen: cfg.Gen, Type: proto.TypeDoneAck}
				// Sent twice to reduce the chance the receiver has to
				// wait out its grace period; DoneAck is idempotent.
				_ = ep.Send(ack.Marshal())
				_ = ep.Send(ack.Marshal())
				return st, sum[:], nil
			}
		case <-t.C():
		}
		t.Stop()
	}
	return st, nil, fmt.Errorf("FIN handshake did not complete after %d tries", maxTries)
}

// RunReceiver receives a file over ep and blocks until the transfer
// completes. It returns the receiver stats and the reassembled file.
func RunReceiver(ep Endpoint, cfg Config) (RecvStats, []byte, error) {
	var st RecvStats
	clk := cfg.clock()

	var expect uint32 // index of next in-order chunk
	buf := map[uint32][]byte{}
	var out bytes.Buffer
	haveFin := false
	var total uint32
	var finHash []byte
	complete := false
	doneFlags := uint8(0)

	sendAck := func() {
		ack := proto.Packet{Gen: cfg.Gen, Type: proto.TypeAck, Seq: cfg.InitialSeq + expect}
		_ = ep.Send(ack.Marshal())
		st.AcksSent++
	}
	sendDone := func() {
		d := proto.Packet{Gen: cfg.Gen, Type: proto.TypeDone, Flags: doneFlags}
		_ = ep.Send(d.Marshal())
		st.DoneSent++
	}

	var timer clock.Timer
	var timerC <-chan time.Time
	stopTimer := func() {
		if timer != nil {
			timer.Stop()
			timer = nil
			timerC = nil
		}
	}
	defer stopTimer()
	// armGrace gives the sender a bounded window to retransmit a lost FIN
	// (and receive our repeated Done) before we exit.
	armGrace := func() {
		stopTimer()
		timer = clk.NewTimer(10 * cfg.RTO)
		timerC = timer.C()
	}

	finish := func() {
		complete = true
		sum := sha256.Sum256(out.Bytes())
		if bytes.Equal(sum[:], finHash) {
			doneFlags = proto.FlagOK
		}
		armGrace()
	}

	for {
		select {
		case <-ep.Done:
			return st, nil, ErrLinkClosed
		case <-timerC:
			// Grace period expired with no further FIN: the sender got a
			// Done (or gave up); either way we are finished.
			if complete {
				return st, out.Bytes(), nil
			}
		case b := <-ep.Packets:
			p, err := proto.Unmarshal(b)
			if err != nil {
				continue
			}
			if p.Gen != cfg.Gen {
				st.StaleDrop++
				continue
			}
			switch p.Type {
			case proto.TypeData:
				st.DataRecv++
				idx := p.Seq - cfg.InitialSeq
				switch {
				case idx < expect:
					st.DupRecv++ // already delivered
				case idx-expect < uint32(cfg.Window):
					// Inside the receive window: buffer at most Window
					// out-of-order chunks — the receiver memory bound.
					if _, ok := buf[idx]; !ok {
						buf[idx] = append([]byte(nil), p.Payload...)
						if len(buf) > st.MaxBuf {
							st.MaxBuf = len(buf)
						}
					} else {
						st.DupRecv++
					}
				default:
					// Beyond the receive window: drop; the sender will
					// retransmit after its timeout.
				}
				for {
					pay, ok := buf[expect]
					if !ok {
						break
					}
					out.Write(pay)
					delete(buf, expect)
					expect++
				}
				sendAck()
				if haveFin && !complete && expect == total {
					finish()
				}
			case proto.TypeFin:
				st.FinRecv++
				haveFin = true
				total = p.Seq - cfg.InitialSeq
				finHash = p.Payload
				if !complete && expect == total {
					finish()
				}
				if complete {
					sendDone()
				}
			case proto.TypeDoneAck:
				if complete {
					return st, out.Bytes(), nil
				}
			}
		}
	}
}
