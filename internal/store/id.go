package store

import (
	"crypto/rand"
	"encoding/hex"
)

// newSnapshotID returns an unguessable 128-bit hex snapshot id.
func newSnapshotID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failing is not recoverable for a network service.
		panic(err)
	}
	return hex.EncodeToString(b[:])
}
