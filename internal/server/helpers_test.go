package server

import (
	"crypto/ed25519"
	"io"
	"log/slog"
	"testing"

	"github.com/example/snapshotprune/internal/crypto"
	"github.com/example/snapshotprune/internal/engine"
	"github.com/example/snapshotprune/internal/types"
)

// demoAddr returns the deterministic demo address for a seed.
func demoAddr(seed string) types.Address {
	a, _ := crypto.AddressFromPub(crypto.DemoPriv(seed).Public().(ed25519.PublicKey))
	return a
}

func testLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// appendBlocks adds n valid alice->bob blocks to the engine.
func appendBlocks(t *testing.T, eng *engine.Engine, n int) {
	t.Helper()
	alice := crypto.DemoPriv("alice")
	bobAddr := demoAddr("bob")
	for i := 0; i < n; i++ {
		tip, err := eng.Tip()
		if err != nil {
			t.Fatal(err)
		}
		tbl, err := eng.Reconstruct(tip)
		if err != nil {
			t.Fatal(err)
		}
		var nonce uint64
		if acc := tbl[demoAddr("alice")]; acc != nil {
			nonce = acc.Nonce
		}
		tx := types.Tx{Nonce: nonce, To: bobAddr, Amount: 5, Fee: 1}
		if err := crypto.SignTx(&tx, alice); err != nil {
			t.Fatal(err)
		}
		p := &engine.Proposal{Height: tip + 1, Txs: []types.Tx{tx}}
		if _, err := eng.AppendProposal(p); err != nil {
			t.Fatalf("append %d: %v", tip+1, err)
		}
	}
}
