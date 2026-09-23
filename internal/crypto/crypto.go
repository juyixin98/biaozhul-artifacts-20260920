// Package crypto defines the domain-separated hashing and signing primitives
// used by the simulated cross-chain inbox. All operations are real
// Ed25519/SHA-256 operations; there is no test-only shortcut.
package crypto

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
)

// Hash is a SHA-256 digest.
type Hash [32]byte

// EncodeString returns a canonical, length-prefixed byte representation of
// the string under the given domain tag. Every distinct object kind uses its
// own domain tag so that encodings cannot collide across types.
func EncodeString(tag, s string) []byte {
	tb := []byte(tag)
	sb := []byte(s)
	out := make([]byte, 0, len(tb)+8+len(sb)+8)
	out = binary.BigEndian.AppendUint32(out, uint32(len(tb)))
	out = append(out, tb...)
	out = binary.BigEndian.AppendUint64(out, uint64(len(sb)))
	out = append(out, sb...)
	return out
}

// EncodeHashSeq encodes a domain tag followed by a sequence of 32-byte
// hashes (used for block commitments and revocation evidence).
func EncodeHashSeq(tag string, hashes []Hash) []byte {
	tb := []byte(tag)
	out := make([]byte, 0, len(tb)+8+len(hashes)*32)
	out = binary.BigEndian.AppendUint32(out, uint32(len(tb)))
	out = append(out, tb...)
	out = binary.BigEndian.AppendUint64(out, uint64(len(hashes)))
	for _, h := range hashes {
		out = append(out, h[:]...)
	}
	return out
}

// EncodeUint64 encodes a tagged unsigned integer.
func EncodeUint64(tag string, n uint64) []byte {
	tb := []byte(tag)
	out := make([]byte, 0, len(tb)+8+8)
	out = binary.BigEndian.AppendUint32(out, uint32(len(tb)))
	out = append(out, tb...)
	out = binary.BigEndian.AppendUint64(out, n)
	return out
}

// DigestHash is SHA-256 over an already-assembled byte slice.
func DigestHash(b []byte) Hash { return sha256.Sum256(b) }

// MessageCommitment binds (channelID, nonce, payloadHash) into one digest.
// This is the value committed inside a block's message Merkle root and the
// value a sender signs. Because the payloadHash is part of the commitment,
// a body can never be silently rewritten.
func MessageCommitment(channelID string, nonce uint64, payloadHash Hash) Hash {
	buf := make([]byte, 0, len(channelID)+64)
	buf = append(buf, EncodeString("inbox/message/channel", channelID)...)
	buf = append(buf, EncodeUint64("inbox/message/nonce", nonce)...)
	buf = append(buf, EncodeHashSeq("inbox/message/payload", []Hash{payloadHash})...)
	return sha256.Sum256(buf)
}

// HeaderHash commits to every field of a source block header.
func HeaderHash(chainID string, height uint64, parent Hash, msgRoot Hash, timestamp int64) Hash {
	buf := make([]byte, 0, 256)
	buf = append(buf, EncodeString("inbox/header/chain", chainID)...)
	buf = append(buf, EncodeUint64("inbox/header/height", height)...)
	buf = append(buf, EncodeHashSeq("inbox/header/parent", []Hash{parent})...)
	buf = append(buf, EncodeHashSeq("inbox/header/msgroot", []Hash{msgRoot})...)
	tb := EncodeUint64("inbox/header/time", uint64(timestamp))
	buf = append(buf, tb...)
	return sha256.Sum256(buf)
}

// RevocationHash is the signed evidence a single validator publishes to
// revoke the current unconfirmed tip.
func RevocationHash(chainID string, block Hash) Hash {
	buf := make([]byte, 0, 128)
	buf = append(buf, EncodeString("inbox/revoke/chain", chainID)...)
	buf = append(buf, EncodeHashSeq("inbox/revoke/block", []Hash{block})...)
	return sha256.Sum256(buf)
}

// Sign produces an Ed25519 signature over the digest.
func Sign(priv ed25519.PrivateKey, h Hash) []byte {
	return ed25519.Sign(priv, h[:])
}

// Verify checks an Ed25519 signature over the digest.
func Verify(pub ed25519.PublicKey, h Hash, sig []byte) error {
	if len(pub) != ed25519.PublicKeySize {
		return errors.New("invalid public key length")
	}
	if len(sig) != ed25519.SignatureSize {
		return fmt.Errorf("invalid signature length: got %d, want %d", len(sig), ed25519.SignatureSize)
	}
	if !ed25519.Verify(pub, h[:], sig) {
		return errors.New("signature verification failed")
	}
	return nil
}

// KeyPair is an Ed25519 key pair with a human-readable role.
type KeyPair struct {
	Name string
	Priv ed25519.PrivateKey
	Pub  ed25519.PublicKey
}

// NewKeyPair derives a deterministic Ed25519 key pair from a seed name.
// The seeds are well-known fixture seeds: this is intentional and lets any
// developer reproduce the exact same validator/sender keys. They must never
// be used outside the simulated chains.
func NewKeyPair(name string) KeyPair {
	seedDigest := sha256.Sum256(EncodeString("inbox/fixture/seed/v1", name))
	priv := ed25519.NewKeyFromSeed(seedDigest[:])
	return KeyPair{Name: name, Priv: priv, Pub: priv.Public().(ed25519.PublicKey)}
}
