package lock

import (
	"path/filepath"
	"testing"
	"time"

	"fencingdemo/clock"
)

// TestPersistenceMonotonicAcrossRestart verifies the acceptance requirement:
// after a restart the fencing token never goes backwards.
func TestPersistenceMonotonicAcrossRestart(t *testing.T) {
	start := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	clk := clock.NewFake(start)
	path := filepath.Join(t.TempDir(), "lock.json")
	ttl := 5 * time.Second

	s1, err := NewStore(clk, path, ttl)
	if err != nil {
		t.Fatalf("first open: %v", err)
	}
	l1, err := s1.Acquire("client-A")
	if err != nil {
		t.Fatalf("acquire A: %v", err)
	}

	// Restart while the lease is still live: state must be recovered.
	s2, err := NewStore(clk, path, ttl)
	if err != nil {
		t.Fatalf("reopen live: %v", err)
	}
	if got := s2.NextToken(); got != l1.Token+1 {
		t.Fatalf("next token after reopen = %d, want %d", got, l1.Token+1)
	}
	if _, err := s2.Acquire("client-B"); err != ErrHeld {
		t.Fatalf("acquire B after restart with live lease: got %v, want ErrHeld", err)
	}

	// Advance past expiry and "restart" again: expired lease is dropped but
	// the counter survives.
	clk.Advance(6 * time.Second)
	s3, err := NewStore(clk, path, ttl)
	if err != nil {
		t.Fatalf("reopen expired: %v", err)
	}
	l3, err := s3.Acquire("client-B")
	if err != nil {
		t.Fatalf("acquire B after expiry+restart: %v", err)
	}
	if l3.Token <= l1.Token {
		t.Fatalf("token regressed after restart: %d -> %d", l1.Token, l3.Token)
	}

	// One more restart (after B's lease expires): counter keeps moving forward.
	clk.Advance(6 * time.Second)
	s4, err := NewStore(clk, path, ttl)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	l4, err := s4.Acquire("client-C")
	if err != nil {
		t.Fatalf("acquire C: %v", err)
	}
	if l4.Token <= l3.Token {
		t.Fatalf("token regressed: %d -> %d", l3.Token, l4.Token)
	}
}
