package sim

import (
	"bytes"
	"crypto/sha256"
	"errors"
	"fmt"
	"math/rand"
	"net"
	"time"

	"udpreliable/internal/clock"
	"udpreliable/internal/proto"
	"udpreliable/internal/transport"
)

// RunConfig describes one simulated transfer.
type RunConfig struct {
	SizeBytes    int           // file size; content is derived deterministically from Seed
	Seed         int64         // seeds file content, connection generation and fault layers
	DropRate     float64       // per-packet drop probability, each direction
	DupRate      float64       // per-packet duplicate probability
	ReorderRate  float64       // per-packet reorder (hold-and-release) probability
	StalePackets int           // old-generation packets injected toward the receiver
	Window       int           // sliding-window size in packets (default 16)
	Chunk        int           // payload bytes per Data packet (default 1024)
	RTO          time.Duration // retransmission timeout (default 50ms)
	Transport    string        // "mem" (default, in-memory channel) or "udp" (loopback UDP)
	Timeout      time.Duration // overall wall-clock deadline (default 30s)
	InitialSeq   uint32        // first sequence number; set near 2^32 to exercise wrap-around
	Clock        clock.Clock   // optional; defaults to the real clock
}

func (c *RunConfig) withDefaults() {
	if c.Window <= 0 {
		c.Window = 16
	}
	if c.Chunk <= 0 {
		c.Chunk = 1024
	}
	if c.RTO <= 0 {
		c.RTO = 50 * time.Millisecond
	}
	if c.Transport == "" {
		c.Transport = "mem"
	}
	if c.Timeout <= 0 {
		c.Timeout = 30 * time.Second
	}
}

// ErrInvalidConfig marks configuration validation failures.
var ErrInvalidConfig = errors.New("invalid config")

func (c RunConfig) validate() error {
	if c.SizeBytes < 0 || c.SizeBytes > 256<<20 {
		return fmt.Errorf("sizeBytes must be in [0, 256MiB], got %d", c.SizeBytes)
	}
	if c.Window < 1 || c.Window > 4096 {
		return fmt.Errorf("window must be in [1, 4096], got %d", c.Window)
	}
	if c.Chunk < 64 || c.Chunk > 60000 {
		return fmt.Errorf("chunkSize must be in [64, 60000], got %d", c.Chunk)
	}
	for _, r := range []float64{c.DropRate, c.DupRate, c.ReorderRate} {
		if r < 0 || r >= 1 {
			return errors.New("drop/dup/reorder rates must be in [0, 1)")
		}
	}
	// Bounded-loss guarantee: a non-zero share of packets always gets
	// through, so retransmission terminates with probability 1.
	if c.DropRate+c.DupRate+c.ReorderRate > 0.95 {
		return errors.New("drop+dup+reorder must be <= 0.95 (bounded loss)")
	}
	if c.StalePackets < 0 || c.StalePackets > 1024 {
		return fmt.Errorf("stalePackets must be in [0, 1024], got %d", c.StalePackets)
	}
	if c.Transport != "mem" && c.Transport != "udp" {
		return fmt.Errorf("transport must be \"mem\" or \"udp\", got %q", c.Transport)
	}
	return nil
}

// RunResult is the outcome of one simulated transfer.
type RunResult struct {
	OK         bool
	Bytes      int
	Hash       string // SHA-256 (hex) of the file as sent
	HashMatch  bool   // receiver's reassembled hash equals Hash
	Duration   time.Duration
	Sender     transport.SendStats
	Receiver   transport.RecvStats
	DataFaults FaultStats // faults injected on the sender->receiver direction
	AckFaults  FaultStats // faults injected on the receiver->sender direction
}

// Run executes one transfer to completion (or timeout) and returns its
// result. A non-nil error means the transfer did not complete; res still
// carries the counters gathered so far.
func Run(cfg RunConfig) (res RunResult, err error) {
	cfg.withDefaults()
	if err := cfg.validate(); err != nil {
		return res, fmt.Errorf("%w: %v", ErrInvalidConfig, err)
	}

	// Deterministic file content and connection generation from the seed.
	rng := rand.New(rand.NewSource(cfg.Seed))
	data := make([]byte, cfg.SizeBytes)
	_, _ = rng.Read(data)
	gen := rng.Uint32()

	done := make(chan struct{})
	defer close(done)

	lossyData := NewLossy(cfg.Seed^0xA5A5A5A5, cfg.DropRate, cfg.DupRate, cfg.ReorderRate)
	lossyAck := NewLossy(cfg.Seed^0x5A5A5A5A, cfg.DropRate, cfg.DupRate, cfg.ReorderRate)

	var senderEP, receiverEP transport.Endpoint
	var rawDataSend, rawAckSend func([]byte) error

	switch cfg.Transport {
	case "udp":
		connA, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
		if err != nil {
			return res, fmt.Errorf("listen udp: %w", err)
		}
		defer connA.Close()
		connB, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
		if err != nil {
			return res, fmt.Errorf("listen udp: %w", err)
		}
		defer connB.Close()
		addrA := connA.LocalAddr().(*net.UDPAddr)
		addrB := connB.LocalAddr().(*net.UDPAddr)
		rawDataSend = func(b []byte) error { _, err := connA.WriteToUDP(b, addrB); return err }
		rawAckSend = func(b []byte) error { _, err := connB.WriteToUDP(b, addrA); return err }
		senderEP = transport.Endpoint{Send: lossyData.Wrap(rawDataSend), Packets: udpReader(connA, done), Done: done}
		receiverEP = transport.Endpoint{Send: lossyAck.Wrap(rawAckSend), Packets: udpReader(connB, done), Done: done}
	default: // "mem"
		ab := make(chan []byte, 4096)
		ba := make(chan []byte, 4096)
		rawDataSend = func(b []byte) error {
			select {
			case ab <- b:
				return nil
			case <-done:
				return transport.ErrLinkClosed
			}
		}
		rawAckSend = func(b []byte) error {
			select {
			case ba <- b:
				return nil
			case <-done:
				return transport.ErrLinkClosed
			}
		}
		senderEP = transport.Endpoint{Send: lossyData.Wrap(rawDataSend), Packets: ba, Done: done}
		receiverEP = transport.Endpoint{Send: lossyAck.Wrap(rawAckSend), Packets: ab, Done: done}
	}
	defer lossyData.Flush(rawDataSend)
	defer lossyAck.Flush(rawAckSend)

	// Inject stale packets from a previous connection generation; the
	// receiver must drop every one of them.
	for i := 0; i < cfg.StalePackets; i++ {
		stale := proto.Packet{
			Gen:     gen ^ 0xDEADBEEF,
			Type:    proto.TypeData,
			Seq:     cfg.InitialSeq + uint32(i),
			Payload: []byte("stale packet from an old connection"),
		}
		_ = rawDataSend(stale.Marshal())
	}

	tcfg := transport.Config{
		Gen:        gen,
		Window:     cfg.Window,
		Chunk:      cfg.Chunk,
		RTO:        cfg.RTO,
		InitialSeq: cfg.InitialSeq,
		Clock:      cfg.Clock,
	}

	type sendOut struct {
		st   transport.SendStats
		hash []byte
		err  error
	}
	type recvOut struct {
		st   transport.RecvStats
		data []byte
		err  error
	}
	sCh := make(chan sendOut, 1)
	rCh := make(chan recvOut, 1)
	start := time.Now()
	go func() {
		st, h, err := transport.RunSender(senderEP, tcfg, data)
		sCh <- sendOut{st, h, err}
	}()
	go func() {
		st, d, err := transport.RunReceiver(receiverEP, tcfg)
		rCh <- recvOut{st, d, err}
	}()

	var s sendOut
	var r recvOut
	deadline := time.After(cfg.Timeout) // wall-clock guard regardless of injected clock
	for i := 0; i < 2; i++ {
		select {
		case s = <-sCh:
		case r = <-rCh:
		case <-deadline:
			res.Sender = s.st
			res.Receiver = r.st
			res.DataFaults = lossyData.Stats()
			res.AckFaults = lossyAck.Stats()
			return res, fmt.Errorf("transfer timed out after %s", cfg.Timeout)
		}
	}
	res.Duration = time.Since(start)
	res.Sender = s.st
	res.Receiver = r.st
	res.DataFaults = lossyData.Stats()
	res.AckFaults = lossyAck.Stats()

	if s.err != nil {
		return res, fmt.Errorf("sender: %w", s.err)
	}
	if r.err != nil {
		return res, fmt.Errorf("receiver: %w", r.err)
	}
	recvHash := sha256.Sum256(r.data)
	res.Bytes = len(r.data)
	res.Hash = fmt.Sprintf("%x", s.hash)
	res.HashMatch = bytes.Equal(s.hash, recvHash[:]) && len(r.data) == len(data)
	res.OK = res.HashMatch
	if !res.OK {
		return res, errors.New("hash mismatch between sender and receiver")
	}
	return res, nil
}

// udpReader forwards datagrams from conn into a channel until done closes.
func udpReader(conn *net.UDPConn, done <-chan struct{}) <-chan []byte {
	ch := make(chan []byte, 1024)
	go func() {
		buf := make([]byte, 64*1024)
		for {
			_ = conn.SetReadDeadline(time.Now().Add(100 * time.Millisecond))
			n, _, err := conn.ReadFromUDP(buf)
			if err != nil {
				var ne net.Error
				if errors.As(err, &ne) && ne.Timeout() {
					select {
					case <-done:
						return
					default:
						continue
					}
				}
				return
			}
			pkt := append([]byte(nil), buf[:n]...)
			select {
			case ch <- pkt:
			case <-done:
				return
			}
		}
	}()
	return ch
}
