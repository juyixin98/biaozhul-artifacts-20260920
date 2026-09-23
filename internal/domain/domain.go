// Package domain holds the core entities and the storage interface of the
// revocable credential index.
package domain

import (
	"errors"
	"time"
)

// Snapshot is the global, monotonically increasing log position. Every
// state-changing append (issuer creation, key rotation/retirement,
// credential issuance, revocation) consumes one snapshot number from a
// Postgres sequence. A verification response carries the snapshot against
// which its conclusion was computed; a cache entry is only valid while the
// head snapshot is unchanged.
//
// Sequence values consumed by aborted transactions may leave gaps —
// monotonicity, not denseness, is the invariant callers rely on.
type Snapshot = int64

// Issuer is a SYNTHETIC test identity. Private keys are stored server-side
// purely so the demo can sign credentials; this is not a production key
// management design.
type Issuer struct {
	ID        string
	Name      string
	CreatedAt time.Time
	Snapshot  Snapshot // snapshot at which the issuer existed
}

// KeyVersion is one interval of a signing key's lifecycle.
//
//   - ValidFrom is the instant the key became usable for signing.
//   - RetiredAt, when non-nil, is the effective instant of retirement
//     (either an explicit retirement or the launch instant of the next key
//     for the default "rotate closes the old interval" behaviour).
//
// Intervals are append-only: rotation inserts a new row with the next Seq
// and (by default) stamps RetiredAt on the previous row. The active key of
// an issuer is the highest-Seq row with NULL RetiredAt.
type KeyVersion struct {
	ID         string
	IssuerID   string
	Seq        int
	PublicKey  string // hex-encoded Ed25519, 32 bytes
	PrivateKey string // hex-encoded Ed25519, 64 bytes — SYNTHETIC TEST ONLY
	Algorithm  string // "Ed25519"
	ValidFrom  time.Time
	RetiredAt  *time.Time
	Snapshot   Snapshot // snapshot at which this version was appended/closed
}

// Credential is an issued, locally signed verifiable credential.
type Credential struct {
	ID          string
	IssuerID    string
	Subject     string
	Purpose     string
	NotBefore   time.Time
	ExpiresAt   time.Time
	ContentHash string
	ContentJSON []byte // canonical JSON of the credential content
	PayloadJSON []byte // canonical JSON of the signed payload (what was signed)
	Signature   []byte // Ed25519 signature over PayloadJSON
	KeyID       string
	IssuedAt    time.Time
	Snapshot    Snapshot // snapshot at which the credential was issued
}

// Revocation is one append-only revocation event.
type Revocation struct {
	ID           string
	CredentialID string
	Reason       string
	EffectiveAt  time.Time
	CreatedAt    time.Time
	Snapshot     Snapshot // snapshot at which the revocation was recorded
}

// Active returns whether the key interval is open at instant t.
func (k KeyVersion) Active(t time.Time) bool {
	if t.Before(k.ValidFrom) {
		return false
	}
	if k.RetiredAt != nil && !t.Before(*k.RetiredAt) {
		return false
	}
	return true
}

// VerificationVerdict is the outcome of a credential check.
type VerificationVerdict string

const (
	// VerdictValid: signature cryptographic and policy checks all passed.
	VerdictValid VerificationVerdict = "VALID"
	// VerdictRevoked: the credential was revoked at or before the query time.
	VerdictRevoked VerificationVerdict = "REVOKED"
	// VerdictExpired: the credential's validity interval does not cover the
	// query time (either not yet valid or already expired).
	VerdictExpired VerificationVerdict = "EXPIRED"
	// VerdictPurposeMismatch: signature is valid but expected_purpose differs.
	VerdictPurposeMismatch VerificationVerdict = "PURPOSE_MISMATCH"
	// VerdictContentMismatch: the supplied content digest does not match.
	VerdictContentMismatch VerificationVerdict = "CONTENT_MISMATCH"
	// VerdictInvalidSignature: signature does not verify under the key that
	// was active at issuance.
	VerdictInvalidSignature VerificationVerdict = "INVALID_SIGNATURE"
	// VerdictKeyInactive: the signing key was retired before issuance.
	VerdictKeyInactive VerificationVerdict = "KEY_INACTIVE"
	// VerdictUnknown: credential did not exist at the queried point in time.
	VerdictUnknown VerificationVerdict = "UNKNOWN"
)

// Sentinel errors returned by Store implementations.
var (
	ErrNotFound       = errors.New("domain: not found")
	ErrConflict       = errors.New("domain: conflict")
	ErrIssuerNotFound = errors.New("issuer not found")
	ErrNoActiveKey    = errors.New("issuer has no active signing key")
)
