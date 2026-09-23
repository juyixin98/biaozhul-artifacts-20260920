package store

import (
	"errors"
	"testing"

	"github.com/example/snapshotprune/internal/types"
)

func TestBlockDeltaRoundTripAndTip(t *testing.T) {
	st, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()

	gen := &types.Block{Header: types.Header{Height: 0, StateRoot: types.Hash{1}}}
	if err := st.PutGenesis(gen); err != nil {
		t.Fatal(err)
	}
	if tip, _ := st.Tip(); tip != 0 {
		t.Fatalf("tip after genesis: %d", tip)
	}

	b := &types.Block{Header: types.Header{Height: 1, ParentHash: gen.Hash(), StateRoot: types.Hash{2}}}
	d := &types.Delta{Height: 1}
	if err := st.PutBlockAndDelta(b, d); err != nil {
		t.Fatal(err)
	}
	if tip, _ := st.Tip(); tip != 1 {
		t.Fatalf("tip: %d", tip)
	}
	got, err := st.Block(1)
	if err != nil || got.Hash() != b.Hash() {
		t.Fatalf("block round trip: %v %v", got, err)
	}
	ok, err := st.HasDelta(1)
	if err != nil || !ok {
		t.Fatalf("HasDelta: %v %v", ok, err)
	}
	if _, err := st.Block(99); !errors.Is(err, ErrNotFound) {
		t.Fatalf("expected ErrNotFound, got %v", err)
	}
}

func TestPruneDeltasAndLowest(t *testing.T) {
	st, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	_ = st.PutGenesis(&types.Block{Header: types.Header{Height: 0}})
	for h := uint64(1); h <= 6; h++ {
		b := &types.Block{Header: types.Header{Height: h}}
		if err := st.PutBlockAndDelta(b, &types.Delta{Height: h}); err != nil {
			t.Fatal(err)
		}
	}
	n, err := st.PruneDeltas(4)
	if err != nil || n != 4 {
		t.Fatalf("PruneDeltas: n=%d err=%v", n, err)
	}
	for h := uint64(1); h <= 4; h++ {
		if ok, _ := st.HasDelta(h); ok {
			t.Fatalf("delta %d survived prune", h)
		}
	}
	if ok, _ := st.HasDelta(5); !ok {
		t.Fatal("delta 5 should remain")
	}
	if ok, _ := st.HasDelta(0); !ok {
		t.Fatal("genesis delta 0 must never be pruned")
	}
	low, ok, err := st.LowestDeltaHeight()
	if err != nil || !ok || low != 0 {
		t.Fatalf("LowestDeltaHeight: low=%d ok=%v err=%v", low, ok, err)
	}
}

func TestSnapshotReaderIsPointInTime(t *testing.T) {
	st, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	_ = st.PutGenesis(&types.Block{Header: types.Header{Height: 0}})
	for h := uint64(1); h <= 3; h++ {
		_ = st.PutBlockAndDelta(&types.Block{Header: types.Header{Height: h}}, &types.Delta{Height: h})
	}
	rd, done, err := st.NewSnapshotReader()
	if err != nil {
		t.Fatal(err)
	}
	defer done()
	if tip, _ := rd.Tip(); tip != 3 {
		t.Fatalf("snapshot tip: %d", tip)
	}
	// Append after snapshotting the DB: reader must stay at 3.
	_ = st.PutBlockAndDelta(&types.Block{Header: types.Header{Height: 4}}, &types.Delta{Height: 4})
	if tip, _ := rd.Tip(); tip != 3 {
		t.Fatalf("point-in-time reader saw new tip %d", tip)
	}
}
