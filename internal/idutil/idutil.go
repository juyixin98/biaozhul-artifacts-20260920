package idutil

import (
	"crypto/rand"
	"encoding/hex"
	"regexp"
)

var validExternalID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// New returns a random 32-character hex identifier.
func New() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failing is not recoverable; panic rather than silently
		// generating colliding IDs.
		panic("cannot read random bytes for id: " + err.Error())
	}
	return hex.EncodeToString(b[:])
}

// ValidExternalID reports whether s is acceptable as a client supplied ID.
func ValidExternalID(s string) bool {
	return validExternalID.MatchString(s)
}
