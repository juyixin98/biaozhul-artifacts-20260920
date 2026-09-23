// Package sig implements the real cryptographic operations of the mirror
// admission system: canonical JSON encoding, Ed25519 signing/verification and
// PEM key I/O. No signature result is ever faked or cached as a boolean —
// verification runs over the actual bytes on every request.
package sig

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
)

// CanonicalJSON deterministically re-encodes arbitrary JSON: object keys
// sorted lexicographically (encoding/json's default), no insignificant
// whitespace, numbers preserved verbatim (UseNumber) so int/float formatting
// cannot drift between signer and verifier.
func CanonicalJSON(raw []byte) ([]byte, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, fmt.Errorf("parse json for canonicalisation: %w", err)
	}
	// Reject trailing tokens/garbage after the document.
	if dec.More() {
		return nil, errors.New("canonical json: unexpected trailing data")
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(canonicalize(v)); err != nil {
		return nil, err
	}
	// Encode appends a newline; signatures must not depend on it.
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

func canonicalize(v any) any {
	switch t := v.(type) {
	case map[string]any:
		// encoding/json marshals maps with sorted keys already; recurse
		// anyway so nested json.Number values survive.
		out := make(map[string]any, len(t))
		for k, val := range t {
			out[k] = canonicalize(val)
		}
		return out
	case []any:
		for i := range t {
			t[i] = canonicalize(t[i])
		}
		return t
	default:
		return v
	}
}

// Sign returns base64(Ed25519(canonical(raw))) using priv.
func Sign(priv ed25519.PrivateKey, raw []byte) (string, []byte, error) {
	canon, err := CanonicalJSON(raw)
	if err != nil {
		return "", nil, err
	}
	sig := ed25519.Sign(priv, canon)
	return base64.StdEncoding.EncodeToString(sig), canon, nil
}

// Verify checks a base64 Ed25519 signature against canonical(raw).
// It returns a descriptive error on ANY failure (bad base64, wrong length,
// cryptographic mismatch) — callers must treat error != nil as invalid.
func Verify(pub ed25519.PublicKey, raw []byte, signatureB64 string) error {
	canon, err := CanonicalJSON(raw)
	if err != nil {
		return fmt.Errorf("payload canonicalisation: %w", err)
	}
	sig, err := base64.StdEncoding.DecodeString(signatureB64)
	if err != nil {
		return fmt.Errorf("signature base64 decode: %w", err)
	}
	if len(sig) != ed25519.SignatureSize {
		return fmt.Errorf("signature length %d, want %d", len(sig), ed25519.SignatureSize)
	}
	if !ed25519.Verify(pub, canon, sig) {
		return errors.New("ed25519 signature mismatch over canonical payload")
	}
	return nil
}

// Key IDs are the lower-hex SHA-256 of the raw public key, giving an
// unforgeable, stable name for the key a payload claims to be signed by.
func KeyID(pub ed25519.PublicKey) (string, error) {
	if len(pub) != ed25519.PublicKeySize {
		return "", fmt.Errorf("public key length %d, want %d", len(pub), ed25519.PublicKeySize)
	}
	return "ed25519:" + fmt.Sprintf("%x", sha256Sum(pub)), nil
}

// --- PEM I/O ----------------------------------------------------------------

const (
	publicPEMType  = "MIRRORAD ED25519 PUBLIC KEY"
	privatePEMType = "MIRRORAD ED25519 PRIVATE KEY"
)

// WritePublicKeyPEM / WritePrivateKeyPEM persist test keys.
func WritePublicKeyPEM(path string, pub ed25519.PublicKey) error {
	return writePEM(path, publicPEMType, pub)
}

func WritePrivateKeyPEM(path string, priv ed25519.PrivateKey) error {
	return writePEM(path, privatePEMType, priv.Seed())
}

func writePEM(path, typ string, der []byte) error {
	b := pem.EncodeToMemory(&pem.Block{Type: typ, Bytes: der})
	return os.WriteFile(path, b, 0o600)
}

// ReadPublicKeyPEM loads and validates a 32-byte Ed25519 public key.
func ReadPublicKeyPEM(path string) (ed25519.PublicKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errors.New("no PEM block in public key file")
	}
	if block.Type != publicPEMType {
		return nil, fmt.Errorf("public key PEM type %q, want %q", block.Type, publicPEMType)
	}
	if len(block.Bytes) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("public key seed length %d, want %d", len(block.Bytes), ed25519.PublicKeySize)
	}
	return ed25519.PublicKey(block.Bytes), nil
}

// ReadPrivateKeyPEM reconstructs the private key from its stored 32-byte seed.
func ReadPrivateKeyPEM(path string) (ed25519.PrivateKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errors.New("no PEM block in private key file")
	}
	if block.Type != privatePEMType {
		return nil, fmt.Errorf("private key PEM type %q, want %q", block.Type, privatePEMType)
	}
	if len(block.Bytes) != ed25519.SeedSize {
		return nil, fmt.Errorf("private seed length %d, want %d", len(block.Bytes), ed25519.SeedSize)
	}
	return ed25519.NewKeyFromSeed(block.Bytes), nil
}
