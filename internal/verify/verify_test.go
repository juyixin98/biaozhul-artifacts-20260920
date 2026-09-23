package verify

import (
	"context"
	"testing"

	"nodesync/internal/chain"
	"nodesync/internal/gen"
	"nodesync/internal/sample"
	"nodesync/internal/store"
)

func fixtures(t *testing.T, n int) ([]chain.Block, *sample.Sample) {
	t.Helper()
	blocks := gen.Build(gen.Spec{Length: n, Seed: []byte("seed"), BodyNonce: []byte("nonce")})
	s := sample.FromBlocks(blocks, []byte("seed"), []byte("nonce"), "now")
	return blocks, s
}

func TestSummaryMatchesFullChain(t *testing.T) {
	blocks, s := fixtures(t, 16)
	st, err := store.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	if err := st.InitializeGenesis(context.Background(), blocks[0]); err != nil {
		t.Fatal(err)
	}
	if err := st.AppendVerified(context.Background(), blocks[1:]); err != nil {
		t.Fatal(err)
	}
	sum, _, ok, err := ChainSummary(context.Background(), st, s)
	if err != nil {
		t.Fatal(err)
	}
	if !ok || !sum.MatchesSample {
		t.Fatalf("expected match, summary=%+v", sum)
	}
	if sum.TipHash != s.TipHash || sum.Digest != s.Digest {
		t.Fatal("tip/digest mismatch")
	}
}

func TestSummaryRejectsPartialPrefix(t *testing.T) {
	blocks, s := fixtures(t, 16)
	st, err := store.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	if err := st.InitializeGenesis(context.Background(), blocks[0]); err != nil {
		t.Fatal(err)
	}
	if err := st.AppendVerified(context.Background(), blocks[1:9]); err != nil {
		t.Fatal(err)
	}
	sum, _, ok, err := ChainSummary(context.Background(), st, s)
	if err != nil {
		t.Fatal(err)
	}
	if ok || sum.MatchesSample {
		t.Fatalf("partial prefix must not match, got %+v", sum)
	}
}

func TestSampleRoundTripAndTamperDetection(t *testing.T) {
	blocks, s := fixtures(t, 8)
	tmp := t.TempDir() + "/sample.json"
	if err := s.Save(tmp); err != nil {
		t.Fatal(err)
	}
	loaded, err := sample.Load(tmp)
	if err != nil {
		t.Fatal(err)
	}
	got, err := loaded.ToBlocks()
	if err != nil {
		t.Fatalf("round trip: %v", err)
	}
	if len(got) != len(blocks) {
		t.Fatalf("block count %d", len(got))
	}

	// Tamper one body and keep the stored digest stale -> load must reject.
	s.Blocks[4].Body = "ff"
	badTmp := t.TempDir() + "/bad.json"
	if err := s.Save(badTmp); err != nil {
		t.Fatal(err)
	}
	bad, err := sample.Load(badTmp)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := bad.ToBlocks(); err == nil {
		t.Fatal("expected tampered sample to be rejected")
	}
}
