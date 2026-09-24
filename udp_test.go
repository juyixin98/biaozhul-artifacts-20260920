package rt

import (
	"bytes"
	"context"
	"testing"
	"time"
)

// 真实 UDP 回环 + 真实时钟，温和的随机故障（乱序报文 5ms 后冲刷）。
func TestUDPLoopback(t *testing.T) {
	if testing.Short() {
		t.Skip("udp loopback uses real clock")
	}
	data := patternData(8000)
	cfg := Config{
		MSS:        512,
		WindowSize: 8,
		RTO:        60 * time.Millisecond,
		MaxRetries: 100,
		StartSeq:   12345,
		Clock:      RealClock{},
	}
	a, b, err := NewUDPLoopbackPair(UDPOptions{
		Clock:   RealClock{},
		HoldMax: 5 * time.Millisecond,
		Faults: PipeFaults{
			AB: FaultPolicy{
				Seed:     31,
				LossRate: 0.15, LossBudget: -1,
				DupRate: 0.05, DupBudget: -1,
				ReorderRate: 0.1, ReorderBudget: -1, ReorderHold: 1,
				Ghosts: ghostsFor(42, 3),
			},
			BA: FaultPolicy{
				Seed:     32,
				LossRate: 0.2, LossBudget: -1,
				DupRate: 0.05, DupBudget: -1,
				Ghosts: ghostsFor(42, 3),
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	var got bytes.Buffer
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	start := time.Now()
	res, err := RunTransfer(ctx, a, b, 42, cfg, bytes.NewReader(data), &got)
	if err != nil {
		t.Fatalf("udp transfer: %v", err)
	}
	if !bytes.Equal(got.Bytes(), data) {
		t.Fatal("udp payload mismatch")
	}
	if !bytes.Equal(res.SenderHash, res.ReceiverHash) {
		t.Fatal("udp hash mismatch")
	}
	if res.Sender.MaxBufferedPackets > cfg.WindowSize {
		t.Errorf("udp MaxBufferedPackets=%d > window", res.Sender.MaxBufferedPackets)
	}
	t.Logf("UDP loopback: %d bytes in %s, DATA lost(AB)=%d ACK lost(BA)=%d, retransmits=%d (timeout=%d fast=%d), stale=%d",
		res.Size, time.Since(start),
		res.Faults.AB.Lost, res.Faults.BA.Lost,
		res.Sender.Retransmits, res.Sender.TimeoutRetransmits, res.Sender.FastRetransmits,
		res.Receiver.StalePackets)
}

// 干净 UDP 回环（无故障）冒烟。
func TestUDPLoopbackClean(t *testing.T) {
	if testing.Short() {
		t.Skip("udp loopback uses real clock")
	}
	data := patternData(3000)
	cfg := Config{
		MSS: 1000, WindowSize: 4,
		RTO: 60 * time.Millisecond, MaxRetries: 50,
		Clock: RealClock{},
	}
	a, b, err := NewUDPLoopbackPair(UDPOptions{Clock: RealClock{}, HoldMax: 2 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	var got bytes.Buffer
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if _, err := RunTransfer(ctx, a, b, 7, cfg, bytes.NewReader(data), &got); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got.Bytes(), data) {
		t.Fatal("clean udp payload mismatch")
	}
}
