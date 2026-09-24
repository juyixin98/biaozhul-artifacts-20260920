package sim

import (
	"testing"
	"time"
)

// TestRunCompletesWithFaults is the main acceptance test: with a fixed
// seed, injected loss/duplication/reordering and stale old-generation
// packets, a 256 KiB transfer must complete with matching hashes and with
// both window memory bounds respected.
func TestRunCompletesWithFaults(t *testing.T) {
	cfg := RunConfig{
		SizeBytes:    256 << 10,
		Seed:         42,
		DropRate:     0.10,
		DupRate:      0.05,
		ReorderRate:  0.10,
		StalePackets: 5,
		Window:       16,
		Chunk:        1024,
		RTO:          10 * time.Millisecond,
		Timeout:      30 * time.Second,
	}
	res, err := Run(cfg)
	if err != nil {
		t.Fatalf("Run: %v\nresult: %+v", err, res)
	}
	if !res.OK || !res.HashMatch {
		t.Fatalf("transfer not OK: %+v", res)
	}
	if res.Bytes != cfg.SizeBytes {
		t.Fatalf("bytes = %d, want %d", res.Bytes, cfg.SizeBytes)
	}

	// Faults must actually have been injected (seeded, deterministic).
	if res.DataFaults.Dropped == 0 {
		t.Error("no data packets were dropped; fault injection inactive?")
	}
	if res.DataFaults.Duplicated == 0 {
		t.Error("no data packets were duplicated; fault injection inactive?")
	}
	if res.DataFaults.Reordered == 0 {
		t.Error("no data packets were reordered; fault injection inactive?")
	}
	if res.Receiver.StaleDrop != cfg.StalePackets {
		t.Errorf("receiver staleDrop = %d, want %d (all injected stale packets dropped)",
			res.Receiver.StaleDrop, cfg.StalePackets)
	}
	if res.Sender.Retransmits == 0 {
		t.Error("no retransmissions happened despite 10% loss")
	}

	// Window memory bounds.
	if res.Sender.MaxInFlight > cfg.Window {
		t.Errorf("sender in-flight high-water mark %d exceeds window %d",
			res.Sender.MaxInFlight, cfg.Window)
	}
	if res.Sender.MaxInFlight == 0 {
		t.Error("sender MaxInFlight is 0; instrumentation broken?")
	}
	if res.Receiver.MaxBuf > cfg.Window {
		t.Errorf("receiver reorder-buffer high-water mark %d exceeds window %d",
			res.Receiver.MaxBuf, cfg.Window)
	}
	t.Logf("stats: %+v", res)
}

// TestRunAcrossSequenceWrap starts the sequence space 16 numbers before the
// 2^32 wrap boundary; the transfer (320 chunks) crosses it.
func TestRunAcrossSequenceWrap(t *testing.T) {
	cfg := RunConfig{
		SizeBytes:   320 * 512,
		Seed:        7,
		DropRate:    0.05,
		DupRate:     0.05,
		ReorderRate: 0.05,
		Window:      8,
		Chunk:       512,
		RTO:         10 * time.Millisecond,
		Timeout:     30 * time.Second,
		InitialSeq:  0xFFFFFFF0,
	}
	res, err := Run(cfg)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if !res.OK {
		t.Fatalf("transfer across sequence wrap not OK: %+v", res)
	}
}

// TestRunEmptyFile transfers zero bytes: the FIN handshake alone must
// complete and verify the empty-file hash.
func TestRunEmptyFile(t *testing.T) {
	res, err := Run(RunConfig{
		SizeBytes: 0,
		Seed:      1,
		DropRate:  0.2,
		RTO:       5 * time.Millisecond,
		Timeout:   10 * time.Second,
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if !res.OK || res.Bytes != 0 {
		t.Fatalf("empty transfer not OK: %+v", res)
	}
}

// TestRunOverLoopbackUDP runs the same protocol over real UDP sockets on
// 127.0.0.1 instead of in-memory channels.
func TestRunOverLoopbackUDP(t *testing.T) {
	res, err := Run(RunConfig{
		SizeBytes:   64 << 10,
		Seed:        99,
		DropRate:    0.05,
		DupRate:     0.02,
		ReorderRate: 0.05,
		Window:      8,
		Chunk:       1024,
		RTO:         20 * time.Millisecond,
		Timeout:     30 * time.Second,
		Transport:   "udp",
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if !res.OK {
		t.Fatalf("UDP loopback transfer not OK: %+v", res)
	}
}

// TestRunRejectsUnboundedLoss makes sure configurations that could stall
// forever are refused.
func TestRunRejectsUnboundedLoss(t *testing.T) {
	_, err := Run(RunConfig{SizeBytes: 1024, DropRate: 0.96, Seed: 1})
	if err == nil {
		t.Fatal("expected validation error for drop rate 0.96")
	}
}

// TestRunDeterministic same seed twice must produce identical stats.
func TestRunDeterministic(t *testing.T) {
	cfg := RunConfig{
		SizeBytes:   64 << 10,
		Seed:        1234,
		DropRate:    0.1,
		DupRate:     0.05,
		ReorderRate: 0.1,
		Window:      8,
		RTO:         10 * time.Millisecond,
		Timeout:     30 * time.Second,
	}
	a, err := Run(cfg)
	if err != nil {
		t.Fatalf("first run: %v", err)
	}
	b, err := Run(cfg)
	if err != nil {
		t.Fatalf("second run: %v", err)
	}
	if a.Hash != b.Hash {
		t.Fatalf("hash differs between runs: %s vs %s", a.Hash, b.Hash)
	}
	if a.DataFaults != b.DataFaults || a.AckFaults != b.AckFaults {
		t.Fatalf("fault stats differ between runs:\n%+v\n%+v", a.DataFaults, b.DataFaults)
	}
}
