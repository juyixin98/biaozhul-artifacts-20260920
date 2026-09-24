package store

import (
	"testing"

	"github.com/example/merklekv/internal/hlc"
)

func TestPutDeleteGet(t *testing.T) {
	s := New(Config{ReplicaID: "r1"})
	if _, ok := s.Get("k"); ok {
		t.Fatal("empty store returned a key")
	}
	e := s.Put("k", []byte("v1"))
	if e.Origin != "r1" || string(e.Value) != "v1" {
		t.Fatalf("unexpected put entry: %+v", e)
	}
	if got, ok := s.Get("k"); !ok || string(got.Value) != "v1" {
		t.Fatalf("get after put: %v %v", got, ok)
	}
	s.Delete("k")
	if _, ok := s.Get("k"); ok {
		t.Fatal("get after delete must miss, tombstone hides the key")
	}
	// The tombstone still exists in the full record set.
	all := s.AllEntries()
	if len(all) != 1 || !all[0].Deleted {
		t.Fatalf("expected one tombstone record, got %+v", all)
	}
}

func TestEpochGuardRejectsStaleApply(t *testing.T) {
	s := New(Config{ReplicaID: "r1"})
	s.Put("k", []byte("v1"))
	epoch := s.Epoch()
	s.Put("k2", []byte("v2")) // bumps epoch

	_, ok := s.ApplyEntries(epoch, []Entry{{Key: "x", Ver: hlc.Timestamp{Wall: 1}, Origin: "r2", Value: []byte("y")}})
	if ok {
		t.Fatal("apply with stale expected epoch must be rejected")
	}
	if _, exists := s.Get("x"); exists {
		t.Fatal("rejected apply must not modify the store")
	}

	// Apply with the current epoch succeeds and bumps it.
	res, ok := s.ApplyEntries(-1, []Entry{{Key: "x", Ver: hlc.Timestamp{Wall: 9999}, Origin: "r2", Value: []byte("y")}})
	if !ok || res.Applied != 1 {
		t.Fatalf("unguarded apply: ok=%v res=%+v", ok, res)
	}
}

func TestLWWMergeAndTombstone(t *testing.T) {
	s := New(Config{ReplicaID: "r1"})
	s.Seed([]Entry{
		{Key: "k", Value: []byte("old"), Ver: hlc.Timestamp{Wall: 10}, Origin: "r1"},
	})

	// Older remote version is ignored.
	res, _ := s.ApplyEntries(-1, []Entry{
		{Key: "k", Value: []byte("stale"), Ver: hlc.Timestamp{Wall: 9}, Origin: "r2"},
	})
	if res.Unchanged != 1 || res.Applied != 0 {
		t.Fatalf("stale write should be skipped: %+v", res)
	}
	if got, _ := s.Get("k"); string(got.Value) != "old" {
		t.Fatal("stale write replaced local value")
	}

	// Newer tombstone wins and hides the key.
	res, _ = s.ApplyEntries(-1, []Entry{
		{Key: "k", Deleted: true, Ver: hlc.Timestamp{Wall: 11}, Origin: "r2"},
	})
	if res.Applied != 1 {
		t.Fatalf("tombstone apply: %+v", res)
	}
	if _, ok := s.Get("k"); ok {
		t.Fatal("newer tombstone must hide the key")
	}

	// A still-newer live version resurrects it.
	res, _ = s.ApplyEntries(-1, []Entry{
		{Key: "k", Value: []byte("new"), Ver: hlc.Timestamp{Wall: 12}, Origin: "r1"},
	})
	if got, ok := s.Get("k"); !ok || string(got.Value) != "new" {
		t.Fatalf("newer live version must win over tombstone: ok=%v", ok)
	}

	// Exact timestamp tie: larger origin wins deterministically.
	s2 := New(Config{ReplicaID: "r1"})
	s2.Seed([]Entry{{Key: "k", Value: []byte("a"), Ver: hlc.Timestamp{Wall: 20}, Origin: "r1"}})
	s2.ApplyEntries(-1, []Entry{{Key: "k", Value: []byte("b"), Ver: hlc.Timestamp{Wall: 20}, Origin: "r2"}})
	if got, _ := s2.Get("k"); string(got.Value) != "b" {
		t.Fatal("tie should break toward r2 (larger origin)")
	}
}

func TestSnapshotImmutableAcrossMutation(t *testing.T) {
	s := New(Config{ReplicaID: "r1"})
	s.Put("k", []byte("v1"))
	snap := s.Snapshot()

	s.Put("k", []byte("v2"))
	s.Put("other", []byte("v3"))

	// The pinned snapshot keeps its original contents and epoch.
	if len(snap.Entries()) != 1 {
		t.Fatalf("snapshot mutated: %d entries", len(snap.Entries()))
	}
	if got := snap.Entries()[0]; string(got.Value) != "v1" {
		t.Fatalf("snapshot value changed: %q", got.Value)
	}
	if same := s.Snapshot(); same.ID == snap.ID {
		// A new epoch must produce a new snapshot.
		t.Fatal("snapshot should not be reused across epochs")
	}
}
