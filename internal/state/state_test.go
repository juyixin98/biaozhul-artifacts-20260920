package state

import (
	"crypto/ed25519"
	"errors"
	"testing"

	"github.com/example/snapshotprune/internal/crypto"
	"github.com/example/snapshotprune/internal/types"
)

func addr(seed string) types.Address {
	a, _ := crypto.AddressFromPub(crypto.DemoPriv(seed).Public().(ed25519.PublicKey))
	return a
}

func TestApplyTransferBurnsFee(t *testing.T) {
	alice := addr("alice")
	bob := addr("bob")
	tbl := Table{
		alice: {Nonce: 0, Balance: 1000},
	}
	tx := types.Tx{Nonce: 0, From: alice, To: bob, Amount: 100, Fee: 7}
	if err := crypto.SignTx(&tx, crypto.DemoPriv("alice")); err != nil {
		t.Fatal(err)
	}
	d, err := Apply(tbl, 1, []types.Tx{tx})
	if err != nil {
		t.Fatal(err)
	}
	if tbl[alice].Balance != 893 || tbl[alice].Nonce != 1 {
		t.Fatalf("sender wrong: %+v", tbl[alice])
	}
	if tbl[bob].Balance != 100 {
		t.Fatalf("recipient wrong: %+v", tbl[bob])
	}
	if TotalSupply(tbl) != 993 {
		t.Fatalf("fee not burned: supply=%d", TotalSupply(tbl))
	}

	// Delta reconstructs the same state from a cloned pre-state.
	pre := Table{alice: {Nonce: 0, Balance: 1000}}
	ApplyDelta(pre, d)
	if Root(pre) != Root(tbl) {
		t.Fatal("ApplyDelta does not reproduce Apply result")
	}
}

func TestApplyRejectsBadNonceAndBalance(t *testing.T) {
	alice := addr("alice")
	bob := addr("bob")
	tbl := Table{alice: {Nonce: 3, Balance: 10}}

	tx := types.Tx{Nonce: 0, To: bob, Amount: 1, Fee: 0} // wrong nonce
	_ = crypto.SignTx(&tx, crypto.DemoPriv("alice"))
	if _, err := Apply(tbl, 1, []types.Tx{tx}); !errors.Is(err, ErrBadNonce) {
		t.Fatalf("expected ErrBadNonce, got %v", err)
	}

	tx2 := types.Tx{Nonce: 3, To: bob, Amount: 100, Fee: 1}
	_ = crypto.SignTx(&tx2, crypto.DemoPriv("alice"))
	if _, err := Apply(tbl, 1, []types.Tx{tx2}); !errors.Is(err, ErrInsufficientBalance) {
		t.Fatalf("expected ErrInsufficientBalance, got %v", err)
	}
}

func TestRootIsDeterministicAndOrderIndependent(t *testing.T) {
	a := addr("a1")
	b := addr("b2")
	t1 := Table{a: {Nonce: 1, Balance: 2}, b: {Nonce: 3, Balance: 4}}
	t2 := Table{b: {Nonce: 3, Balance: 4}, a: {Nonce: 1, Balance: 2}}
	if Root(t1) != Root(t2) {
		t.Fatal("map iteration order leaked into root")
	}
	if Root(t1) == Root(Table{a: {Nonce: 1, Balance: 2}}) {
		t.Fatal("different states produced same root")
	}
}
