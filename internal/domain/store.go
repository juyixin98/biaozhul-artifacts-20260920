package domain

import (
	"context"
	"time"
)

// SignFunc is invoked inside the issuance transaction with the key the
// store selected; the service performs the REAL Ed25519 signing there, so
// credential row, payload and snapshot commit atomically.
type SignFunc func(k KeyVersion, issuedAt time.Time) (
	payloadJSON, contentJSON, signature []byte,
	contentHash string,
	err error,
)

// VerificationState is the frozen slice of history a verifier needs to
// replay a decision as-of a point in time. Rows are read under
// FOR SHARE/serializable snapshot so the returned Snapshot is the exact
// state represented.
type VerificationState struct {
	Snapshot   Snapshot
	Credential *Credential // nil if the credential does not exist at all
	Key        *KeyVersion // the key version named by the credential
	Revocation *Revocation // the earliest-effective revocation (nil if none)
}

// Store is the append-only log store.
type Store interface {
	// Migrate installs the schema (idempotent).
	Migrate(ctx context.Context) error

	// Head returns the current global snapshot (0 on an empty log).
	Head(ctx context.Context) (Snapshot, error)

	// CreateIssuer appends an issuer row and its genesis key version,
	// returning the created entities and the consumed snapshot.
	CreateIssuer(ctx context.Context, name string, pub, priv string, now time.Time) (Issuer, KeyVersion, error)

	// RotateKey appends a new key version for issuerID with seq = prev+1 and,
	// when closeOld is true, retires the previously active key effective at
	// now. Returns the new version. ErrConflict if no active key exists
	// (caller must retire-explicitly / create first).
	RotateKey(ctx context.Context, issuerID string, pub, priv string, now time.Time, closeOld bool) (KeyVersion, KeyVersion, error)

	// RetireActiveKey marks the currently active key of issuerID retired
	// effective at now. Returns the closed version. ErrNotFound /
	// ErrNoActiveKey when there is nothing to retire.
	RetireActiveKey(ctx context.Context, issuerID string, now time.Time) (KeyVersion, error)

	// GetIssuer loads an issuer.
	GetIssuer(ctx context.Context, issuerID string) (Issuer, error)

	// ActiveKey returns the open key version of issuerID.
	ActiveKey(ctx context.Context, issuerID string) (KeyVersion, error)

	// IssueCredential appends a credential under the issuer's active key.
	// sign is invoked inside the transaction so signing and the snapshot
	// append are atomic. Returns ErrNoActiveKey when the issuer has no key
	// interval open at now.
	IssueCredential(ctx context.Context, issuerID, subject, purpose string, notBefore, expiresAt time.Time, now time.Time, sign SignFunc) (Credential, KeyVersion, error)

	// GetCredential loads a credential (current row).
	GetCredential(ctx context.Context, credentialID string) (Credential, error)

	// Revoke appends a revocation event. It is an error to revoke an
	// unknown credential or one that already has any revocation event
	// recorded (ErrNotFound / ErrConflict). The check and the insert happen
	// in one transaction with a row lock, so concurrent revokes of the same
	// credential leave exactly one event.
	Revoke(ctx context.Context, credentialID, reason string, effectiveAt, now time.Time) (Revocation, error)

	// ReadVerificationState locks the relevant history (FOR SHARE) and
	// returns the credential, its signing key version and the earliest
	// revocation event, together with the head snapshot of the read. A
	// credential that was never issued yields Credential == nil.
	ReadVerificationState(ctx context.Context, credentialID string) (VerificationState, error)
}
