package store

import (
	"github.com/jackc/pgx/v5"

	"vci/internal/domain"
)

type rowScanner interface {
	Scan(dest ...any) error
}
type rowsScanner interface {
	Next() bool
	Scan(dest ...any) error
}

func scanCredential(r rowScanner) (domain.Credential, error) {
	var c domain.Credential
	err := r.Scan(
		&c.ID, &c.IssuerID, &c.Subject, &c.Purpose, &c.NotBefore, &c.ExpiresAt,
		&c.ContentHash, &c.ContentJSON, &c.PayloadJSON, &c.Signature,
		&c.KeyID, &c.IssuedAt, &c.Snapshot,
	)
	return c, err
}

// scanKey consumes exactly the first row from rows, ErrNotFound if empty.
func scanKey(rows pgx.Rows) (domain.KeyVersion, error) {
	defer rows.Close()
	if !rows.Next() {
		if err := rows.Err(); err != nil {
			return domain.KeyVersion{}, err
		}
		return domain.KeyVersion{}, domain.ErrNotFound
	}
	return scanRowKey(rows)
}

func scanRowKey(r rowsScanner) (domain.KeyVersion, error) {
	var k domain.KeyVersion
	if err := r.Scan(
		&k.ID, &k.IssuerID, &k.Seq, &k.PublicKey, &k.PrivateKey,
		&k.Algorithm, &k.ValidFrom, &k.RetiredAt, &k.Snapshot,
	); err != nil {
		return domain.KeyVersion{}, err
	}
	return k, nil
}
