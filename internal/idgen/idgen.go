package idgen

import "github.com/google/uuid"

// NewID returns a random v7 UUID when supported, falling back to v4.
// v7 is time-ordered, which keeps insert order cache/IO friendly; the random
// suffix still prevents id enumeration.
func NewID() uuid.UUID {
	id, err := uuid.NewV7()
	if err != nil {
		return uuid.New()
	}
	return id
}

// NewLeaseToken is an opaque token identifying one specific lease grant.
func NewLeaseToken() uuid.UUID {
	return uuid.New()
}
