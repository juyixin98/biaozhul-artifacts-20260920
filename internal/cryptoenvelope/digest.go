package cryptoenvelope

import "crypto/sha256"

// sha256Sum is split out so the digest path is explicit.
func sha256Sum(b []byte) [32]byte {
	return sha256.Sum256(b)
}
