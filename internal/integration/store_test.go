package integration

import (
	"context"
	"errors"
	"testing"

	"forkindexer/internal/domain"
	"forkindexer/internal/pgstore"
)

func TestLinearChainAndGenesisRebuild(t *testing.T) {
	s := newTestStore(t)
	b := newChainBuilder(t)
	sc := b.threeForks()

	// Deliver only the B fork in order.
	deliver(t, s, sc.G, 1)
	deliver(t, s, sc.B1, 2)
	deliver(t, s, sc.B2, 3)
	deliver(t, s, sc.B3, 4)

	v, err := s.Head(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if v.Head != sc.B3.Hash || v.Height != 3 || v.CumWeight != 4 || v.Cursor != 4 {
		t.Fatalf("unexpected head view: %+v", v)
	}
	got := balancesOf(t, s, addrA, addrB, addrD)
	// B chain G(+1000 bob), B1(-40 bob +40 dave), B2(-5 dave +5 alice),
	// B3(-200 alice +200 bob):
	// alice: +5 -200 = -195 ... plus genesis? genesis alice is sender only in
	// three-fork scenario: alice -1000 +5 -200 = -1195
	// bob: 1000 - 40 + 200 = 1160 ; dave: 40 - 5 = 35
	want := map[string]int64{addrA: -1195, addrB: 1160, addrD: 35}
	for k, wv := range want {
		if got[k] != wv {
			t.Fatalf("balance %s: got %d want %d", k, got[k], wv)
		}
	}
	mustVerifyOK(t, s)
}

func TestOrphanStagingThenCascadeConnect(t *testing.T) {
	s := newTestStore(t)
	b := newChainBuilder(t)
	sc := b.threeForks()

	// B3 before B2/B1: must stage and not crash, head remains empty/old.
	deliver(t, s, sc.B3, 1)
	v, _ := s.Head(context.Background())
	if v.Head != "" || v.Cursor != 1 {
		t.Fatalf("orphan before genesis: head=%q cursor=%d", v.Head, v.Cursor)
	}
	deliver(t, s, sc.G, 2)
	deliver(t, s, sc.B1, 3)
	// B3 still staged (missing B2).
	v, _ = s.Head(context.Background())
	if v.Head != sc.B1.Hash {
		t.Fatalf("after B1 head should be B1, got %q", v.Head)
	}
	// B2 arrives -> B3 cascades and becomes head.
	res := deliver(t, s, sc.B2, 4)
	if res.HeadAfter != sc.B3.Hash || !res.Reorg {
		t.Fatalf("cascade did not promote B3: %+v", res)
	}
	mustVerifyOK(t, s)
}

func TestThreeForkCumulativeWeightAndReorg(t *testing.T) {
	s := newTestStore(t)
	b := newChainBuilder(t)
	sc := b.threeForks()
	var seq int64 = 1
	ing := func(blk *domain.Block) pgstore.IngestResult {
		r := deliver(t, s, blk, seq)
		seq++
		return r
	}

	ing(sc.G)
	ing(sc.A1)
	ing(sc.B1)
	r := ing(sc.A2)
	// G-A1-A2 is weight 3 while G-B1 is weight 2: head moves from B1 to A2.
	// Whether it is reported as reorg is incidental; assert the chosen head.
	if r.HeadAfter != sc.A2.Hash {
		t.Fatalf("after A2 head must be A2 (weight3 over weight2): %+v", r)
	}
	// B2 arrives: B chain weight 3 ties A chain weight 3. Tips hashes decide.
	r = ing(sc.B2)
	winner := tieWinner([]string{sc.A2.Hash, sc.B2.Hash})
	if r.HeadAfter != winner {
		t.Fatalf("tie at weight 3: expected %s got %s", winner, r.HeadAfter)
	}
	// C2 third fork, also weight 3: the winner is now the min hash of all
	// three coexisting weight-3 tips.
	r = ing(sc.C2)
	winner = tieWinner([]string{sc.A2.Hash, sc.B2.Hash, sc.C2.Hash})
	if r.HeadAfter != winner {
		t.Fatalf("C2 weight-3 three-way tie: expected %s got %s", winner, r.HeadAfter)
	}
	r = ing(sc.B3)
	if r.HeadAfter != sc.B3.Hash {
		t.Fatalf("B3 weight 4 must be final head, got %s", r.HeadAfter)
	}
	v, _ := s.Head(context.Background())
	if v.CumWeight != 4 || v.Height != 3 {
		t.Fatalf("final head view: %+v", v)
	}
	// Off-chain blocks remain stored and queryable.
	view, err := s.Block(context.Background(), sc.A2.Hash)
	if err != nil {
		t.Fatal(err)
	}
	if view.InChain || view.Status != "connected" {
		t.Fatalf("A2 must be connected but off-chain: %+v", view)
	}
	// carol only ever received funds on the A fork; after B wins she is 0.
	bal, head, err := s.Balance(context.Background(), addrC)
	if err != nil {
		t.Fatal(err)
	}
	if bal != 0 || head != sc.B3.Hash {
		t.Fatalf("carol=%d head=%s", bal, head)
	}
	mustVerifyOK(t, s)
}

func tieWinner(hs []string) string {
	min := hs[0]
	for _, h := range hs[1:] {
		if h < min {
			min = h
		}
	}
	return min
}

func TestReorgBalanceUndoThenApply(t *testing.T) {
	s := newTestStore(t)
	b := newChainBuilder(t)

	// G is empty. Fork X moves 300 A->B; fork Y moves 300 A->C then 100 C->D.
	// The model has no overdraft check, so A may go negative.
	G := b.block(domain.ZeroHash, 0, nil)
	X1 := b.block(G.Hash, 1, []domain.Transfer{{From: addrA, To: addrB, Amount: 300}})
	Y1 := b.block(G.Hash, 1, []domain.Transfer{{From: addrA, To: addrC, Amount: 300}})
	Y2 := b.block(Y1.Hash, 2, []domain.Transfer{{From: addrC, To: addrD, Amount: 100}})

	deliver(t, s, G, 1)
	deliver(t, s, X1, 2) // head X1 weight 2
	if v, _ := s.Head(context.Background()); v.Head != X1.Hash {
		t.Fatalf("X1 head expected, got %q", v.Head)
	}
	if got := balancesOf(t, s, addrA, addrB); got[addrB] != 300 || got[addrA] != -300 {
		t.Fatalf("post X1 balances wrong: %v", got)
	}
	deliver(t, s, Y1, 3) // tie weight 2; pick deterministic
	deliver(t, s, Y2, 4) // Y weight 3 overtakes X
	if v, _ := s.Head(context.Background()); v.Head != Y2.Hash {
		t.Fatal("Y2 must overtake X1")
	}
	got := balancesOf(t, s, addrA, addrB, addrC, addrD)
	want := map[string]int64{addrA: -300, addrB: 0, addrC: 200, addrD: 100}
	for k, wv := range want {
		if got[k] != wv {
			t.Fatalf("after reorg %s got %d want %d (all %v)", k, got[k], wv, got)
		}
	}
	mustVerifyOK(t, s)
}

func TestDuplicateBlockIsNotDoubleCounted(t *testing.T) {
	s := newTestStore(t)
	b := newChainBuilder(t)
	sc := b.threeForks()
	deliver(t, s, sc.G, 1)
	deliver(t, s, sc.B1, 2)
	deliver(t, s, sc.B2, 3)
	deliver(t, s, sc.B3, 4)
	before := balancesOf(t, s, addrA, addrB, addrD)

	// Replay identical envelopes with the SAME sequences: must be accepted as
	// idempotent retries, never applied twice.
	for i, blk := range []*domain.Block{sc.G, sc.B1, sc.B2, sc.B3} {
		res := deliver(t, s, blk, int64(i+1))
		if !res.Known {
			t.Fatalf("replay of seq %d not marked already_known", i+1)
		}
	}
	after := balancesOf(t, s, addrA, addrB, addrD)
	for k := range before {
		if before[k] != after[k] {
			t.Fatalf("balance changed after replay: %s %d -> %d", k, before[k], after[k])
		}
	}
	v, _ := s.Head(context.Background())
	if v.Cursor != 4 {
		t.Fatalf("cursor moved on replay: %d", v.Cursor)
	}

	// Same block again WITHOUT a sequence (bare): idempotent as well.
	res := deliver(t, s, sc.B3, 0)
	if !res.Known {
		t.Fatal("bare redelivery not recognized")
	}
	mustVerifyOK(t, s)
}

func TestSequenceConflicts(t *testing.T) {
	s := newTestStore(t)
	b := newChainBuilder(t)
	sc := b.threeForks()
	deliver(t, s, sc.G, 1)

	// Gap: sequence 3 while cursor is 1.
	if err := deliverErr(t, s, sc.B1, 3); !errors.Is(err, domain.ErrSequenceGap) {
		t.Fatalf("expected ErrSequenceGap, got %v", err)
	}
	// Reuse a consumed sequence for a DIFFERENT block.
	other := b.block(sc.G.Hash, 1, []domain.Transfer{{From: addrA, To: addrC, Amount: 7}})
	if err := deliverErr(t, s, other, 1); !errors.Is(err, domain.ErrSequenceTooSmall) {
		t.Fatalf("expected ErrSequenceTooSmall, got %v", err)
	}
	// Same sequence same block -> fine.
	deliver(t, s, sc.G, 1)
	mustVerifyOK(t, s)
}

func TestRejectedRulesAndDescendants(t *testing.T) {
	s := newTestStore(t)
	b := newChainBuilder(t)
	sc := b.threeForks()

	// Height != parent+1: craft a block claiming height 9 under genesis.
	rogue := b.block(sc.G.Hash, 9, []domain.Transfer{{From: addrA, To: addrB, Amount: 1}})
	child := b.block(rogue.Hash, 10, nil)
	deliver(t, s, sc.G, 1)
	res := deliver(t, s, rogue, 2)
	if res.BlockStatus != "rejected" {
		t.Fatalf("rogue should be rejected, got %s", res.BlockStatus)
	}
	res = deliver(t, s, child, 3)
	if res.BlockStatus != "rejected" {
		t.Fatalf("descendant of rejected block must reject, got %s", res.BlockStatus)
	}
	// Zero-parent block at non-zero height rejected; does not poison others.
	fake := b.block(domain.ZeroHash, 2, nil)
	res = deliver(t, s, fake, 4)
	if res.BlockStatus != "rejected" {
		t.Fatalf("zero-parent height-2 must reject, got %s", res.BlockStatus)
	}
	deliver(t, s, sc.B1, 5) // normal chain still connects
	v, _ := s.Head(context.Background())
	if v.Head != sc.B1.Hash {
		t.Fatalf("normal head after rejects: %q", v.Head)
	}
	mustVerifyOK(t, s)
}

func TestHashMismatchRejected(t *testing.T) {
	s := newTestStore(t)
	b := newChainBuilder(t)
	sc := b.threeForks()
	// A forged declared hash is rejected at the protocol boundary
	// (Normalize), before the store is touched.
	forged := *sc.G
	forged.Hash = "0x" + repeat('9', 64)
	if err := domain.Normalize(&forged); !errors.Is(err, domain.ErrHashMismatch) {
		t.Fatalf("normalize expected hash mismatch, got %v", err)
	}
	if err := deliverErr(t, s, sc.G, 1); err != nil {
		t.Fatalf("valid genesis should ingest: %v", err)
	}
}

func TestSameHashDifferentContentAtStore(t *testing.T) {
	s := newTestStore(t)
	ctx := context.Background()
	b := newChainBuilder(t)
	sc := b.threeForks()
	deliver(t, s, sc.G, 1)

	// Defensive check: if the stored raw bytes for a known hash ever diverge
	// from the delivered canonical body, ingest must refuse. A brand-new
	// sequence is used so the request does not take the idempotent-replay
	// shortcut, exercising the raw-byte comparison directly.
	if _, err := s.DB().ExecContext(ctx,
		"UPDATE blocks SET raw=$2 WHERE hash=$1", sc.G.Hash, []byte(`{"tampered":true}`)); err != nil {
		t.Fatal(err)
	}
	if err := deliverErr(t, s, sc.G, 2); !errors.Is(err, domain.ErrSameHashDifferentContent) {
		t.Fatalf("expected same-hash-different-content, got %v", err)
	}
}

func TestIncrementalEqualsRebuildAcrossForks(t *testing.T) {
	// Headline acceptance: stream a shuffled sequence over three forks and
	// assert after EVERY delivery that incremental balances equal an
	// independent from-genesis rebuild.
	s := newTestStore(t)
	b := newChainBuilder(t)
	sc := b.threeForks()

	// Extend forks further: A chain to height 4, C chain height 3, B to 5.
	X3 := b.block(sc.A2.Hash, 3, []domain.Transfer{{From: addrB, To: addrC, Amount: 33}})
	X4 := b.block(X3.Hash, 4, []domain.Transfer{{From: addrC, To: addrA, Amount: 33}})
	D1 := b.block(sc.C2.Hash, 3, []domain.Transfer{{From: addrD, To: addrB, Amount: 12}})
	B4 := b.block(sc.B3.Hash, 4, []domain.Transfer{{From: addrB, To: addrD, Amount: 60}})
	B5 := b.block(B4.Hash, 5, []domain.Transfer{{From: addrA, To: addrC, Amount: 8}})

	// Shuffled delivery: B3 arrives as orphan before G; extensions arrive
	// before their parents too.
	stream := []*domain.Block{
		sc.B3, // orphan
		sc.G,
		X3, // orphan under A2 (A2 not yet delivered)
		sc.A1,
		sc.B1,
		X4,    // orphan
		sc.A2, // now X3 cascades
		sc.B2, // B3 cascades, reorg
		sc.C2,
		D1, // C chain height 3
		B4,
		B5, // final winner weight 6
	}
	for seq, blk := range stream {
		deliver(t, s, blk, int64(seq+1))
		mustVerifyOK(t, s) // invariant after every single delivery
	}

	final, _ := s.Head(context.Background())
	if final.Head != B5.Hash || final.CumWeight != 6 || final.Height != 5 {
		t.Fatalf("final head want B5 weight6/height5, got %s (w=%d h=%d)",
			final.Head, final.CumWeight, final.Height)
	}

	// Replay every envelope: still equal to rebuild, cursor unchanged.
	for seq, blk := range stream {
		deliver(t, s, blk, int64(seq+1))
	}
	mustVerifyOK(t, s)
	if v, _ := s.Head(context.Background()); v.Cursor != int64(len(stream)) {
		t.Fatalf("cursor changed on replay: %d", v.Cursor)
	}
}

func TestBatchAtomicity(t *testing.T) {
	s := newTestStore(t)
	b := newChainBuilder(t)
	sc := b.threeForks()
	envs := []domain.Envelope{
		{Sequence: 1, Block: *sc.G},
		{Sequence: 2, Block: *sc.B1},
		{Sequence: 9, Block: *sc.B2}, // gap -> whole batch must abort
	}
	if _, err := s.IngestBatch(context.Background(), envs); err == nil {
		t.Fatal("expected batch error")
	}
	v, _ := s.Head(context.Background())
	if v.Cursor != 0 || v.Head != "" {
		t.Fatalf("batch must be atomic, got head=%q cursor=%d", v.Head, v.Cursor)
	}
	mustVerifyOK(t, s)
}

func TestUnknownBlockAndAddressQueries(t *testing.T) {
	s := newTestStore(t)
	if _, err := s.Block(context.Background(), "0x"+repeat('e', 64)); !errors.Is(err, pgstore.ErrNotFound) {
		t.Fatalf("expected ErrNotFound, got %v", err)
	}
	// Unknown address balance is 0 with empty head.
	v, head, err := s.Balance(context.Background(), addrA)
	if err != nil || v != 0 || head != "" {
		t.Fatalf("unknown balance: v=%d head=%q err=%v", v, head, err)
	}
}

func repeat(c byte, n int) string {
	b := make([]byte, n)
	for i := range b {
		b[i] = c
	}
	return string(b)
}
