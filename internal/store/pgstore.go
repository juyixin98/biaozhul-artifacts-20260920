// Package store implements domain.Store against PostgreSQL.
//
// Concurrency model: one Postgres sequence (vc_snapshot_seq) numbers every
// append; readers take FOR SHARE row locks and the sequence's last_value so
// the snapshot returned with a verification is exactly the state read; the
// unique index on revocations.credential_id plus a credential-row FOR
// UPDATE lock makes concurrent revocations of the same credential resolve
// to exactly one winner.
package store

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"vci/internal/domain"
)

//go:embed migrations/0001_init.sql
var migrationSQL string

// PGStore is the PostgreSQL implementation of domain.Store.
type PGStore struct {
	pool *pgxpool.Pool
}

func New(ctx context.Context, url string) (*PGStore, error) {
	cfg, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, fmt.Errorf("store: parse db url: %w", err)
	}
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("store: connect: %w", err)
	}
	return &PGStore{pool: pool}, nil
}

func (s *PGStore) Ping(ctx context.Context) error { return s.pool.Ping(ctx) }
func (s *PGStore) Close()                         { s.pool.Close() }

func (s *PGStore) Migrate(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, migrationSQL)
	if err != nil {
		return fmt.Errorf("store: migrate: %w", err)
	}
	return nil
}

// nextSnapshot consumes one global snapshot number.
func nextSnapshot(ctx context.Context, tx pgx.Tx) (int64, error) {
	var snap int64
	if err := tx.QueryRow(ctx, "SELECT nextval('vc_snapshot_seq')").Scan(&snap); err != nil {
		return 0, fmt.Errorf("store: next snapshot: %w", err)
	}
	return snap, nil
}

func (s *PGStore) Head(ctx context.Context) (int64, error) {
	var snap int64
	// last_value is populated even before the first nextval (it holds the
	// sequence start); is_called distinguishes a never-used sequence,
	// whose logical head is 0.
	err := s.pool.QueryRow(ctx,
		`SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM vc_snapshot_seq`).Scan(&snap)
	if err != nil {
		return 0, fmt.Errorf("store: head: %w", err)
	}
	return snap, nil
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}

// ---------------- issuer / keys ----------------

func (s *PGStore) CreateIssuer(ctx context.Context, name string, pub, priv string, now time.Time) (domain.Issuer, domain.KeyVersion, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return domain.Issuer{}, domain.KeyVersion{}, err
	}
	defer tx.Rollback(ctx)

	snap, err := nextSnapshot(ctx, tx)
	if err != nil {
		return domain.Issuer{}, domain.KeyVersion{}, err
	}
	issuerID := newID("iss")
	if _, err := tx.Exec(ctx,
		`INSERT INTO issuers (id, name, created_at, snapshot) VALUES ($1,$2,$3,$4)`,
		issuerID, name, now, snap); err != nil {
		return domain.Issuer{}, domain.KeyVersion{}, err
	}

	key := domain.KeyVersion{
		ID:         keyID(issuerID, 1),
		IssuerID:   issuerID,
		Seq:        1,
		PublicKey:  pub,
		PrivateKey: priv,
		Algorithm:  "Ed25519",
		ValidFrom:  now,
		Snapshot:   snap,
	}
	if _, err := tx.Exec(ctx,
		`INSERT INTO issuer_keys (id, issuer_id, seq, public_key, private_key, algorithm, valid_from, retired_at, snapshot)
		 VALUES ($1,$2,$3,$4,$5,$6,$7,NULL,$8)`,
		key.ID, key.IssuerID, key.Seq, key.PublicKey, key.PrivateKey,
		key.Algorithm, key.ValidFrom, snap); err != nil {
		return domain.Issuer{}, domain.KeyVersion{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return domain.Issuer{}, domain.KeyVersion{}, err
	}
	return domain.Issuer{ID: issuerID, Name: name, CreatedAt: now, Snapshot: snap}, key, nil
}

// RotateKey appends a new version. When closeOld is true an existing open
// version is retired effective at now; rotation is also permitted when all
// versions have been retired (re-key after a full retirement), in which
// case the new version continues the sequence from the historical maximum.
func (s *PGStore) RotateKey(ctx context.Context, issuerID string, pub, priv string, now time.Time, closeOld bool) (domain.KeyVersion, domain.KeyVersion, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return domain.KeyVersion{}, domain.KeyVersion{}, err
	}
	defer tx.Rollback(ctx)

	// Confirm issuer exists and serialize rotations of the same issuer.
	var exists bool
	if err := tx.QueryRow(ctx, `SELECT true FROM issuers WHERE id=$1 FOR UPDATE`, issuerID).Scan(&exists); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return domain.KeyVersion{}, domain.KeyVersion{}, domain.ErrIssuerNotFound
		}
		return domain.KeyVersion{}, domain.KeyVersion{}, err
	}

	var old domain.KeyVersion
	hadOpen := false
	rows, err := tx.Query(ctx,
		`SELECT id, issuer_id, seq, public_key, private_key, algorithm, valid_from, retired_at, snapshot
		 FROM issuer_keys WHERE issuer_id=$1 AND retired_at IS NULL ORDER BY seq FOR UPDATE`,
		issuerID)
	if err != nil {
		return domain.KeyVersion{}, domain.KeyVersion{}, err
	}
	old, err = scanKey(rows)
	switch {
	case err == nil:
		hadOpen = true
	case errors.Is(err, domain.ErrNotFound):
	default:
		return domain.KeyVersion{}, domain.KeyVersion{}, err
	}
	if hadOpen {
		var openCount int
		if err := tx.QueryRow(ctx,
			`SELECT count(*) FROM issuer_keys WHERE issuer_id=$1 AND retired_at IS NULL`,
			issuerID).Scan(&openCount); err != nil {
			return domain.KeyVersion{}, domain.KeyVersion{}, err
		}
		if openCount != 1 {
			return domain.KeyVersion{}, domain.KeyVersion{}, fmt.Errorf("%w: %d open keys", domain.ErrConflict, openCount)
		}
	}

	nextSeq := int64(1)
	if hadOpen {
		nextSeq = int64(old.Seq + 1)
	} else {
		// Re-key after full retirement: continue after the highest seq.
		if err := tx.QueryRow(ctx,
			`SELECT COALESCE(max(seq)+1, 1) FROM issuer_keys WHERE issuer_id=$1`,
			issuerID).Scan(&nextSeq); err != nil {
			return domain.KeyVersion{}, domain.KeyVersion{}, err
		}
	}

	snap, err := nextSnapshot(ctx, tx)
	if err != nil {
		return domain.KeyVersion{}, domain.KeyVersion{}, err
	}

	if hadOpen && closeOld {
		if _, err := tx.Exec(ctx,
			`UPDATE issuer_keys SET retired_at=$2, snapshot=$3 WHERE id=$1`,
			old.ID, now, snap); err != nil {
			return domain.KeyVersion{}, domain.KeyVersion{}, err
		}
		old.RetiredAt = &now
		old.Snapshot = snap
	}

	nk := domain.KeyVersion{
		ID:         keyID(issuerID, int(nextSeq)),
		IssuerID:   issuerID,
		Seq:        int(nextSeq),
		PublicKey:  pub,
		PrivateKey: priv,
		Algorithm:  "Ed25519",
		ValidFrom:  now,
		Snapshot:   snap,
	}
	if _, err := tx.Exec(ctx,
		`INSERT INTO issuer_keys (id, issuer_id, seq, public_key, private_key, algorithm, valid_from, retired_at, snapshot)
		 VALUES ($1,$2,$3,$4,$5,$6,$7,NULL,$8)`,
		nk.ID, nk.IssuerID, nk.Seq, nk.PublicKey, nk.PrivateKey,
		nk.Algorithm, nk.ValidFrom, snap); err != nil {
		if isUniqueViolation(err) {
			return domain.KeyVersion{}, domain.KeyVersion{}, domain.ErrConflict
		}
		return domain.KeyVersion{}, domain.KeyVersion{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return domain.KeyVersion{}, domain.KeyVersion{}, err
	}
	return nk, old, nil
}

func (s *PGStore) RetireActiveKey(ctx context.Context, issuerID string, now time.Time) (domain.KeyVersion, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return domain.KeyVersion{}, err
	}
	defer tx.Rollback(ctx)

	k, err := activeKeyTx(ctx, tx, issuerID, true)
	if err != nil {
		return domain.KeyVersion{}, err
	}
	snap, err := nextSnapshot(ctx, tx)
	if err != nil {
		return domain.KeyVersion{}, err
	}
	if _, err := tx.Exec(ctx,
		`UPDATE issuer_keys SET retired_at=$2, snapshot=$3 WHERE id=$1`,
		k.ID, now, snap); err != nil {
		return domain.KeyVersion{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return domain.KeyVersion{}, err
	}
	k.RetiredAt = &now
	k.Snapshot = snap
	return k, nil
}

func (s *PGStore) GetIssuer(ctx context.Context, issuerID string) (domain.Issuer, error) {
	var iss domain.Issuer
	err := s.pool.QueryRow(ctx,
		`SELECT id, name, created_at, snapshot FROM issuers WHERE id=$1`,
		issuerID).Scan(&iss.ID, &iss.Name, &iss.CreatedAt, &iss.Snapshot)
	if errors.Is(err, pgx.ErrNoRows) {
		return domain.Issuer{}, domain.ErrIssuerNotFound
	}
	return iss, err
}

func (s *PGStore) ActiveKey(ctx context.Context, issuerID string) (domain.KeyVersion, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return domain.KeyVersion{}, err
	}
	defer tx.Rollback(ctx)
	k, err := activeKeyTx(ctx, tx, issuerID, false)
	if err != nil {
		return domain.KeyVersion{}, err
	}
	return k, tx.Commit(ctx)
}

func activeKeyTx(ctx context.Context, tx pgx.Tx, issuerID string, forUpdate bool) (domain.KeyVersion, error) {
	lock := ""
	if forUpdate {
		lock = " FOR UPDATE"
	}
	rows, err := tx.Query(ctx, `
		SELECT id, issuer_id, seq, public_key, private_key, algorithm, valid_from, retired_at, snapshot
		FROM issuer_keys WHERE issuer_id=$1 AND retired_at IS NULL ORDER BY seq DESC`+lock, issuerID)
	if err != nil {
		return domain.KeyVersion{}, err
	}
	defer rows.Close()
	if !rows.Next() {
		// Distinguish unknown issuer from "known issuer, no open key".
		var exists bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM issuers WHERE id=$1)`, issuerID).Scan(&exists); err != nil {
			return domain.KeyVersion{}, err
		}
		if !exists {
			return domain.KeyVersion{}, domain.ErrIssuerNotFound
		}
		return domain.KeyVersion{}, domain.ErrNoActiveKey
	}
	return scanRowKey(rows)
}

// ---------------- credentials ----------------

func (s *PGStore) IssueCredential(
	ctx context.Context,
	issuerID, subject, purpose string,
	notBefore, expiresAt, now time.Time,
	sign domain.SignFunc,
) (domain.Credential, domain.KeyVersion, error) {
	if !expiresAt.After(notBefore) {
		return domain.Credential{}, domain.KeyVersion{}, fmt.Errorf("%w: expires_at must be after not_before", domain.ErrConflict)
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return domain.Credential{}, domain.KeyVersion{}, err
	}
	defer tx.Rollback(ctx)

	// Lock the issuer's open key row for the whole transaction so the key
	// used for signing cannot be retired concurrently. An "open" key is
	// exactly one whose interval has not been closed (retired_at IS NULL):
	// rotation stamps the old row's retired_at before inserting the new
	// one, so there is never a future-dated open version.
	rows, err := tx.Query(ctx, `
		SELECT id, issuer_id, seq, public_key, private_key, algorithm, valid_from, retired_at, snapshot
		FROM issuer_keys
		WHERE issuer_id=$1 AND retired_at IS NULL
		ORDER BY seq DESC
		FOR UPDATE`, issuerID)
	if err != nil {
		return domain.Credential{}, domain.KeyVersion{}, err
	}
	k, kerr := scanKey(rows)
	rows.Close()
	if errors.Is(kerr, domain.ErrNotFound) {
		var exists bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM issuers WHERE id=$1)`, issuerID).Scan(&exists); err != nil {
			return domain.Credential{}, domain.KeyVersion{}, err
		}
		if !exists {
			return domain.Credential{}, domain.KeyVersion{}, domain.ErrIssuerNotFound
		}
		return domain.Credential{}, domain.KeyVersion{}, domain.ErrNoActiveKey
	}
	if kerr != nil {
		return domain.Credential{}, domain.KeyVersion{}, kerr
	}

	payloadJSON, contentJSON, signature, contentHash, err := sign(k, now)
	if err != nil {
		return domain.Credential{}, domain.KeyVersion{}, err
	}

	snap, err := nextSnapshot(ctx, tx)
	if err != nil {
		return domain.Credential{}, domain.KeyVersion{}, err
	}

	c := domain.Credential{
		ID:          newID("cred"),
		IssuerID:    issuerID,
		Subject:     subject,
		Purpose:     purpose,
		NotBefore:   notBefore,
		ExpiresAt:   expiresAt,
		ContentHash: contentHash,
		ContentJSON: contentJSON,
		PayloadJSON: payloadJSON,
		Signature:   signature,
		KeyID:       k.ID,
		IssuedAt:    now,
		Snapshot:    snap,
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO credentials
		(id, issuer_id, subject, purpose, not_before, expires_at, content_hash,
		 content_json, payload_json, signature, key_id, issued_at, snapshot)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`,
		c.ID, c.IssuerID, c.Subject, c.Purpose, c.NotBefore, c.ExpiresAt,
		c.ContentHash, c.ContentJSON, c.PayloadJSON, c.Signature,
		c.KeyID, c.IssuedAt, c.Snapshot); err != nil {
		if isUniqueViolation(err) {
			return domain.Credential{}, domain.KeyVersion{}, domain.ErrConflict
		}
		return domain.Credential{}, domain.KeyVersion{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return domain.Credential{}, domain.KeyVersion{}, err
	}
	return c, k, nil
}

func (s *PGStore) GetCredential(ctx context.Context, credentialID string) (domain.Credential, error) {
	row := s.pool.QueryRow(ctx, `
		SELECT id, issuer_id, subject, purpose, not_before, expires_at,
		       content_hash, content_json, payload_json, signature, key_id, issued_at, snapshot
		FROM credentials WHERE id=$1`, credentialID)
	c, err := scanCredential(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return domain.Credential{}, domain.ErrNotFound
	}
	return c, err
}

// ---------------- revocation ----------------

func (s *PGStore) Revoke(ctx context.Context, credentialID, reason string, effectiveAt, now time.Time) (domain.Revocation, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return domain.Revocation{}, err
	}
	defer tx.Rollback(ctx)

	// FOR UPDATE serializes concurrent revokes of the same credential:
	// the second tx blocks until the first commits, then sees the existing
	// revocation and returns ErrConflict without burning a snapshot.
	var existsID string
	err = tx.QueryRow(ctx,
		`SELECT id FROM credentials WHERE id=$1 FOR UPDATE`, credentialID).Scan(&existsID)
	if errors.Is(err, pgx.ErrNoRows) {
		return domain.Revocation{}, domain.ErrNotFound
	}
	if err != nil {
		return domain.Revocation{}, err
	}

	var existing string
	err = tx.QueryRow(ctx,
		`SELECT id FROM revocations WHERE credential_id=$1`, credentialID).Scan(&existing)
	if err == nil {
		return domain.Revocation{}, fmt.Errorf("%w: credential already revoked", domain.ErrConflict)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return domain.Revocation{}, err
	}

	snap, err := nextSnapshot(ctx, tx)
	if err != nil {
		return domain.Revocation{}, err
	}
	rev := domain.Revocation{
		ID:           newID("rev"),
		CredentialID: credentialID,
		Reason:       reason,
		EffectiveAt:  effectiveAt,
		CreatedAt:    now,
		Snapshot:     snap,
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO revocations (id, credential_id, reason, effective_at, created_at, snapshot)
		VALUES ($1,$2,$3,$4,$5,$6)`,
		rev.ID, rev.CredentialID, rev.Reason, rev.EffectiveAt, rev.CreatedAt, rev.Snapshot); err != nil {
		if isUniqueViolation(err) {
			return domain.Revocation{}, fmt.Errorf("%w: credential already revoked", domain.ErrConflict)
		}
		return domain.Revocation{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return domain.Revocation{}, err
	}
	return rev, nil
}

// ReadVerificationState returns the locked history slice and the head
// snapshot. Locks taken:
//
//	credential row: FOR SHARE  (blocks concurrent revoke of THIS credential;
//	                            shared with other verifiers)
//	key row:        FOR SHARE  (blocks concurrent rotation of THIS key)
//	sequence:       FOR SHARE-ish via last_value read at the end
//
// Taking the head AFTER the locks means any concurrent committer either
// committed before our read (we include their change and see their snap) or
// is blocked until we release, in which case the cached verdict for our
// snapshot cannot possibly predate their change.
func (s *PGStore) ReadVerificationState(ctx context.Context, credentialID string) (domain.VerificationState, error) {
	tx, err := s.pool.Begin(ctx) // default READ COMMITTED is sufficient with row locks
	if err != nil {
		return domain.VerificationState{}, err
	}
	defer tx.Rollback(ctx)

	st := domain.VerificationState{}

	c, err := getCredentialTx(ctx, tx, credentialID, true)
	if errors.Is(err, domain.ErrNotFound) {
		// Unknown credential: still report the current head so caches key
		// correctly.
		head, herr := headTx(ctx, tx)
		if herr != nil {
			return domain.VerificationState{}, herr
		}
		st.Snapshot = head
		return st, tx.Commit(ctx)
	}
	if err != nil {
		return domain.VerificationState{}, err
	}
	st.Credential = &c

	rows, err := tx.Query(ctx, `
		SELECT id, issuer_id, seq, public_key, private_key, algorithm, valid_from, retired_at, snapshot
		FROM issuer_keys WHERE id=$1 FOR SHARE`, c.KeyID)
	if err != nil {
		return domain.VerificationState{}, err
	}
	k, err := scanKey(rows)
	if err == nil {
		st.Key = &k
	} else if !errors.Is(err, domain.ErrNotFound) {
		return domain.VerificationState{}, err
	}

	var rev domain.Revocation
	err = tx.QueryRow(ctx, `
		SELECT id, credential_id, reason, effective_at, created_at, snapshot
		FROM revocations WHERE credential_id=$1
		ORDER BY effective_at ASC, snapshot ASC LIMIT 1`, credentialID).
		Scan(&rev.ID, &rev.CredentialID, &rev.Reason, &rev.EffectiveAt, &rev.CreatedAt, &rev.Snapshot)
	switch {
	case err == nil:
		st.Revocation = &rev
	case errors.Is(err, pgx.ErrNoRows):
	default:
		return domain.VerificationState{}, err
	}

	head, err := headTx(ctx, tx)
	if err != nil {
		return domain.VerificationState{}, err
	}
	st.Snapshot = head
	return st, tx.Commit(ctx)
}

func headTx(ctx context.Context, tx pgx.Tx) (int64, error) {
	var head int64
	if err := tx.QueryRow(ctx,
		`SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM vc_snapshot_seq`).Scan(&head); err != nil {
		return 0, err
	}
	return head, nil
}

func getCredentialTx(ctx context.Context, tx pgx.Tx, id string, forShare bool) (domain.Credential, error) {
	lock := ""
	if forShare {
		lock = " FOR SHARE"
	}
	row := tx.QueryRow(ctx, `
		SELECT id, issuer_id, subject, purpose, not_before, expires_at,
		       content_hash, content_json, payload_json, signature, key_id, issued_at, snapshot
		FROM credentials WHERE id=$1`+lock, id)
	c, err := scanCredential(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return domain.Credential{}, domain.ErrNotFound
	}
	return c, err
}
