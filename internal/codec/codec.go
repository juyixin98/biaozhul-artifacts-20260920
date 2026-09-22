// Package codec encrypts transfer authorization codes at rest.
//
// Auth codes are treated like passwords-ish secrets: they are never stored in
// plain text, never returned after creation/rotation (except the one-time
// reveal to the owner) and never logged. Storage uses AES-256-GCM with a key
// supplied via configuration.
package codec

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"fmt"
)

var (
	ErrKeySize    = errors.New("codec: key must be 32 bytes (AES-256)")
	ErrCiphertext = errors.New("codec: malformed ciphertext")
)

type Codec struct {
	aead cipher.AEAD
}

// New creates an AES-256-GCM codec from a 32-byte key.
func New(key []byte) (*Codec, error) {
	if len(key) != 32 {
		return nil, ErrKeySize
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, fmt.Errorf("codec: %w", err)
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("codec: %w", err)
	}
	return &Codec{aead: aead}, nil
}

// Encrypt seals plaintext and returns a self-contained base64 nonce||ciphertext blob.
func (c *Codec) Encrypt(plaintext string) (string, error) {
	nonce := make([]byte, c.aead.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return "", err
	}
	sealed := c.aead.Seal(nil, nonce, []byte(plaintext), nil)
	out := append(nonce, sealed...)
	return base64.StdEncoding.EncodeToString(out), nil
}

// Decrypt opens a blob produced by Encrypt.
func (c *Codec) Decrypt(blob string) (string, error) {
	raw, err := base64.StdEncoding.DecodeString(blob)
	if err != nil {
		return "", ErrCiphertext
	}
	ns := c.aead.NonceSize()
	if len(raw) < ns {
		return "", ErrCiphertext
	}
	pt, err := c.aead.Open(nil, raw[:ns], raw[ns:], nil)
	if err != nil {
		return "", ErrCiphertext
	}
	return string(pt), nil
}

// ConstantTimeEqual compares an expected code against a candidate without
// leaking timing information.
func ConstantTimeEqual(a, b string) bool {
	return subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1
}
