// Package store owns the PostgreSQL connection pool, schema migration and
// service_meta key/value bootstrap.
package store

import (
	"context"
	_ "embed"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"deadlockcheck/internal/evidence"
)

//go:embed schema.sql
var schemaSQL string

// Store wraps the connection pool.
type Store struct {
	Pool *pgxpool.Pool
}

// Open connects to the database and waits for it to accept connections
// (the container may still be starting up).
func Open(ctx context.Context, url string) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, fmt.Errorf("parse database url: %w", err)
	}
	cfg.MaxConns = 10

	var pool *pgxpool.Pool
	var lastErr error
	for attempt := 0; attempt < 30; attempt++ {
		pool, lastErr = pgxpool.NewWithConfig(ctx, cfg)
		if lastErr == nil {
			if pingErr := pool.Ping(ctx); pingErr == nil {
				return &Store{Pool: pool}, nil
			} else {
				lastErr = pingErr
				pool.Close()
			}
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		default:
		}
		time.Sleep(time.Duration(attempt+1) * 200 * time.Millisecond)
	}
	return nil, fmt.Errorf("connect to postgres after retries: %w", lastErr)
}

// Migrate executes the idempotent embedded schema.
func (s *Store) Migrate(ctx context.Context) error {
	if _, err := s.Pool.Exec(ctx, schemaSQL); err != nil {
		return fmt.Errorf("run schema migration: %w", err)
	}
	return nil
}

// BootstrapSigner loads the HMAC key from service_meta, creating a random
// one when none exists (or using the provided override). The key persists
// across restarts so previously issued evidence stays verifiable.
func (s *Store) BootstrapSigner(ctx context.Context, override string) (*evidence.Signer, error) {
	tx, err := s.Pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)

	var stored string
	err = tx.QueryRow(ctx,
		`SELECT value FROM service_meta WHERE key = 'evidence_key' FOR UPDATE`).
		Scan(&stored)

	switch {
	case err == nil:
		// existing key wins: an override cannot silently replace the key
		// that signed historical evidence.
	case err == pgx.ErrNoRows:
		if override != "" {
			stored = override
		} else {
			stored, err = evidence.GenerateKey()
			if err != nil {
				return nil, err
			}
		}
		if _, err = tx.Exec(ctx,
			`INSERT INTO service_meta (key, value) VALUES ('evidence_key', $1)`,
			stored); err != nil {
			return nil, err
		}
	default:
		return nil, err
	}

	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	key, err := evidence.ParseKey(stored)
	if err != nil {
		return nil, err
	}
	return evidence.NewSigner(key), nil
}

// SchemaVersion reports the migrated schema version from service_meta.
func (s *Store) SchemaVersion(ctx context.Context) (string, error) {
	var v string
	err := s.Pool.QueryRow(ctx,
		`SELECT value FROM service_meta WHERE key = 'schema_version'`).Scan(&v)
	return v, err
}
