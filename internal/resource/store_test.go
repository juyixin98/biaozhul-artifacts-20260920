package resource

import (
	"errors"
	"path/filepath"
	"testing"
)

func newTestStore(t *testing.T) *Store {
	t.Helper()
	s, err := NewStore(filepath.Join(t.TempDir(), "resource.json"))
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	return s
}

func TestWriteEnforcesHighWaterMark(t *testing.T) {
	s := newTestStore(t)

	if _, _, err := s.Write(0, "x"); !errors.Is(err, ErrBadToken) {
		t.Fatalf("write with zero token: got %v, want ErrBadToken", err)
	}
	if _, _, err := s.Write(-3, "x"); !errors.Is(err, ErrBadToken) {
		t.Fatalf("write with negative token: got %v, want ErrBadToken", err)
	}

	v1, hw, err := s.Write(1, "from-A")
	if err != nil {
		t.Fatalf("write token 1: %v", err)
	}
	if v1 != 1 || hw != 1 {
		t.Fatalf("first write version/hw = %d/%d, want 1/1", v1, hw)
	}

	// Retry with the same token is accepted (idempotent monotonic rule).
	v2, _, err := s.Write(1, "from-A-retry")
	if err != nil {
		t.Fatalf("equal-token write: %v", err)
	}
	if v2 != 2 {
		t.Fatalf("equal-token write version = %d, want 2", v2)
	}

	// A newer holder writes with a larger token.
	v3, hw, err := s.Write(2, "from-B")
	if err != nil {
		t.Fatalf("write token 2: %v", err)
	}
	if v3 != 3 || hw != 2 {
		t.Fatalf("write token 2 version/hw = %d/%d, want 3/2", v3, hw)
	}

	// THE FENCING CHECK: delayed write from the old holder carrying token 1.
	_, hw, err = s.Write(1, "stale-from-A")
	if !errors.Is(err, ErrStaleWrite) {
		t.Fatalf("stale write: got %v, want ErrStaleWrite", err)
	}
	if hw != 2 {
		t.Fatalf("stale write moved high-water mark to %d, want 2", hw)
	}

	value, version, high := s.Read()
	if value != "from-B" || version != 3 || high != 2 {
		t.Fatalf("read = %q v%d hw%d, want %q v3 hw2", value, version, high, "from-B")
	}
}

func TestResourcePersistsHighWaterMark(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "resource.json")

	s1, err := NewStore(path)
	if err != nil {
		t.Fatalf("open 1: %v", err)
	}
	if _, _, err := s1.Write(5, "newer-write"); err != nil {
		t.Fatalf("write token 5: %v", err)
	}

	// Simulate resource service restart from the same file.
	s2, err := NewStore(path)
	if err != nil {
		t.Fatalf("open 2: %v", err)
	}
	value, version, hw := s2.Read()
	if value != "newer-write" || version != 1 || hw != 5 {
		t.Fatalf("after restart read = %q v%d hw%d, want new-write/1/5", value, version, hw)
	}
	// Old-holder write arriving after restart is still rejected.
	if _, _, err := s2.Write(1, "old-after-restart"); !errors.Is(err, ErrStaleWrite) {
		t.Fatalf("stale write after restart: got %v, want ErrStaleWrite", err)
	}
}
