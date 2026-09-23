package main

import (
	"inbox/internal/fixtures"
	"inbox/internal/wire"
)

func openReq(chainID, channelID string, senderPubs ...[]byte) wire.OpenChannelRequest {
	pubs := make([]string, len(senderPubs))
	for i, p := range senderPubs {
		pubs[i] = hx(p)
	}
	return wire.OpenChannelRequest{ChainID: chainID, ChannelID: channelID, SenderPubHex: pubs}
}

// genBasic demonstrates out-of-order staging and gap filling.
//
// confirmations = 2: a block at height h is confirmed when the tip is
// h+2. We build five blocks; channel orders carries nonces 1,2,3 in
// blocks 1,2,3. Messages are submitted in order 1, 3, 2 so that nonce 3
// is staged ahead of the watermark and waits for the gap.
func genBasic() (*scenario, error) {
	cf := fixtures.Chains[fixtures.ChainA]
	alice := cf.Senders[0]
	b := fixtures.NewChainBuilder(cf)

	blk1 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "orders", Nonce: 1, Sender: alice, Payload: []byte(`{"order":"buy-1"}`)}})
	blk2 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "orders", Nonce: 2, Sender: alice, Payload: []byte(`{"order":"buy-2"}`)}})
	blk3 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "orders", Nonce: 3, Sender: alice, Payload: []byte(`{"order":"buy-3"}`)}})
	blk4 := b.EmptyBlock()
	blk5 := b.EmptyBlock()

	sc := &scenario{}
	sc.open = []wire.OpenChannelRequest{openReq(cf.ID, "orders", alice.Pub)}
	sc.headers = []wire.HeaderRequest{
		headerReq(blk1), headerReq(blk2), headerReq(blk3), headerReq(blk4), headerReq(blk5),
	}
	sc.messages = []namedMessage{
		{"message-1", messageReq(blk1.Messages[0])},
		{"message-2", messageReq(blk2.Messages[0])},
		{"message-3", messageReq(blk3.Messages[0])},
	}
	sc.steps = []string{
		"1. POST open-1.json    -> /v1/chains/chain-a/channels",
		"2. POST header-1.json  -> /headers (tip=1)",
		"3. POST header-2.json  -> /headers (tip=2)",
		"4. POST header-3.json  -> /headers (tip=3: block1 confirmed, block2..3 unconfirmed)",
		"5. POST message-1.json -> /messages (nonce1, confirmed)",
		"6. POST message-3.json -> /messages (nonce3, candidate, staged AHEAD of watermark)",
		"7. POST process       -> /v1/process (delivers nonce1 only; gap at 2 stops the prefix)",
		"8. POST message-2.json -> /messages (fills the gap; nonce2 still candidate until tip advances)",
		"9. POST process       -> /v1/process (still only nonce1 delivered: block2 unconfirmed)",
		"10. POST header-4.json -> /headers (tip=4: block2 confirmed)",
		"11. POST process      -> /v1/process (delivers nonce2; nonce3 still unconfirmed)",
		"12. POST header-5.json -> /headers (tip=5: block3 confirmed -> prefix 1,2,3 contiguous)",
		"13. POST process      -> /v1/process (delivers nonce3)",
		"14. POST message-1.json -> /messages (identical duplicate: idempotent, no second delivery)",
		"15. GET deliveries    -> /v1/chains/chain-a/channels/orders/deliveries (exactly 3 rows, attempts=1)",
	}
	return sc, nil
}

// genReorg: a pre-finality tip is revoked with validator evidence, its
// candidate message cancelled, then a replacement block carries nonce 1.
func genReorg() (*scenario, error) {
	cf := fixtures.Chains[fixtures.ChainB]
	dave := cf.Senders[0]
	b := fixtures.NewChainBuilder(cf)

	// block1 empty; block2 is the unconfirmed tip carrying the old nonce1.
	blk1 := b.BuildBlock(nil)
	tip2 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "payments", Nonce: 1, Sender: dave, Payload: []byte("old-tip-payment")}})

	pub, sig := b.Revocation(tip2.Block)
	revoke := wire.RevocationRequest{ChainID: cf.ID, BlockHex: hx(tip2.Block[:]),
		ValidatorPubHex: hx(pub), SignatureHex: hx(sig)}

	// Replacement block2 extends blk1; block3 extends it (tip=3, block2
	// still needs one more); block4 gives block2 depth 2 -> confirmed.
	fork := fixtures.NewForkBuilder(cf, blk1.Block, blk1.Height)
	newBlk2 := fork.BuildBlock([]fixtures.MsgSpec{{ChannelID: "payments", Nonce: 1, Sender: dave, Payload: []byte("new-tip-payment")}})
	cont := fixtures.NewForkBuilder(cf, newBlk2.Block, newBlk2.Height)
	blk3 := cont.BuildBlock(nil)
	blk4 := fixtures.NewForkBuilder(cf, blk3.Block, blk3.Height).BuildBlock(nil)

	sc := &scenario{}
	sc.open = []wire.OpenChannelRequest{openReq(cf.ID, "payments", dave.Pub)}
	sc.headers = []wire.HeaderRequest{
		headerReq(blk1), headerReq(tip2), headerReq(newBlk2), headerReq(blk3), headerReq(blk4),
	}
	sc.revokes = []wire.RevocationRequest{revoke}
	sc.messages = []namedMessage{
		{"message-old-1", messageReq(tip2.Messages[0])},
		{"message-new-1", messageReq(newBlk2.Messages[0])},
	}
	sc.steps = []string{
		"1. POST open-1.json        -> /v1/chains/chain-b/channels",
		"2. POST header-1.json      -> /headers (block1)",
		"3. POST header-2.json      -> /headers (block2 = unconfirmed tip, old nonce1)",
		"4. POST message-old-1.json -> /messages (candidate under unconfirmed tip)",
		"5. POST revoke-1.json      -> /revocations (tip revoked; candidate -> cancelled)",
		"6. POST message-old-1.json -> /messages (rejected: its block is no longer canonical)",
		"7. POST header-3.json      -> /headers (replacement block2 with new nonce1)",
		"8. POST message-new-1.json -> /messages (accepted under canonical block)",
		"9. POST header-4.json      -> /headers (extension; replacement still depth 1)",
		"10. POST header-5.json      -> /headers (replacement confirmed at depth 2)",
		"11. POST process           -> /v1/process (delivers the NEW nonce1 once; old stays cancelled)",
	}
	return sc, nil
}

// genConflict: the same key (chain, channel, nonce) is committed in two
// honest canonical blocks with two different immutable bodies. Once the
// first is executed, the second copy is conflicting evidence: the channel
// is frozen and a critical alert raised; executed history is kept.
func genConflict() (*scenario, error) {
	cf := fixtures.Chains[fixtures.ChainA]
	bob := cf.Senders[1]
	b := fixtures.NewChainBuilder(cf)

	// block1 carries nonce1 body A; block2 empty; block3 re-commits nonce1
	// with body B (a genuine duplicate-nonce/conflicting-body proof signed
	// by the same registered sender and proven in block3's Merkle root).
	blk1 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "settle", Nonce: 1, Sender: bob, Payload: []byte("body-A pay alice 10")}})
	blk2 := b.BuildBlock(nil)
	blk3 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "settle", Nonce: 1, Sender: bob, Payload: []byte("body-B pay alice 999")}})

	sc := &scenario{}
	sc.open = []wire.OpenChannelRequest{openReq(cf.ID, "settle", bob.Pub)}
	sc.headers = []wire.HeaderRequest{headerReq(blk1), headerReq(blk2), headerReq(blk3)}
	sc.messages = []namedMessage{
		{"message-A-1", messageReq(blk1.Messages[0])},
		{"message-B-1", messageReq(blk3.Messages[0])},
	}
	sc.steps = []string{
		"1. POST open-1.json      -> /v1/chains/chain-a/channels",
		"2. POST header-1.json    -> /headers (block1, body A)",
		"3. POST header-2.json    -> /headers (A confirmed)",
		"4. POST message-A-1.json -> /messages",
		"5. POST header-3.json    -> /headers (A finalized; block3 also contains nonce1 body B)",
		"6. POST process         -> /v1/process (A executed: nonce 1 delivered)",
		"7. POST message-B-1.json -> /messages (CONFLICT vs executed body -> 409, channel FROZEN, critical alert; history NOT rewritten)",
		"8. GET  /v1/alerts      -> inspect the message_conflict_executed incident",
		"9. POST message-A-1.json -> /messages (now rejected: channel locked)",
	}
	return sc, nil
}
