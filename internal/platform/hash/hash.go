// Package hash holds the digest primitives shared by event dedup and the
// append-only audit chain.
package hash

import (
	"crypto/sha256"
	"encoding/hex"
	"strconv"
)

const ZeroHash = "0000000000000000000000000000000000000000000000000000000000000000"

// SHA256Hex returns the lowercase hex sha256 of data.
func SHA256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// AuditEntryHash derives the hash of one audit-chain entry:
// sha256(seq || "\n" || prevHash || "\n" || sha256(canonicalPayload)).
// Verification recomputes exactly these three fields, so a tampered payload or
// a reordered/missing predecessor changes the hash.
func AuditEntryHash(seq int64, prevHash string, canonicalPayload []byte) string {
	payloadDigest := SHA256Hex(canonicalPayload)
	return SHA256Hex([]byte(strconv.FormatInt(seq, 10) + "\n" + prevHash + "\n" + payloadDigest))
}
