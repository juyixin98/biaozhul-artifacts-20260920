package crypto

import (
	"crypto/ed25519"
	"strings"
	"testing"

	"github.com/example/snapshotprune/internal/types"
)

func TestSignVerifyRoundTrip(t *testing.T) {
	priv := DemoPriv("alice")
	tx := &types.Tx{Nonce: 7, To: types.Address{9}, Amount: 42, Fee: 3}
	if err := SignTx(tx, priv); err != nil {
		t.Fatal(err)
	}
	if err := CheckTx(tx); err != nil {
		t.Fatalf("valid tx rejected: %v", err)
	}
	// Tamper with amount after signing -> signature must fail.
	tx.Amount = 43
	if err := CheckTx(tx); err == nil {
		t.Fatal("tampered amount accepted")
	}
}

func TestSignerAddressBinding(t *testing.T) {
	alice := DemoPriv("alice")
	bobPub := DemoPriv("bob").Public().(ed25519.PublicKey)
	bobAddr, _ := AddressFromPub(bobPub)
	tx := &types.Tx{Nonce: 0, From: bobAddr, To: types.Address{1}, Amount: 1}
	if err := SignTx(tx, alice); err != nil {
		t.Fatal(err)
	}
	// SignTx sets From to the real signer; if a caller overrides it,
	// CheckTx must reject the mismatch.
	tx.From = bobAddr
	if err := CheckTx(tx); err == nil || !strings.Contains(err.Error(), "mismatch") {
		t.Fatalf("expected signer mismatch, got %v", err)
	}
}

func TestNodeKeyPersistence(t *testing.T) {
	dir := t.TempDir()
	k1, err := LoadOrCreateNodeKey(dir)
	if err != nil {
		t.Fatal(err)
	}
	k2, err := LoadOrCreateNodeKey(dir)
	if err != nil {
		t.Fatal(err)
	}
	if k1.PubHex() != k2.PubHex() {
		t.Fatal("node key not stable across reload")
	}
	msg := []byte("manifest-body")
	sig := k1.SignManifest(msg)
	if err := VerifyManifest(k2.Pub, msg, sig); err != nil {
		t.Fatalf("manifest signature verify: %v", err)
	}
	sig[0] ^= 1
	if err := VerifyManifest(k2.Pub, msg, sig); err == nil {
		t.Fatal("forged manifest signature accepted")
	}
}

func TestAddressDerivation(t *testing.T) {
	a, err := AddressFromPub(DemoPriv("alice").Public().(ed25519.PublicKey))
	if err != nil {
		t.Fatal(err)
	}
	if len(a.Hex()) != 42 {
		t.Fatalf("address hex length: %s", a.Hex())
	}
	if _, err := types.ParseAddress(a.Hex()); err != nil {
		t.Fatal(err)
	}
}
