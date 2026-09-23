package register

import (
	"errors"
	"testing"

	"vccsim/internal/clock"
)

func ctx(id string, vc clock.VectorClock) ContextEntry {
	return ContextEntry{ID: id, VC: vc}
}

// Offline divergent writes at two replicas must produce two concurrent
// siblings; convergence must keep both.
func TestOfflineDivergenceKeepsSiblings(t *testing.T) {
	a := New("A")
	b := New("B")

	va := a.LocalWrite("k", []byte(`"a-write"`))
	vb := b.LocalWrite("k", []byte(`"b-write"`))

	if clock.Compare(va.VC, vb.VC) != clock.RelConcurrent {
		t.Fatalf("offline writes should be concurrent: %v vs %v", va.VC, vb.VC)
	}
	if added := b.Ingest("k", va); !added {
		t.Fatal("A's divergent write should be added at B")
	}
	if added := a.Ingest("k", vb); !added {
		t.Fatal("B's divergent write should be added at A")
	}
	if got := len(a.Snapshot("k")); got != 2 {
		t.Fatalf("A has %d siblings, want 2 (no silent loss)", got)
	}
	if got := len(b.Snapshot("k")); got != 2 {
		t.Fatalf("B has %d siblings, want 2 (no silent loss)", got)
	}
}

// Duplicate delivery must be idempotent.
func TestDuplicateDeliveryIdempotent(t *testing.T) {
	a := New("A")
	b := New("B")
	v := a.LocalWrite("k", []byte(`1`))
	for i := 0; i < 3; i++ {
		if b.Ingest("k", v) != (i == 0) {
			t.Fatalf("delivery #%d had wrong idempotence", i+1)
		}
	}
	if got := len(b.Snapshot("k")); got != 1 {
		t.Fatalf("B has %d versions, want 1", got)
	}
}

// A causally older delivery is pruned by a known descendant.
func TestOldVersionPruned(t *testing.T) {
	a := New("A")
	v1 := a.LocalWrite("k", []byte(`1`))
	v2 := a.LocalWrite("k", []byte(`2`))

	b := New("B")
	if !b.Ingest("k", v2) {
		t.Fatal("newer version should be accepted")
	}
	if b.Ingest("k", v1) {
		t.Fatal("stale ancestor must not resurrect as a sibling")
	}
	if got := len(b.Snapshot("k")); got != 1 || b.Snapshot("k")[0].ID != v2.ID {
		t.Fatalf("snapshot = %v, want only %s", b.Snapshot("k"), v2.ID)
	}
}

// A valid merge covering every sibling applies and collapses the sibling set.
func TestMergeWriteWithFullContext(t *testing.T) {
	a := New("A")
	b := New("B")
	va := a.LocalWrite("k", []byte(`"a"`))
	vb := b.LocalWrite("k", []byte(`"b"`))
	a.Ingest("k", vb)
	b.Ingest("k", va)

	// Client read both siblings at A and merged them.
	merged, err := a.MergeWrite(MergeRequest{
		Node:  "A",
		Key:   "k",
		Value: []byte(`"ab"`),
		Context: []ContextEntry{
			ctx(va.ID, va.VC),
			ctx(vb.ID, vb.VC),
		},
	})
	if err != nil {
		t.Fatalf("full-context merge rejected: %v", err)
	}
	if got := a.Snapshot("k"); len(got) != 1 || got[0].ID != merged.ID {
		t.Fatalf("after merge = %v, want single merged version", got)
	}

	// Propagating the merged version collapses the set elsewhere too.
	if b.Ingest("k", merged) {
		if got := len(b.Snapshot("k")); got != 1 {
			t.Fatalf("B after merge propagation has %d siblings, want 1", got)
		}
	}
}

// A merge missing a concurrent sibling is rejected (no silent overwrite).
func TestMergeWriteMissingConcurrentSibling(t *testing.T) {
	a := New("A")
	b := New("B")
	va := a.LocalWrite("k", []byte(`"a"`))
	vb := b.LocalWrite("k", []byte(`"b"`))
	b.Ingest("k", va)

	_, err := b.MergeWrite(MergeRequest{
		Node:    "B",
		Key:     "k",
		Value:   []byte(`"only-saw-b"`),
		Context: []ContextEntry{ctx(vb.ID, vb.VC)}, // never saw A's write
	})
	var me *MergeError
	if !errors.As(err, &me) || !errors.Is(err, ErrConcurrentNotCovered) {
		t.Fatalf("want ErrConcurrentNotCovered, got %v", err)
	}
	if got := len(b.Snapshot("k")); got != 2 {
		t.Fatalf("rejected merge must leave both siblings, got %d", got)
	}
}

// A merge built on a strictly stale ancestor is rejected as stale.
func TestMergeWriteStaleAncestor(t *testing.T) {
	b := New("B")
	v1 := b.LocalWrite("k", []byte(`"old"`))
	v2 := b.LocalWrite("k", []byte(`"new"`)) // v1 pruned locally

	// Client read v1, went away, now writes with the old context while v2 lives.
	_, err := b.MergeWrite(MergeRequest{
		Node:    "B",
		Key:     "k",
		Value:   []byte(`"from-old-read"`),
		Context: []ContextEntry{{ID: v1.ID, VC: v1.VC}},
	})
	if !errors.Is(err, ErrStaleContext) {
		t.Fatalf("want ErrStaleContext, got %v", err)
	}
	if got := b.Snapshot("k"); len(got) != 1 || got[0].ID != v2.ID {
		t.Fatalf("rejected stale merge must preserve v2, got %v", got)
	}
}

// An unknown context id without a resolvable clock is rejected.
func TestMergeWriteUnknownContext(t *testing.T) {
	b := New("B")
	b.LocalWrite("k", []byte(`"x"`))
	_, err := b.MergeWrite(MergeRequest{
		Node:    "B",
		Key:     "k",
		Value:   []byte(`"y"`),
		Context: []ContextEntry{{ID: "Z:9"}},
	})
	if !errors.Is(err, ErrUnknownContext) {
		t.Fatalf("want ErrUnknownContext, got %v", err)
	}
}
