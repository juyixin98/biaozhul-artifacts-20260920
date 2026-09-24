// Package crypto implements real cryptographic operations for telemetry messages.
//
// Every device shares a secret with the backend (provisioned in PostgreSQL).
// Telemetry payloads carry an HMAC-SHA256 signature over a canonical byte
// representation of the message. Signatures are genuinely computed and verified
// with crypto/hmac + crypto/sha256 — constant-time compared, never bypassed in
// the happy path.
package crypto

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
)

// CanonicalString is the exact byte string that is signed.
// Field order is fixed so that JSON key reordering does not change the
// signature, while any change to a signed field invalidates it.
type CanonicalFields struct {
	DeviceID string
	BootGen  int64
	Seq      int64
	Value    float64
	TSMillis int64
}

// CanonicalString returns the deterministic representation that is signed:
//
//	"v1\ndevice=<id>\nboot=<gen>\nseq=<n>\nvalue=<v>\nts=<millis>"
func CanonicalString(f CanonicalFields) string {
	return fmt.Sprintf("v1\ndevice=%s\nboot=%d\nseq=%d\nvalue=%s\nts=%d",
		f.DeviceID, f.BootGen, f.Seq, formatFloat(f.Value), f.TSMillis)
}

// Sign returns the lowercase hex HMAC-SHA256 of the canonical string.
func Sign(secret string, f CanonicalFields) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(CanonicalString(f)))
	return hex.EncodeToString(mac.Sum(nil))
}

// Verify checks a lowercase hex HMAC-SHA256 signature in constant time.
func Verify(secret string, f CanonicalFields, sigHex string) bool {
	got, err := hex.DecodeString(sigHex)
	if err != nil {
		return false
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(CanonicalString(f)))
	return hmac.Equal(got, mac.Sum(nil))
}
