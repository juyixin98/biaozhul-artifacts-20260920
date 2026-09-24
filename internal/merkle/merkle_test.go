package merkle

import (
	"testing"

	"merklesync/internal/store"
)

func buildFrom(entries ...store.Entry) *Tree {
	s := store.New()
	_ = s.ApplyEntries(entries)
	return Build(s.Snapshot())
}

func TestIdenticalSnapshotsSameRoot(t *testing.T) {
	s1, s2 := store.New(), store.New()
	for i := 0; i < 50; i++ {
		_, _ = s1.Put(key(i), "v", 1)
		_, _ = s2.Put(key(i), "v", 1)
	}
	if Build(s1.Snapshot()).Root() != Build(s2.Snapshot()).Root() {
		t.Fatal("identical snapshots must have the same root")
	}
}

func key(i int) string {
	return "k-" + string(rune('a'+i%26)) + string(rune('a'+(i/26)%26)) + string(rune('a'+(i/676)%26))
}

func TestValueVersionDeleteEachChangeRoot(t *testing.T) {
	base := buildFrom(store.Entry{Key: "x", Value: "a", Version: 1})

	valueChange := buildFrom(store.Entry{Key: "x", Value: "b", Version: 2})
	versionOnly := buildFrom(store.Entry{Key: "x", Value: "a", Version: 2})
	deleted := buildFrom(store.Entry{Key: "x", Version: 2, Deleted: true})

	for name, tr := range map[string]*Tree{
		"value change": valueChange,
		"version bump": versionOnly,
		"tombstone":    deleted,
	} {
		if tr.Root() == base.Root() {
			t.Fatalf("%s did not change the root", name)
		}
	}
	if valueChange.Root() == versionOnly.Root() || versionOnly.Root() == deleted.Root() {
		t.Fatal("different change kinds must produce different roots")
	}
}

func TestChangeFlipsExactlyOnePath(t *testing.T) {
	s1, s2 := store.New(), store.New()
	for i := 0; i < 100; i++ {
		_, _ = s1.Put(key(i), "v", 1)
		_, _ = s2.Put(key(i), "v", 1)
	}
	_, _ = s2.Put(key(42), "v2", 2)
	t1, t2 := Build(s1.Snapshot()), Build(s2.Snapshot())

	b := HashKey(key(42))
	diffLeaves := 0
	for i := 0; i < BucketCount; i++ {
		if t1.LeafHash(i) != t2.LeafHash(i) {
			diffLeaves++
			if i != b {
				t.Fatalf("unexpected differing bucket %d", i)
			}
		}
	}
	if diffLeaves != 1 {
		t.Fatalf("want exactly 1 differing leaf, got %d", diffLeaves)
	}
	// Exactly one node differs at every inner level.
	for level := 1; level < Levels; level++ {
		n := 0
		for i := 0; i < Width(level); i++ {
			if t1.Node(level, i) != t2.Node(level, i) {
				n++
			}
		}
		if n != 1 {
			t.Fatalf("level %d: want 1 differing node, got %d", level, n)
		}
	}
}

func TestEmptyTreeDeterministic(t *testing.T) {
	a := Build(store.Snapshot{})
	b := Build((&store.Store{}).Snapshot())
	if a.Root() != b.Root() {
		t.Fatal("empty snapshots must share a root")
	}
	// A populated tree must not have the empty root.
	s := store.New()
	_, _ = s.Put("k", "v", 1)
	if Build(s.Snapshot()).Root() == a.Root() {
		t.Fatal("populated root equals empty root")
	}
}

func TestOrderIndependence(t *testing.T) {
	s1, s2 := store.New(), store.New()
	_, _ = s1.Put("a", "1", 1)
	_, _ = s1.Put("b", "2", 1)
	_, _ = s2.Put("b", "2", 1)
	_, _ = s2.Put("a", "1", 1)
	if Build(s1.Snapshot()).Root() != Build(s2.Snapshot()).Root() {
		t.Fatal("root must not depend on insertion order")
	}
}
