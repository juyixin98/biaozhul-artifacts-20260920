package cryptoenvelope

import (
	"crypto/ed25519"
	"crypto/sha256"
	"fmt"
)

// fixtureNamespace makes the deterministic seeds local to this test fixture.
const fixtureNamespace = "ximbox/local-fixture-key/v1:"

// FixtureKey is a deterministic Ed25519 keypair used to simulate a source
// chain's signer. It is generated from a fixed label, not from os randomness,
// so every machine derives the identical keys and can verify the recorded
// examples. These keys are for local testing only and are publicly known;
// never use them on a real network.
type FixtureKey struct {
	Chain   string
	Private ed25519.PrivateKey
	Public  ed25519.PublicKey
}

// FixtureKey derives the deterministic fixture keypair for a chain label.
func FixtureKeyFor(chain string) FixtureKey {
	seed := sha256.Sum256([]byte(fixtureNamespace + chain))
	priv := ed25519.NewKeyFromSeed(seed[:])
	pub := priv.Public().(ed25519.PublicKey)
	pubCopy := append(ed25519.PublicKey(nil), pub...)
	return FixtureKey{
		Chain:   chain,
		Private: priv,
		Public:  pubCopy,
	}
}

// TrustedFixtures returns the immutable signer registry the inbox trusts:
// one simulated signer per source chain.
func TrustedFixtures() map[string]FixtureKey {
	chains := []string{ChainA, ChainB}
	m := make(map[string]FixtureKey, len(chains))
	for _, c := range chains {
		m[c] = FixtureKeyFor(c)
	}
	return m
}

// TrustedPublicKeys returns the verification registry keyed by source chain.
func TrustedPublicKeys() map[string]ed25519.PublicKey {
	fixtures := TrustedFixtures()
	out := make(map[string]ed25519.PublicKey, len(fixtures))
	for chain, k := range fixtures {
		out[chain] = k.Public
	}
	return out
}

// MustFixtureKey resolves a fixture key for a chain label or panics if the
// chain is unknown. Used by the signing CLI.
func MustFixtureKey(chain string) FixtureKey {
	k := FixtureKeyFor(chain)
	switch chain {
	case ChainA, ChainB:
		return k
	default:
		panic(fmt.Sprintf("unknown fixture chain %q (want %q or %q)", chain, ChainA, ChainB))
	}
}
