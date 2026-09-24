package lock

import (
	"errors"
	"path/filepath"
	"testing"
	"time"

	"fencingdemo/clock"
)

func newTestStore(t *testing.T) (*Store, *clock.Fake) {
	t.Helper()
	start := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	clk := clock.NewFake(start)
	path := filepath.Join(t.TempDir(), "lock.json")
	s, err := NewStore(clk, path, 5*time.Second)
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	return s, clk
}

func TestAcquireIssuesIncreasingTokens(t *testing.T) {
	s, clk := newTestStore(t)

	l1, err := s.Acquire("client-A")
	if err != nil {
		t.Fatalf("first acquire: %v", err)
	}
	if l1.Token != 1 {
		t.Fatalf("first token = %d, want 1", l1.Token)
	}

	// Another client is refused while the lease is live.
	if _, err := s.Acquire("client-B"); !errors.Is(err, ErrHeld) {
		t.Fatalf("second acquire while held: got %v, want ErrHeld", err)
	}

	// Advance beyond the TTL: the old holder is stale, B gets a higher token.
	clk.Advance(6 * time.Second)
	l2, err := s.Acquire("client-B")
	if err != nil {
		t.Fatalf("acquire after expiry: %v", err)
	}
	if l2.Token <= l1.Token {
		t.Fatalf("new token %d not greater than old token %d", l2.Token, l1.Token)
	}
	if l2.ID != "client-B" {
		t.Fatalf("holder = %q, want client-B", l2.ID)
	}
}

func TestRenewExtendsLease(t *testing.T) {
	s, clk := newTestStore(t)

	l, err := s.Acquire("client-A")
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	oldExpiry := l.Expires

	clk.Advance(2 * time.Second)
	r, err := s.Renew("client-A")
	if err != nil {
		t.Fatalf("renew: %v", err)
	}
	if !r.Expires.After(oldExpiry) {
		t.Fatalf("renewed expiry %v not after %v", r.Expires, oldExpiry)
	}
	if r.Token != l.Token {
		t.Fatalf("renew changed token: %d -> %d", l.Token, r.Token)
	}

	// Renewal shifted expiry to start+2s+ttl = start+7s. Advancing to
	// start+6s must therefore keep the lock held.
	clk.Advance(4 * time.Second)
	if _, _, held := s.Status(); !held {
		t.Fatal("lock expired despite renewal extending the deadline")
	}

	// Advancing beyond the renewed deadline expires it.
	clk.Advance(2 * time.Second)
	if _, _, held := s.Status(); held {
		t.Fatal("lock still held past renewed deadline")
	}
}

func TestRenewErrors(t *testing.T) {
	s, clk := newTestStore(t)

	if _, err := s.Renew("nobody"); !errors.Is(err, ErrNotHolder) {
		t.Fatalf("renew free lock: got %v, want ErrNotHolder", err)
	}

	l, err := s.Acquire("client-A")
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	if _, err := s.Renew("client-B"); !errors.Is(err, ErrNotHolder) {
		t.Fatalf("renew as non-holder: got %v, want ErrNotHolder", err)
	}

	clk.Advance(6 * time.Second)
	if _, err := s.Renew(l.ID); !errors.Is(err, ErrExpired) {
		t.Fatalf("renew expired lease: got %v, want ErrExpired", err)
	}
}

func TestRelease(t *testing.T) {
	s, clk := newTestStore(t)

	if err := s.Release("nobody"); !errors.Is(err, ErrNotHolder) {
		t.Fatalf("release free lock: got %v, want ErrNotHolder", err)
	}

	l, err := s.Acquire("client-A")
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	if err := s.Release("client-B"); !errors.Is(err, ErrNotHolder) {
		t.Fatalf("release by non-holder: got %v, want ErrNotHolder", err)
	}
	if err := s.Release(l.ID); err != nil {
		t.Fatalf("release by holder: %v", err)
	}
	// Lock is free immediately: a new acquire succeeds.
	l2, err := s.Acquire("client-B")
	if err != nil {
		t.Fatalf("acquire after release: %v", err)
	}
	if l2.Token <= l.Token {
		t.Fatalf("token after release = %d, want > %d", l2.Token, l.Token)
	}

	// Releasing an expired lease is an error (it is no longer yours).
	clk.Advance(6 * time.Second) // B's lease from above expires
	l3, err := s.Acquire("client-C")
	if err != nil {
		t.Fatalf("acquire C: %v", err)
	}
	clk.Advance(6 * time.Second)
	if err := s.Release(l3.ID); !errors.Is(err, ErrExpired) {
		t.Fatalf("release expired: got %v, want ErrExpired", err)
	}
}

func TestStatus(t *testing.T) {
	s, clk := newTestStore(t)

	if _, _, held := s.Status(); held {
		t.Fatal("fresh store reports held")
	}
	l, _ := s.Acquire("client-A")
	got, _, held := s.Status()
	if !held || got.Token != l.Token {
		t.Fatalf("status = %+v held=%v, want token %d held=true", got, held, l.Token)
	}
	clk.Advance(6 * time.Second)
	if _, _, held := s.Status(); held {
		t.Fatal("expired lease still reports held")
	}
}
