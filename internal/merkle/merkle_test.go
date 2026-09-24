package merkle

import (
	"strings"
	"testing"

	"github.com/example/merklekv/internal/hlc"
	"github.com/example/merklekv/internal/store"
)

func seedTree(t *testing.T, p Params, entries []store.Entry) (*store.Store, *store.Snapshot, *Tree) {
	t.Helper()
	s := store.New(store.Config{ReplicaID: "r1"})
	s.Seed(entries)
	snap := s.Snapshot()
	return s, snap, Build(p, snap)
}

func TestFixedBucketingIsStable(t *testing.T) {
	p, _ := NewParams(16, 3)
	idx1 := p.BucketIndex("user:42")
	idx2 := p.BucketIndex("user:42")
	if idx1 != idx2 {
		t.Fatal("bucketing is not deterministic")
	}
	for _, k := range []string{"a", "b", "c", "user:1", "user:2"} {
		if idx := p.BucketIndex(k); idx < 0 || idx >= p.NumBuckets() {
			t.Fatalf("bucket for %q out of range: %d", k, idx)
		}
	}
}

func TestEqualContentEqualRoot(t *testing.T) {
	p, _ := NewParams(16, 2)
	entries := []store.Entry{
		{Key: "a", Value: []byte("1"), Ver: hlc.Timestamp{Wall: 1}, Origin: "r1"},
		{Key: "b", Value: []byte("2"), Ver: hlc.Timestamp{Wall: 2}, Origin: "r1"},
	}
	_, _, t1 := seedTree(t, p, entries)
	// Same content, inserted in a different order into a second replica.
	s2 := store.New(store.Config{ReplicaID: "r2"})
	s2.Seed([]store.Entry{entries[1], entries[0]})
	t2 := Build(p, s2.Snapshot())
	if t1.RootHash() != t2.RootHash() {
		t.Fatal("identical content on two replicas must hash to the same root")
	}
}

func TestSingleLeafChangeTouchesOnlyOnePath(t *testing.T) {
	p, _ := NewParams(16, 2) // 256 buckets
	entries := makeEntries(0, 500)
	_, snap1, t1 := seedTree(t, p, entries)

	// Change exactly one key: its bucket's leaf hash and the path to root move.
	entries[7] = store.Entry{Key: entries[7].Key, Value: []byte("changed!"),
		Ver: hlc.Timestamp{Wall: 999}, Origin: "r2"}
	s2 := store.New(store.Config{ReplicaID: "r1"})
	s2.Seed(entries)
	t2 := Build(p, s2.Snapshot())
	if t1.RootHash() == t2.RootHash() {
		t.Fatal("root must change")
	}
	diffLeaves := 0
	var changedBucket int
	for i := 0; i < p.NumBuckets(); i++ {
		if t1.LeafHash(i) != t2.LeafHash(i) {
			diffLeaves++
			changedBucket = i
		}
	}
	if diffLeaves != 1 {
		t.Fatalf("want exactly 1 differing leaf, got %d", diffLeaves)
	}
	if changedBucket != p.BucketIndex(entries[7].Key) {
		t.Fatal("changed bucket does not match key placement")
	}
	// ExpectedRoot with only that leaf replaced must equal the fully rebuilt tree.
	predicted, err := t1.ExpectedRoot(map[int]string{changedBucket: t2.LeafHash(changedBucket)})
	if err != nil {
		t.Fatal(err)
	}
	if predicted != t2.RootHash() {
		t.Fatal("ExpectedRoot prediction does not match rebuilt tree")
	}
	_ = snap1
}

func TestTombstoneAndVersionChangeHash(t *testing.T) {
	p, _ := NewParams(4, 1)
	live := []store.Entry{{Key: "k", Value: []byte("v"), Ver: hlc.Timestamp{Wall: 1}, Origin: "r1"}}
	olderTomb := []store.Entry{{Key: "k", Deleted: true, Ver: hlc.Timestamp{Wall: 0}, Origin: "r1"}}
	newerLive := []store.Entry{{Key: "k", Value: []byte("v"), Ver: hlc.Timestamp{Wall: 2}, Origin: "r1"}}
	_, _, t1 := seedTree(t, p, live)
	_, _, t2 := seedTree(t, p, olderTomb)
	_, _, t3 := seedTree(t, p, newerLive)
	if t1.RootHash() == t2.RootHash() {
		t.Fatal("tombstone with different version must hash differently")
	}
	if t1.RootHash() == t3.RootHash() {
		t.Fatal("version bump without value change must still hash differently")
	}
}

func TestPathNavigation(t *testing.T) {
	p, _ := NewParams(16, 3)
	entries := makeEntries(0, 300)
	_, _, tr := seedTree(t, p, entries)

	root, err := tr.Node("")
	if err != nil || root.Level != 0 || len(root.Children) != 16 {
		t.Fatalf("root node wrong: %+v err=%v", root, err)
	}
	for _, idx := range []int{0, 1, 4095, 1234} {
		digits := p.BucketPath(idx)
		n, err := tr.Node(JoinPath(digits))
		if err != nil {
			t.Fatal(err)
		}
		if !n.Leaf || n.Bucket != idx {
			t.Fatalf("path %v did not resolve to leaf bucket %d: %+v", digits, idx, n)
		}
		if n.Hash != tr.LeafHash(idx) {
			t.Logf("note: bucket %d holds %d entries", idx, n.Count)
		}
	}
	if _, err := tr.Node("16/0"); err == nil {
		t.Fatal("out-of-fanout path should error")
	}
	if _, err := tr.Node(strings.Repeat("0/", 4) + "0"); err == nil {
		t.Fatal("too-deep path should error")
	}
}

func TestEmptyTreeRootStable(t *testing.T) {
	p, _ := NewParams(8, 2)
	_, _, t1 := seedTree(t, p, nil)
	_, _, t2 := seedTree(t, p, nil)
	if t1.RootHash() != t2.RootHash() {
		t.Fatal("empty trees on different replicas must agree")
	}
}

// makeEntries builds keys key-0000.. with values of the given byte size.
func makeEntries(start, n int) []store.Entry {
	out := make([]store.Entry, n)
	for i := range out {
		out[i] = store.Entry{
			Key:    "key-" + itoa4(start+i),
			Value:  []byte("v"),
			Ver:    hlc.Timestamp{Wall: uint64(start + i + 1)},
			Origin: "r1",
		}
	}
	return out
}

func itoa4(i int) string {
	digits := []byte("0000")
	for j := 3; j >= 0; j-- {
		digits[j] = byte('0' + i%10)
		i /= 10
	}
	return string(digits)
}
