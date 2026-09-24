package store

import "testing"

func TestPutGetVersionBump(t *testing.T) {
	s := New()
	if changed, err := s.Put("a", "v1", 0); err != nil || !changed {
		t.Fatalf("first put: changed=%v err=%v", changed, err)
	}
	e, ok := s.Get("a")
	if !ok || e.Version != 1 || e.Value != "v1" || e.Deleted {
		t.Fatalf("unexpected entry: %+v ok=%v", e, ok)
	}
	if _, err := s.Put("a", "v2", 0); err != nil {
		t.Fatal(err)
	}
	if e, _ := s.Get("a"); e.Version != 2 || e.Value != "v2" {
		t.Fatalf("version bump failed: %+v", e)
	}
	if s.Revision() != 2 {
		t.Fatalf("revision = %d, want 2", s.Revision())
	}
}

func TestStaleWriteLoses(t *testing.T) {
	s := New()
	_, _ = s.Put("a", "new", 3)
	if changed, _ := s.Put("a", "old", 2); changed {
		t.Fatal("stale write must not change state")
	}
	if changed, _ := s.Put("a", "same", 3); changed {
		t.Fatal("equal-version write must not change state")
	}
	if e, _ := s.Get("a"); e.Value != "new" {
		t.Fatalf("LWW violated: %+v", e)
	}
}

func TestDeleteTombstoneAndPropagation(t *testing.T) {
	s := New()
	_, _ = s.Put("a", "v1", 0)
	if changed, _ := s.Delete("a", 0); !changed {
		t.Fatal("delete should change state")
	}
	e, ok := s.Get("a")
	if !ok || !e.Deleted || e.Version != 2 {
		t.Fatalf("expected tombstone v2, got %+v ok=%v", e, ok)
	}

	// Delete of a never-seen key still creates a tombstone that must
	// propagate to replicas holding the key.
	if changed, _ := s.Delete("ghost", 0); !changed {
		t.Fatal("delete of missing key should create tombstone")
	}
	if e, ok := s.Get("ghost"); !ok || !e.Deleted || e.Version != 1 {
		t.Fatalf("ghost tombstone wrong: %+v ok=%v", e, ok)
	}

	// Stale delete must not resurrect or overwrite.
	if changed, _ := s.Delete("a", 1); changed {
		t.Fatal("stale delete must be ignored")
	}
}

func TestApplyEntriesLWW(t *testing.T) {
	s := New()
	_, _ = s.Put("a", "old", 2)
	_, _ = s.Put("b", "keep", 5)

	applied := s.ApplyEntries([]Entry{
		{Key: "a", Value: "new", Version: 3},   // wins
		{Key: "a", Value: "older", Version: 1}, // loses
		{Key: "b", Value: "x", Version: 4},     // loses
		{Key: "c", Version: 7, Deleted: true},  // tombstone wins (new key)
		{Key: "d", Version: 0, Value: "bad"},   // invalid version, skipped
	})
	if len(applied) != 2 {
		t.Fatalf("applied = %d, want 2: %+v", len(applied), applied)
	}
	if e, _ := s.Get("a"); e.Value != "new" || e.Version != 3 {
		t.Fatalf("a = %+v", e)
	}
	if e, _ := s.Get("b"); e.Value != "keep" {
		t.Fatalf("b = %+v", e)
	}
	if e, _ := s.Get("c"); !e.Deleted || e.Version != 7 {
		t.Fatalf("c = %+v", e)
	}
	if _, ok := s.Get("d"); ok {
		t.Fatal("invalid entry should not have been applied")
	}
}

func TestResetEpoch(t *testing.T) {
	s := New()
	_, _ = s.Put("a", "1", 1)
	oldEpoch := s.Epoch()
	s.Reset()
	if s.Revision() != 0 || s.Epoch() != oldEpoch+1 {
		t.Fatalf("after reset: rev=%d epoch=%d", s.Revision(), s.Epoch())
	}
	if _, ok := s.Get("a"); ok {
		t.Fatal("store not empty after reset")
	}
}
