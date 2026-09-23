package core_test

import (
	"context"
	"crypto/ed25519"
	"encoding/hex"
	"testing"

	"inbox/internal/core"
	"inbox/internal/crypto"
	"inbox/internal/fixtures"
	"inbox/internal/merkle"
	"inbox/internal/testsupport"
)

type key = ed25519.PublicKey

type harness struct {
	*testsupport.Harness
	Exec *core.Executor
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	th := testsupport.New(t)
	ex := core.NewExecutor(th.Store)
	var seed []core.ChainConfig
	for _, id := range fixtures.ChainIDs() {
		cf := fixtures.Chains[id]
		seed = append(seed, core.ChainConfig{
			ID: id, ValidatorPub: cf.Validator.Pub, Confirmations: cf.Confirmations,
		})
	}
	if err := ex.SeedChains(context.Background(), seed); err != nil {
		t.Fatalf("seed chains: %v", err)
	}
	return &harness{Harness: th, Exec: ex}
}

func toHex(b []byte) string { return hex.EncodeToString(b) }

func equalHash(hexStr string, want crypto.Hash) bool { return hexStr == toHex(want[:]) }

func cloneSteps(in []merkle.ProofStep) []merkle.ProofStep {
	out := make([]merkle.ProofStep, len(in))
	for i, s := range in {
		out[i] = merkle.ProofStep{Sibling: s.Sibling, Side: s.Side}
	}
	return out
}

func submitBlock(t *testing.T, ex *core.Executor, b fixtures.BuiltBlock) {
	t.Helper()
	err := ex.SubmitHeader(context.Background(), core.Header{
		ChainID: b.ChainID, Height: b.Height, Parent: b.Parent,
		MsgRoot: b.MsgRoot, Timestamp: b.Timestamp,
	}, b.Validator.Pub, b.Sig)
	if err != nil {
		t.Fatalf("submit header h%d: %v", b.Height, err)
	}
}

func submitMsg(t *testing.T, ex *core.Executor, m fixtures.BuiltMessage) {
	t.Helper()
	err := ex.SubmitMessage(context.Background(), core.Message{
		ChainID: m.ChainID, ChannelID: m.ChannelID, Nonce: m.Nonce,
		PayloadHash: m.PayloadHash, Block: m.Block,
		SenderPub: m.Sender.Pub, SenderSig: m.SenderSig,
	}, m.Proof)
	if err != nil {
		t.Fatalf("submit message nonce %d: %v", m.Nonce, err)
	}
}

func deliveriesOf(t *testing.T, ex *core.Executor, chain, channel string) []core.DeliveryView {
	t.Helper()
	out, err := ex.ListDeliveries(context.Background(), chain, channel)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func mustCode(t *testing.T, err error, want core.ErrorCode) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected error %s, got nil", want)
	}
	var de *core.DomainError
	// *core.DomainError is returned by value-pointer from domErr.
	if !asDomain(err, &de) || de.Code != want {
		t.Fatalf("expected error %s, got %v", want, err)
	}
}

func asDomain(err error, target **core.DomainError) bool {
	de, ok := err.(*core.DomainError)
	if !ok {
		return false
	}
	*target = de
	return true
}
