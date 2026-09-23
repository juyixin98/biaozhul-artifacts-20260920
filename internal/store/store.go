// Package store persists a per-record conversion audit trail to
// PostgreSQL. Payloads are hashed with SHA-256 for integrity reference.
package store

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/jackc/pgx/v5/pgxpool"
)

// AuditRecord is one converted (or failed) stream item.
type AuditRecord struct {
	RecordID         string
	Direction        string // "v1->v2" or "v2->v1"
	SourceVersion    string
	TargetVersion    string
	ConverterVersion string
	MappingVersion   string
	Status           string // "ok" or "error"
	ErrorDetail      *ErrorDetail
	PayloadSHA256    string // hex
}

// ErrorDetail mirrors convert.Error for JSONB persistence.
type ErrorDetail struct {
	FieldPath string `json:"field_path"`
	Reason    string `json:"reason"`
	RawValue  string `json:"raw_value"`
}

// Store writes audit rows. The nil *Store is a valid no-op store.
type Store struct {
	pool *pgxpool.Pool
}

// Connect opens a pool and verifies it with a ping.
func Connect(ctx context.Context, dsn string) (*Store, error) {
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return nil, fmt.Errorf("connect postgres: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping postgres: %w", err)
	}
	return &Store{pool: pool}, nil
}

// Close releases the pool.
func (s *Store) Close() {
	if s != nil && s.pool != nil {
		s.pool.Close()
	}
}

// Migrate applies the schema (idempotent).
func (s *Store) Migrate(ctx context.Context, ddl string) error {
	if _, err := s.pool.Exec(ctx, ddl); err != nil {
		return fmt.Errorf("migrate: %w", err)
	}
	return nil
}

// InsertAudit writes one audit row.
func (s *Store) InsertAudit(ctx context.Context, rec AuditRecord) error {
	var detail []byte
	if rec.ErrorDetail != nil {
		b, err := json.Marshal(rec.ErrorDetail)
		if err != nil {
			return fmt.Errorf("marshal error detail: %w", err)
		}
		detail = b
	}
	_, err := s.pool.Exec(ctx, `
		INSERT INTO conversion_audit
		  (record_id, direction, source_version, target_version,
		   converter_version, mapping_version, status, error_detail, payload_sha256)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
		rec.RecordID, rec.Direction, rec.SourceVersion, rec.TargetVersion,
		rec.ConverterVersion, rec.MappingVersion, rec.Status, detail, rec.PayloadSHA256)
	if err != nil {
		return fmt.Errorf("insert audit: %w", err)
	}
	return nil
}

// CountAudits is a test/inspection helper.
func (s *Store) CountAudits(ctx context.Context) (int64, error) {
	var n int64
	if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM conversion_audit`).Scan(&n); err != nil {
		return 0, err
	}
	return n, nil
}
