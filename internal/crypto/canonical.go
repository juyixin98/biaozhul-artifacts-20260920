// Package crypto implements the deterministic encoding and the real
// Ed25519 signature operations used by the credential index.
//
// Everything in this package operates on SYNTHETIC test identities only:
// every key is generated locally and there is no connection to any real
// issuer, person, or certificate authority.
package crypto

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
)

// ContentDigestPrefix identifies the hash algorithm used for content digests.
const ContentDigestPrefix = "sha256:"

// CanonicalJSON serializes a generic JSON value (as produced by
// encoding/json when decoding into interface{}) into a deterministic byte
// form:
//
//   - object members are sorted lexicographically by UTF-8 key;
//   - no insignificant whitespace is emitted;
//   - strings use compact JSON escaping;
//   - numbers are passed through with their exact input text when possible;
//   - null, booleans, strings and arrays behave like JSON.
//
// The bytes returned are the exact bytes that are signed and hashed, so
// issuer and verifier always agree on the message.
func CanonicalJSON(v interface{}) ([]byte, error) {
	var b []byte
	if err := writeCanonical(&b, v); err != nil {
		return nil, err
	}
	return b, nil
}

func writeCanonical(b *[]byte, v interface{}) error {
	switch t := v.(type) {
	case nil:
		*b = append(*b, "null"...)
	case bool:
		if t {
			*b = append(*b, "true"...)
		} else {
			*b = append(*b, "false"...)
		}
	case string:
		writeJSONString(b, t)
	case json.Number:
		s := t.String()
		if s == "" {
			return errors.New("crypto: empty json.Number")
		}
		*b = append(*b, s...)
	case float64:
		// encoding/json decodes numbers to float64 by default. Use the
		// shortest round-trippable representation, matching json.Marshal.
		*b = appendFloat(*b, t)
	case int64:
		*b = appendDecInt(*b, t)
	case int:
		*b = appendDecInt(*b, int64(t))
	case []interface{}:
		*b = append(*b, '[')
		for i, e := range t {
			if i > 0 {
				*b = append(*b, ',')
			}
			if err := writeCanonical(b, e); err != nil {
				return err
			}
		}
		*b = append(*b, ']')
	case map[string]interface{}:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		*b = append(*b, '{')
		for i, k := range keys {
			if i > 0 {
				*b = append(*b, ',')
			}
			writeJSONString(b, k)
			*b = append(*b, ':')
			if err := writeCanonical(b, t[k]); err != nil {
				return err
			}
		}
		*b = append(*b, '}')
	default:
		return fmt.Errorf("crypto: cannot canonicalize value of type %T", v)
	}
	return nil
}

// ContentDigest returns "sha256:" + hex(sha256(canonicalJSON(content))).
func ContentDigest(content interface{}) (string, error) {
	cb, err := CanonicalJSON(content)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(cb)
	return ContentDigestPrefix + hex.EncodeToString(sum[:]), nil
}

// VerifyDigest recomputes the digest of content and compares it with want.
func VerifyDigest(want string, content interface{}) (bool, error) {
	got, err := ContentDigest(content)
	if err != nil {
		return false, err
	}
	return got == want, nil
}

// GenerateEd25519Key creates a fresh synthetic Ed25519 key pair.
func GenerateEd25519Key() (ed25519.PublicKey, ed25519.PrivateKey, error) {
	return ed25519.GenerateKey(rand.Reader)
}

// Sign signs message with the Ed25519 private key (RFC 8032).
func Sign(priv ed25519.PrivateKey, message []byte) []byte {
	return ed25519.Sign(priv, message)
}

// Verify checks an Ed25519 signature. It returns nil when the signature is
// valid for message under pub.
func Verify(pub ed25519.PublicKey, message, signature []byte) error {
	if len(pub) != ed25519.PublicKeySize {
		return errors.New("crypto: invalid public key length")
	}
	if !ed25519.Verify(pub, message, signature) {
		return errors.New("crypto: signature verification failed")
	}
	return nil
}

// EncodePublic / DecodePublic hex-encode the 32-byte Ed25519 public key.
func EncodePublic(pub ed25519.PublicKey) string { return hex.EncodeToString(pub) }

func DecodePublic(s string) (ed25519.PublicKey, error) {
	raw, err := hex.DecodeString(s)
	if err != nil {
		return nil, fmt.Errorf("crypto: public key not hex: %w", err)
	}
	if len(raw) != ed25519.PublicKeySize {
		return nil, errors.New("crypto: public key must be 32 bytes")
	}
	return ed25519.PublicKey(raw), nil
}

// EncodePrivate / DecodePrivate hex-encode the 64-byte Ed25519 private key.
func EncodePrivate(priv ed25519.PrivateKey) string { return hex.EncodeToString(priv) }

func DecodePrivate(s string) (ed25519.PrivateKey, error) {
	raw, err := hex.DecodeString(s)
	if err != nil {
		return nil, fmt.Errorf("crypto: private key not hex: %w", err)
	}
	if len(raw) != ed25519.PrivateKeySize {
		return nil, errors.New("crypto: private key must be 64 bytes")
	}
	return ed25519.PrivateKey(raw), nil
}

// B64 / Unb64 are thin helpers for the JSON field encoding of the payload
// and signature, so credentials can be transported as pure JSON.
func B64(b []byte) string            { return base64.StdEncoding.EncodeToString(b) }
func Unb64(s string) ([]byte, error) { return base64.StdEncoding.DecodeString(s) }
