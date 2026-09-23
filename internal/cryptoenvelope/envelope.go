// Package cryptoenvelope defines the canonical, signed representation of a
// cross-chain message and implements real cryptographic operations:
// SHA-256 content digests and Ed25519 signatures (stdlib only).
//
// The message key (SourceChain, Channel, Sequence) plus the immutable body
// digest is what the source-chain fixture signs. A relayer cannot swap the
// body after the fact: the server recomputes body_digest over the exact bytes
// it received and compares it against the signed digest.
package cryptoenvelope

import (
	"crypto/ed25519"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

// ChainA / ChainB are the two simulated source chains.
const (
	ChainA = "chainA"
	ChainB = "chainB"
)

// ErrInvalidSignature is returned when an Ed25519 signature fails verification.
var ErrInvalidSignature = errors.New("invalid message signature")

// Envelope is the wire representation a relayer POSTs to the inbox.
//
// Body is the raw, exactly-as-received JSON payload; its SHA-256 digest must
// equal BodyDigest. Signature is an Ed25519 signature over the canonical
// serialization returned by SigningBytes.
type Envelope struct {
	SourceChain string          `json:"source_chain"`
	Channel     string          `json:"channel"`
	Sequence    uint64          `json:"sequence"`
	BlockHash   string          `json:"block_hash"` // source block this message is anchored to
	Body        json.RawMessage `json:"body"`
	BodyDigest  string          `json:"body_digest"` // 0x-prefixed hex SHA-256 of Body
	Signer      string          `json:"signer"`      // 0x-prefixed hex Ed25519 public key (32 bytes)
	Signature   string          `json:"signature"`   // 0x-prefixed hex Ed25519 signature (64 bytes)
}

// signedDocument is the exact JSON document a fixture signs. Field order is
// fixed by struct declaration; encoding/json emits fields in declaration
// order, and both signer and verifier use this same type, so the bytes match.
type signedDocument struct {
	SourceChain string `json:"source_chain"`
	Channel     string `json:"channel"`
	Sequence    uint64 `json:"sequence"`
	BlockHash   string `json:"block_hash"`
	BodyDigest  string `json:"body_digest"`
}

// SigningBytes returns the canonical bytes that are signed for this message
// key + anchor + digest. It is deterministic: compact JSON, fixed key order.
func SigningBytes(sourceChain, channel, blockHash string, sequence uint64, bodyDigest string) ([]byte, error) {
	if sourceChain == "" || channel == "" || blockHash == "" {
		return nil, errors.New("source_chain, channel and block_hash are required")
	}
	return json.Marshal(signedDocument{
		SourceChain: sourceChain,
		Channel:     channel,
		Sequence:    sequence,
		BlockHash:   blockHash,
		BodyDigest:  bodyDigest,
	})
}

// canonicalBody returns the deterministic byte representation a digest is
// computed over: compact JSON with no extra whitespace. Canonicalizing at
// both signing and verification means the digest survives the inevitable
// re-encoding a JSON envelope undergoes in transit (Go's encoding/json in
// particular emits compact bytes when unmarshalling a nested RawMessage).
func canonicalBody(body []byte) ([]byte, error) {
	if !json.Valid(body) {
		return nil, errors.New("body must be valid JSON")
	}
	dec := json.NewDecoder(strings.NewReader(string(body)))
	dec.UseNumber() // preserve integer literals exactly (no float64 rounding)
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	return json.Marshal(v)
}

// DigestBody computes the 0x-prefixed lowercase hex SHA-256 digest of the
// CANONICAL (compact) message body. The digest is part of the signed
// document, so the body is immutable: any semantic change changes the digest.
func DigestBody(body []byte) string {
	canonical, err := canonicalBody(body)
	if err != nil {
		// Keep this helper total for callers that already hold validated JSON;
		// invalid input simply hashes as-is (it will be rejected by Verify).
		canonical = body
	}
	sum := sha256Sum(canonical)
	return "0x" + hex.EncodeToString(sum[:])
}

// Sign produces a fully populated Envelope for the given key, anchor and body.
// The body must be valid JSON.
func Sign(priv ed25519.PrivateKey, sourceChain, channel, blockHash string, sequence uint64, body json.RawMessage) (*Envelope, error) {
	canonical, err := canonicalBody(body)
	if err != nil {
		return nil, err
	}
	digest := DigestBody(canonical)
	doc, err := SigningBytes(sourceChain, channel, blockHash, sequence, digest)
	if err != nil {
		return nil, err
	}
	sig := ed25519.Sign(priv, doc)
	pub, ok := priv.Public().(ed25519.PublicKey)
	if !ok {
		return nil, errors.New("not an Ed25519 private key")
	}
	return &Envelope{
		SourceChain: sourceChain,
		Channel:     channel,
		Sequence:    sequence,
		BlockHash:   blockHash,
		Body:        canonical,
		BodyDigest:  digest,
		Signer:      "0x" + hex.EncodeToString(pub),
		Signature:   "0x" + hex.EncodeToString(sig),
	}, nil
}

// Verify checks the envelope's internal consistency and its Ed25519 signature
// against the supplied trusted public key. It performs real verification;
// a tampered body, key or signature always fails.
func (e *Envelope) Verify(trusted ed25519.PublicKey) error {
	if e == nil {
		return errors.New("nil envelope")
	}
	if len(trusted) != ed25519.PublicKeySize {
		return fmt.Errorf("trusted public key has wrong size %d", len(trusted))
	}
	canonical, err := canonicalBody(e.Body)
	if err != nil {
		return errors.New("body must be valid JSON")
	}
	e.Body = canonical
	if len(e.BlockHash) != 66 || e.BlockHash[:2] != "0x" {
		return errors.New("block_hash must be 0x + 64 hex chars")
	}
	if got := DigestBody(e.Body); got != e.BodyDigest {
		return fmt.Errorf("body digest mismatch: envelope says %s, body hashes to %s", e.BodyDigest, got)
	}
	signer, err := decodeHex(e.Signer, ed25519.PublicKeySize)
	if err != nil {
		return fmt.Errorf("signer: %w", err)
	}
	if !e.publicKeyEqual(signer, trusted) {
		return errors.New("signer is not the trusted key for this source chain")
	}
	sig, err := decodeHex(e.Signature, ed25519.SignatureSize)
	if err != nil {
		return fmt.Errorf("signature: %w", err)
	}
	doc, err := SigningBytes(e.SourceChain, e.Channel, e.BlockHash, e.Sequence, e.BodyDigest)
	if err != nil {
		return err
	}
	if !ed25519.Verify(trusted, doc, sig) {
		return ErrInvalidSignature
	}
	return nil
}

func (e *Envelope) publicKeyEqual(a, b ed25519.PublicKey) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func decodeHex(s string, want int) ([]byte, error) {
	if len(s) < 2 || s[:2] != "0x" {
		return nil, errors.New("expected 0x-prefixed hex string")
	}
	raw, err := hex.DecodeString(s[2:])
	if err != nil {
		return nil, err
	}
	if len(raw) != want {
		return nil, fmt.Errorf("expected %d bytes, got %d", want, len(raw))
	}
	return raw, nil
}
