// Package fixtures holds the deterministic local signing fixtures for the
// two simulated source chains. All keys are derived from public seed names
// via SHA-256 -> Ed25519, so every participant reproduces the identical
// keys; no keys are read from the network or generated ad hoc.
package fixtures

import (
	"inbox/internal/crypto"
)

// ChainID identifies one of the two simulated source chains.
type ChainID = string

const (
	ChainA ChainID = "chain-a"
	ChainB ChainID = "chain-b"
)

// ChainFixture bundles a chain's static configuration.
type ChainFixture struct {
	ID            string
	Validator     crypto.KeyPair
	Confirmations uint64
	Senders       []crypto.KeyPair
}

// Sender returns the registered sender key pair with the given name.
func (c ChainFixture) Sender(name string) (crypto.KeyPair, bool) {
	for _, s := range c.Senders {
		if s.Name == name {
			return s, true
		}
	}
	return crypto.KeyPair{}, false
}

// Chains are the two simulated source chains, each with one validator key
// (whose signatures finalize headers / revocation evidence) and three
// registered application sender keys.
var Chains = map[string]ChainFixture{
	ChainA: {
		ID:            ChainA,
		Validator:     crypto.NewKeyPair("validator/chain-a"),
		Confirmations: 2,
		Senders: []crypto.KeyPair{
			crypto.NewKeyPair("sender/chain-a/alice"),
			crypto.NewKeyPair("sender/chain-a/bob"),
			crypto.NewKeyPair("sender/chain-a/carol"),
		},
	},
	ChainB: {
		ID:            ChainB,
		Validator:     crypto.NewKeyPair("validator/chain-b"),
		Confirmations: 2,
		Senders: []crypto.KeyPair{
			crypto.NewKeyPair("sender/chain-b/dave"),
			crypto.NewKeyPair("sender/chain-b/erin"),
			crypto.NewKeyPair("sender/chain-b/frank"),
		},
	},
}

// ChainIDs returns the chain IDs in stable order.
func ChainIDs() []string { return []string{ChainA, ChainB} }
