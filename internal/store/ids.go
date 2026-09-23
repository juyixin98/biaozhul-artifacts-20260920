package store

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
)

// newID returns a random identifier prefixed with kind, e.g. "cred_3f1a…".
func newID(kind string) string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(fmt.Errorf("store: random id: %w", err))
	}
	return kind + "_" + hex.EncodeToString(b[:])
}

// keyID names key versions deterministically by issuer and sequence:
// "key_<issuer-without-prefix>_1".
func keyID(issuerID string, seq int) string {
	return fmt.Sprintf("key_%s_%d", stripPrefix(issuerID, "iss_"), seq)
}

func stripPrefix(s, prefix string) string {
	if len(s) >= len(prefix) && s[:len(prefix)] == prefix {
		return s[len(prefix):]
	}
	return s
}
