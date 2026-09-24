package store

import (
	"path/filepath"
	"testing"
	"time"
)

func newTestStore(t *testing.T, opts Options) *Store {
	t.Helper()
	opts.Dir = filepath.Join(t.TempDir(), "data")
	st, err := Open(opts)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return st
}

func TestAppendMonotonicAndSince(t *testing.T) {
	st := newTestStore(t, Options{})
	base := time.Now().UTC()
	for i := 0; i < 5; i++ {
		ev, err := st.Append(string(rune('a'+i)), base.Add(time.Duration(i)*time.Second))
		if err != nil {
			t.Fatal(err)
		}
		if ev.ID != uint64(i+1) {
			t.Fatalf("event %d got id %d", i, ev.ID)
		}
	}
	got := st.Since(2, 0)
	if len(got) != 3 || got[0].ID != 3 || got[2].ID != 5 {
		t.Fatalf("Since(2) = %+v", got)
	}
	if got := st.Since(5, 0); got != nil {
		t.Fatalf("Since(last) must be empty, got %+v", got)
	}
	if got := st.Since(0, 2); len(got) != 2 || got[0].ID != 1 {
		t.Fatalf("Since(0, limit=2) = %+v", got)
	}
	b := st.Bounds()
	if b.Oldest != 1 || b.Last != 5 || b.Empty {
		t.Fatalf("bounds = %+v", b)
	}
}

func TestDurabilityAcrossReopen(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "data")
	opts := Options{Dir: dir, Sync: true}
	st, err := Open(opts)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 10; i++ {
		if _, err := st.Append("payload", time.Now().UTC()); err != nil {
			t.Fatal(err)
		}
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}

	st2, err := Open(opts)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	if n := st2.Count(); n != 10 {
		t.Fatalf("after reopen count=%d want 10", n)
	}
	// The next ID must continue monotonically after recovery.
	ev, _ := st2.Append("after-restart", time.Now().UTC())
	if ev.ID != 11 {
		t.Fatalf("post-restart id=%d want 11", ev.ID)
	}
}

func TestMaxEventsRetentionAndCompaction(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "data")
	opts := Options{Dir: dir, MaxEvents: 5, CompactEventDelta: 1}
	st, err := Open(opts)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 12; i++ {
		if _, err := st.Append("x", time.Now().UTC()); err != nil {
			t.Fatal(err)
		}
	}
	b := st.Bounds()
	if b.Oldest != 8 || b.Last != 12 || st.Count() != 5 {
		t.Fatalf("retention bounds=%+v count=%d", b, st.Count())
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}

	// Compacted WAL on disk must contain only the retained window.
	st2, err := Open(Options{Dir: dir, MaxEvents: 5, CompactEventDelta: 1})
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	b2 := st2.Bounds()
	if b2.Oldest != 8 || b2.Last != 12 {
		t.Fatalf("after reopen bounds=%+v want [8,12]", b2)
	}
}

func TestMaxAgeRetention(t *testing.T) {
	st := newTestStore(t, Options{MaxAge: time.Hour, CompactEventDelta: 1})
	now := time.Now().UTC()
	if _, err := st.Append("old", now.Add(-2*time.Hour)); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Append("fresh", now); err != nil {
		t.Fatal(err)
	}
	if st.Count() != 1 {
		t.Fatalf("age retention count=%d want 1", st.Count())
	}
	if got := st.Since(0, 0); got[0].Data != "fresh" {
		t.Fatalf("wrong survivor: %+v", got)
	}
}

func TestTornWriteRecovery(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "data")
	st, err := Open(Options{Dir: dir, Sync: true})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.Append("good", time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}

	// Append garbage simulating a torn write past the good record.
	path := filepath.Join(dir, "events.log")
	f, err := openAppend(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Write([]byte{0x00, 0x00, 0x00, 0x00, 0x00}); err != nil {
		t.Fatal(err)
	}
	_ = f.Close()

	st2, err := Open(Options{Dir: dir, Sync: true})
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	if st2.Count() != 1 {
		t.Fatalf("torn recovery count=%d want 1", st2.Count())
	}
	// Appending after recovery must succeed and the torn bytes must be gone.
	ev, err := st2.Append("after", time.Now().UTC())
	if err != nil {
		t.Fatalf("append after torn write: %v", err)
	}
	if ev.ID != 2 {
		t.Fatalf("id after recovery=%d want 2", ev.ID)
	}
}

func TestSinceReturnsCopy(t *testing.T) {
	st := newTestStore(t, Options{})
	_, _ = st.Append("x", time.Now().UTC())
	got := st.Since(0, 0)
	got[0].Data = "mutated"
	again := st.Since(0, 0)
	if again[0].Data != "x" {
		t.Fatal("caller was able to mutate store state")
	}
}
