package core_test

import (
	"context"
	"testing"

	"inbox/internal/core"
	"inbox/internal/crypto"
	"inbox/internal/fixtures"
	"inbox/internal/merkle"
)

// TestGapFillAndExactlyOnce covers out-of-order arrival, staging beyond
// the watermark, gap filling, confirmation gating and exactly-once.
func TestGapFillAndExactlyOnce(t *testing.T) {
	h := newHarness(t)
	ex := h.Exec
	ctx := context.Background()
	cf := fixtures.Chains[fixtures.ChainA]
	alice := cf.Senders[0]

	if err := ex.OpenChannel(ctx, cf.ID, "orders", []key{alice.Pub}); err != nil {
		t.Fatal(err)
	}

	b := fixtures.NewChainBuilder(cf)
	blk1 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "orders", Nonce: 1, Sender: alice, Payload: []byte("one")}})
	blk2 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "orders", Nonce: 2, Sender: alice, Payload: []byte("two")}})
	blk3 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "orders", Nonce: 3, Sender: alice, Payload: []byte("three")}})
	blk4 := b.EmptyBlock()
	blk5 := b.EmptyBlock()

	submitBlock(t, ex, blk1)
	submitBlock(t, ex, blk2)
	submitBlock(t, ex, blk3)

	// Arrival order 1, 3, ... 2: nonce 3 is staged ahead of the watermark.
	submitMsg(t, ex, blk1.Messages[0]) // block1 confirmed (tip 3, conf 2)
	submitMsg(t, ex, blk3.Messages[0]) // block3 unconfirmed -> candidate

	n, err := ex.ProcessAll(ctx)
	if err != nil || n != 1 {
		t.Fatalf("first tick: n=%d err=%v, want 1", n, err)
	}

	submitMsg(t, ex, blk2.Messages[0]) // fills the key gap, but block2 unconfirmed
	if n, _ := ex.ProcessAll(ctx); n != 0 {
		t.Fatalf("block2 unconfirmed: delivered %d, want 0", n)
	}

	submitBlock(t, ex, blk4) // tip 4 -> block2 confirmed
	if n, _ := ex.ProcessAll(ctx); n != 1 {
		t.Fatalf("after block4: delivered %d, want 1 (nonce2)", n)
	}

	submitBlock(t, ex, blk5) // tip 5 -> block3 confirmed
	if n, _ := ex.ProcessAll(ctx); n != 1 {
		t.Fatalf("after block5: delivered %d, want 1 (nonce3)", n)
	}

	got := deliveriesOf(t, ex, cf.ID, "orders")
	if len(got) != 3 {
		t.Fatalf("deliveries=%d, want 3", len(got))
	}
	for i, d := range got {
		if d.Nonce != uint64(i+1) {
			t.Fatalf("delivery %d has nonce %d, want in-order %d", i, d.Nonce, i+1)
		}
		if d.Attempts != 1 {
			t.Fatalf("nonce %d attempts=%d, want 1 (exactly-once)", d.Nonce, d.Attempts)
		}
	}

	// Identical duplicate must be idempotent and must not re-execute.
	submitMsg(t, ex, blk1.Messages[0])
	if n, _ := ex.ProcessAll(ctx); n != 0 {
		t.Fatalf("duplicate re-delivered %d messages", n)
	}
	if len(deliveriesOf(t, ex, cf.ID, "orders")) != 3 {
		t.Fatal("duplicate created an extra delivery")
	}

	// Channel watermark advanced past the prefix.
	cs, err := ex.GetChannel(ctx, cf.ID, "orders")
	if err != nil || cs.NextNonce != 4 || cs.Status != "open" {
		t.Fatalf("channel state: %+v err=%v", cs, err)
	}
}

// TestReorgCancelsCandidates covers pre-finality revocation: candidate
// messages under a revoked tip are cancelled, replacement block executes.
func TestReorgCancelsCandidates(t *testing.T) {
	h := newHarness(t)
	ex := h.Exec
	ctx := context.Background()
	cf := fixtures.Chains[fixtures.ChainB]
	dave := cf.Senders[0]

	if err := ex.OpenChannel(ctx, cf.ID, "payments", []key{dave.Pub}); err != nil {
		t.Fatal(err)
	}
	b := fixtures.NewChainBuilder(cf)
	blk1 := b.BuildBlock(nil)
	tip2 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "payments", Nonce: 1, Sender: dave, Payload: []byte("old")}})
	submitBlock(t, ex, blk1)
	submitBlock(t, ex, tip2)
	submitMsg(t, ex, tip2.Messages[0])

	// Non-final candidate is not executable.
	if n, _ := ex.ProcessAll(ctx); n != 0 {
		t.Fatalf("unconfirmed candidate executed: %d", n)
	}

	pub, sig := b.Revocation(tip2.Block)
	if err := ex.SubmitRevocation(ctx, core.Revocation{
		ChainID: cf.ID, Block: tip2.Block, Validator: pub, Signature: sig,
	}); err != nil {
		t.Fatal(err)
	}

	msgs, err := ex.ListMessages(ctx, cf.ID, "payments")
	if err != nil || len(msgs) != 1 || msgs[0].Status != "cancelled" {
		t.Fatalf("old candidate should be cancelled: %+v err=%v", msgs, err)
	}

	// The old proof is now anchored at a non-canonical block.
	err = ex.SubmitMessage(ctx, core.Message{
		ChainID: cf.ID, ChannelID: "payments", Nonce: 1,
		PayloadHash: tip2.Messages[0].PayloadHash, Block: tip2.Block,
		SenderPub: dave.Pub, SenderSig: tip2.Messages[0].SenderSig,
	}, tip2.Messages[0].Proof)
	mustCode(t, err, core.CodeBlockNotCanonical)

	// A revoked block cannot be revoked again.
	err = ex.SubmitRevocation(ctx, core.Revocation{ChainID: cf.ID, Block: tip2.Block, Validator: pub, Signature: sig})
	mustCode(t, err, core.CodeTipNotCurrent)

	// Replacement chain: new block2 then two extensions.
	fork := fixtures.NewForkBuilder(cf, blk1.Block, blk1.Height)
	newBlk2 := fork.BuildBlock([]fixtures.MsgSpec{{ChannelID: "payments", Nonce: 1, Sender: dave, Payload: []byte("new")}})
	cont := fixtures.NewForkBuilder(cf, newBlk2.Block, newBlk2.Height)
	blk3 := cont.BuildBlock(nil)
	blk4 := fixtures.NewForkBuilder(cf, blk3.Block, blk3.Height).BuildBlock(nil)
	submitBlock(t, ex, newBlk2)
	submitMsg(t, ex, newBlk2.Messages[0])
	submitBlock(t, ex, blk3)
	if n, _ := ex.ProcessAll(ctx); n != 0 {
		t.Fatal("replacement at depth 1 must not execute")
	}
	submitBlock(t, ex, blk4) // depth 2 -> confirmed
	if n, _ := ex.ProcessAll(ctx); n != 1 {
		t.Fatalf("replacement delivery: %d, want 1", n)
	}

	got := deliveriesOf(t, ex, cf.ID, "payments")
	if len(got) != 1 || got[0].Nonce != 1 || !equalHash(got[0].BlockHex, newBlk2.Block) {
		t.Fatalf("delivery must reference replacement block: %+v", got)
	}

	// A warning alert was recorded for the reorg.
	alerts, _ := ex.ListAlerts(ctx, cf.ID, "warning", 10)
	found := false
	for _, a := range alerts {
		if a.Kind == "tip_revoked" {
			found = true
		}
	}
	if !found {
		t.Fatal("expected a tip_revoked warning alert")
	}
}

// TestRevokeFinalizedBlockRejected ensures confirmed history can never be
// silently undone by a revocation.
func TestRevokeFinalizedBlockRejected(t *testing.T) {
	h := newHarness(t)
	ex := h.Exec
	cf := fixtures.Chains[fixtures.ChainB]
	b := fixtures.NewChainBuilder(cf)
	blk1 := b.BuildBlock(nil)
	blk2 := b.BuildBlock(nil)
	blk3 := b.BuildBlock(nil)
	submitBlock(t, ex, blk1)
	submitBlock(t, ex, blk2)
	submitBlock(t, ex, blk3) // block1 finalized (tip 3, conf 2)

	pub, sig := b.Revocation(blk1.Block)
	err := ex.SubmitRevocation(context.Background(), core.Revocation{
		ChainID: cf.ID, Block: blk1.Block, Validator: pub, Signature: sig,
	})
	mustCode(t, err, core.CodeBlockFinalized)
}

// TestConflictAfterExecutionFreezes: an executed message receives a
// different body for the same key — freeze + critical alert, no rewrite.
func TestConflictAfterExecutionFreezes(t *testing.T) {
	h := newHarness(t)
	ex := h.Exec
	ctx := context.Background()
	cf := fixtures.Chains[fixtures.ChainA]
	bob := cf.Senders[1]
	if err := ex.OpenChannel(ctx, cf.ID, "settle", []key{bob.Pub}); err != nil {
		t.Fatal(err)
	}
	b := fixtures.NewChainBuilder(cf)
	blk1 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "settle", Nonce: 1, Sender: bob, Payload: []byte("body-A")}})
	blk2 := b.BuildBlock(nil)
	blk3 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "settle", Nonce: 1, Sender: bob, Payload: []byte("body-B")}})
	submitBlock(t, ex, blk1)
	submitBlock(t, ex, blk2)
	submitMsg(t, ex, blk1.Messages[0])
	submitBlock(t, ex, blk3) // A finalized
	if n, _ := ex.ProcessAll(ctx); n != 1 {
		t.Fatal("body A should execute")
	}

	err := ex.SubmitMessage(ctx, core.Message{
		ChainID: cf.ID, ChannelID: "settle", Nonce: 1,
		PayloadHash: blk3.Messages[0].PayloadHash, Block: blk3.Block,
		SenderPub: bob.Pub, SenderSig: blk3.Messages[0].SenderSig,
	}, blk3.Messages[0].Proof)
	mustCode(t, err, core.CodeConflictFrozen)

	cs, _ := ex.GetChannel(ctx, cf.ID, "settle")
	if cs.Status != "frozen" || cs.FrozenReason == "" {
		t.Fatalf("channel must be frozen with a reason: %+v", cs)
	}

	// Executed history is intact and immutable.
	got := deliveriesOf(t, ex, cf.ID, "settle")
	if len(got) != 1 || !equalHash(got[0].BlockHex, blk1.Block) {
		t.Fatalf("executed body A history altered: %+v", got)
	}
	msgs, _ := ex.ListMessages(ctx, cf.ID, "settle")
	if len(msgs) != 1 || msgs[0].Status != "executed" || msgs[0].PayloadHashHex != toHex(blk1.Messages[0].PayloadHash[:]) {
		t.Fatalf("executed message must remain unchanged: %+v", msgs)
	}

	// Frozen channel refuses everything.
	mustCode(t, ex.SubmitMessage(ctx, core.Message{
		ChainID: cf.ID, ChannelID: "settle", Nonce: 1,
		PayloadHash: blk1.Messages[0].PayloadHash, Block: blk1.Block,
		SenderPub: bob.Pub, SenderSig: blk1.Messages[0].SenderSig,
	}, blk1.Messages[0].Proof), core.CodeChannelFrozen)

	alerts, _ := ex.ListAlerts(ctx, cf.ID, "critical", 10)
	kinds := map[string]bool{}
	for _, a := range alerts {
		kinds[a.Kind] = true
	}
	if !kinds["message_conflict_executed"] {
		t.Fatalf("missing critical conflict alert, got %v", kinds)
	}
}

// TestConflictBeforeExecutionAlertsOnly: two different bodies for one key,
// neither executed — the conflicting copy is rejected and alerted, channel
// stays open.
func TestConflictBeforeExecutionAlertsOnly(t *testing.T) {
	h := newHarness(t)
	ex := h.Exec
	ctx := context.Background()
	cf := fixtures.Chains[fixtures.ChainA]
	carol := cf.Senders[2]
	if err := ex.OpenChannel(ctx, cf.ID, "vault", []key{carol.Pub}); err != nil {
		t.Fatal(err)
	}
	b := fixtures.NewChainBuilder(cf)
	blk1 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "vault", Nonce: 1, Sender: carol, Payload: []byte("X")}})
	blk2 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "vault", Nonce: 1, Sender: carol, Payload: []byte("Y")}})
	blk3 := b.EmptyBlock()
	submitBlock(t, ex, blk1)
	submitBlock(t, ex, blk2)
	submitBlock(t, ex, blk3) // both blocks final/confirmed
	submitMsg(t, ex, blk1.Messages[0])

	err := ex.SubmitMessage(ctx, core.Message{
		ChainID: cf.ID, ChannelID: "vault", Nonce: 1,
		PayloadHash: blk2.Messages[0].PayloadHash, Block: blk2.Block,
		SenderPub: carol.Pub, SenderSig: blk2.Messages[0].SenderSig,
	}, blk2.Messages[0].Proof)
	mustCode(t, err, core.CodeConflict)

	cs, _ := ex.GetChannel(ctx, cf.ID, "vault")
	if cs.Status != "open" {
		t.Fatal("channel must remain open when nothing was executed")
	}
	// The first-arriving body A remains the sole live message and is
	// delivered exactly once; the conflicting B never creates a second
	// delivery.
	if n, _ := ex.ProcessAll(ctx); n != 1 {
		t.Fatalf("first body should still execute once, got %d", n)
	}
	if got := deliveriesOf(t, ex, cf.ID, "vault"); len(got) != 1 {
		t.Fatalf("conflicting copy created %d deliveries", len(got))
	}
	alerts, _ := ex.ListAlerts(ctx, cf.ID, "critical", 10)
	found := false
	for _, a := range alerts {
		if a.Kind == "message_conflict" {
			found = true
		}
	}
	if !found {
		t.Fatal("expected a message_conflict critical alert")
	}
}

// TestFinalizedEquivocation: a competing header at a finalized height
// triggers channel freeze + critical alert without rewriting history.
func TestFinalizedEquivocation(t *testing.T) {
	h := newHarness(t)
	ex := h.Exec
	ctx := context.Background()
	cf := fixtures.Chains[fixtures.ChainA]
	alice := cf.Senders[0]
	if err := ex.OpenChannel(ctx, cf.ID, "eq", []key{alice.Pub}); err != nil {
		t.Fatal(err)
	}
	b := fixtures.NewChainBuilder(cf)
	blk1 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "eq", Nonce: 1, Sender: alice, Payload: []byte("orig")}})
	blk2 := b.BuildBlock(nil)
	blk3 := b.BuildBlock(nil)
	submitBlock(t, ex, blk1)
	submitBlock(t, ex, blk2)
	submitBlock(t, ex, blk3)
	submitMsg(t, ex, blk1.Messages[0])
	if n, _ := ex.ProcessAll(ctx); n != 1 {
		t.Fatal("original nonce1 should execute")
	}

	evil := fixtures.NewForkBuilder(cf, crypto.Hash{}, 0).
		BuildBlock([]fixtures.MsgSpec{{ChannelID: "eq", Nonce: 1, Sender: alice, Payload: []byte("evil")}})
	err := ex.SubmitHeader(ctx, core.Header{
		ChainID: evil.ChainID, Height: evil.Height, Parent: evil.Parent,
		MsgRoot: evil.MsgRoot, Timestamp: evil.Timestamp,
	}, evil.Validator.Pub, evil.Sig)
	mustCode(t, err, core.CodeEquivocation)

	cs, _ := ex.GetChannel(ctx, cf.ID, "eq")
	if cs.Status != "frozen" {
		t.Fatal("channel with executed message from the attacked block must freeze")
	}
	got := deliveriesOf(t, ex, cf.ID, "eq")
	if len(got) != 1 || !equalHash(got[0].BlockHex, blk1.Block) {
		t.Fatalf("canonical history rewritten: %+v", got)
	}
	headers, _ := ex.ListHeaders(ctx, cf.ID, false)
	for _, hd := range headers {
		if hd.Height == 1 && hd.BlockHex != toHex(blk1.Block[:]) {
			t.Fatal("competing header must not replace canonical history")
		}
	}
	alerts, _ := ex.ListAlerts(ctx, cf.ID, "critical", 10)
	kinds := map[string]bool{}
	for _, a := range alerts {
		kinds[a.Kind] = true
	}
	if !kinds["finalized_equivocation"] || !kinds["channel_frozen"] {
		t.Fatalf("missing equivocation alerts, got %v", kinds)
	}
}

// TestAuthenticationFailures exercises real signature/Merkle verification.
func TestAuthenticationFailures(t *testing.T) {
	h := newHarness(t)
	ex := h.Exec
	ctx := context.Background()
	cfA := fixtures.Chains[fixtures.ChainA]
	cfB := fixtures.Chains[fixtures.ChainB]
	alice := cfA.Senders[0]
	dave := cfB.Senders[0]

	if err := ex.OpenChannel(ctx, cfA.ID, "sec", []key{alice.Pub}); err != nil {
		t.Fatal(err)
	}
	b := fixtures.NewChainBuilder(cfA)
	// Three leaves so the first message has a genuine multi-level Merkle
	// proof (including the odd-node duplication case) to tamper with.
	blk1 := b.BuildBlock([]fixtures.MsgSpec{
		{ChannelID: "sec", Nonce: 1, Sender: alice, Payload: []byte("hi")},
		{ChannelID: "sec", Nonce: 2, Sender: alice, Payload: []byte("ho")},
		{ChannelID: "sec", Nonce: 3, Sender: alice, Payload: []byte("he")},
	})

	// Header signed by the wrong validator.
	err := ex.SubmitHeader(ctx, core.Header{
		ChainID: blk1.ChainID, Height: blk1.Height, Parent: blk1.Parent,
		MsgRoot: blk1.MsgRoot, Timestamp: blk1.Timestamp,
	}, dave.Pub, blk1.Sig)
	mustCode(t, err, core.CodeBadSignature)

	// Genuine header accepted.
	submitBlock(t, ex, blk1)

	// Tampered sender signature.
	m := blk1.Messages[0]
	badSig := append([]byte(nil), m.SenderSig...)
	badSig[0] ^= 0xff
	err = ex.SubmitMessage(ctx, core.Message{
		ChainID: cfA.ID, ChannelID: "sec", Nonce: 1, PayloadHash: m.PayloadHash,
		Block: m.Block, SenderPub: alice.Pub, SenderSig: badSig,
	}, m.Proof)
	mustCode(t, err, core.CodeBadSignature)

	// Sender key not registered on the channel.
	err = ex.SubmitMessage(ctx, core.Message{
		ChainID: cfA.ID, ChannelID: "sec", Nonce: 1, PayloadHash: m.PayloadHash,
		Block: m.Block, SenderPub: dave.Pub,
		SenderSig: crypto.Sign(dave.Priv, m.Commitment),
	}, m.Proof)
	mustCode(t, err, core.CodeBadSignature)

	// Body hash changed without a matching signature -> signature fails.
	forgedBody := crypto.DigestHash([]byte("not hi"))
	err = ex.SubmitMessage(ctx, core.Message{
		ChainID: cfA.ID, ChannelID: "sec", Nonce: 1, PayloadHash: forgedBody,
		Block: m.Block, SenderPub: alice.Pub, SenderSig: m.SenderSig,
	}, m.Proof)
	mustCode(t, err, core.CodeBadSignature)

	// Correct signature but tampered Merkle proof -> proof fails.
	if len(m.Proof.Steps) == 0 {
		t.Fatal("test setup needs a multi-leaf block for proof tampering")
	}
	tamperedProof := merkle.Proof{Index: m.Proof.Index, Steps: cloneSteps(m.Proof.Steps)}
	tamperedProof.Steps[0].Sibling[0] ^= 0xff
	err = ex.SubmitMessage(ctx, core.Message{
		ChainID: cfA.ID, ChannelID: "sec", Nonce: 1, PayloadHash: m.PayloadHash,
		Block: m.Block, SenderPub: alice.Pub, SenderSig: m.SenderSig,
	}, tamperedProof)
	mustCode(t, err, core.CodeBadProof)

	// Revocation signed by the wrong key.
	pub, sig := b.Revocation(blk1.Block)
	_ = pub
	err = ex.SubmitRevocation(ctx, core.Revocation{
		ChainID: cfA.ID, Block: blk1.Block, Validator: dave.Pub, Signature: sig,
	})
	mustCode(t, err, core.CodeBadSignature)
}

// TestMultiLeafBlockRealProofs builds a block with several messages on
// different channels so Merkle proofs have non-empty paths.
func TestMultiLeafBlockRealProofs(t *testing.T) {
	h := newHarness(t)
	ex := h.Exec
	ctx := context.Background()
	cf := fixtures.Chains[fixtures.ChainA]
	alice, bob := cf.Senders[0], cf.Senders[1]
	if err := ex.OpenChannel(ctx, cf.ID, "c1", []key{alice.Pub}); err != nil {
		t.Fatal(err)
	}
	if err := ex.OpenChannel(ctx, cf.ID, "c2", []key{bob.Pub}); err != nil {
		t.Fatal(err)
	}
	b := fixtures.NewChainBuilder(cf)
	blk1 := b.BuildBlock([]fixtures.MsgSpec{
		{ChannelID: "c1", Nonce: 1, Sender: alice, Payload: []byte("m1")},
		{ChannelID: "c2", Nonce: 1, Sender: bob, Payload: []byte("m2")},
		{ChannelID: "c1", Nonce: 2, Sender: alice, Payload: []byte("m3")},
	})
	if len(blk1.Messages[0].Proof.Steps) == 0 {
		t.Fatal("3-leaf block proofs must have levels")
	}
	submitBlock(t, ex, blk1)
	for _, mm := range blk1.Messages {
		submitMsg(t, ex, mm)
	}
	// None confirmed yet (tip at 1): staged.
	if n, _ := ex.ProcessAll(ctx); n != 0 {
		t.Fatalf("unconfirmed messages executed: %d", n)
	}
	submitBlock(t, ex, b.EmptyBlock())
	submitBlock(t, ex, b.EmptyBlock())
	if n, _ := ex.ProcessAll(ctx); n != 3 {
		t.Fatalf("want 3 deliveries across channels, got %d", n)
	}
}
