// Package testkeys deterministically generates Ed25519 key material for tests
// and for the example-generator command. These keys are TEST-ONLY: they are
// derived from public labels, not secrets, and must never anchor real trust.
package testkeys

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
)

type KeyPair struct {
	ID        string
	Public    ed25519.PublicKey
	Private   ed25519.PrivateKey
	PublicHex string
}

func derive(seedLabel string) KeyPair {
	seed := sha256.Sum256([]byte("build-attestation TEST seed v1: " + seedLabel))
	priv := ed25519.NewKeyFromSeed(seed[:])
	pub := priv.Public().(ed25519.PublicKey)
	return KeyPair{
		ID:        "key-" + seedLabel,
		Public:    pub,
		Private:   priv,
		PublicHex: hex.EncodeToString(pub),
	}
}

// CI returns the CI builder's current and rotated-away keys.
func CI() (current, old, revoked KeyPair) {
	return derive("ci-current"), derive("ci-old"), derive("ci-revoked")
}

// Other returns a key belonging to a different, unrelated builder — used to
// show cross-builder key misuse is rejected.
func Other() KeyPair { return derive("other-builder") }
