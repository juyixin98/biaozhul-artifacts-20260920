// Package store owns the PostgreSQL connection pool, schema migration and
// durable key material used to sign resource-holding evidence tokens.
package store

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	_ "embed"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed schema.sql
var schemaSQL string

// ErrConflict is returned by service when an optimistic/state guard fails.
var ErrConflict = errors.New("state conflict")

// Store wraps the connection pool.
type Store struct {
	Pool *pgxpool.Pool
}

// Open connects, verifies the connection and runs the idempotent migration.
func Open(ctx context.Context, dsn string) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse dsn: %w", err)
	}
	cfg.MaxConns = 10
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping: %w", err)
	}
	s := &Store{Pool: pool}
	if err := s.migrate(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) migrate(ctx context.Context) error {
	if _, err := s.Pool.Exec(ctx, schemaSQL); err != nil {
		return fmt.Errorf("migrate: %w", err)
	}
	return nil
}

func (s *Store) Close() { s.Pool.Close() }

// EvidenceKey returns the Ed25519 signing key, creating and persisting one
// on first use. The 32-byte seed lives in meta so signatures stay valid
// across restarts and are verifiable by anyone holding the public key.
func (s *Store) EvidenceKey(ctx context.Context) (ed25519.PrivateKey, ed25519.PublicKey, error) {
	var seedB64 string
	err := s.Pool.QueryRow(ctx, `SELECT value FROM meta WHERE key='evidence_seed'`).Scan(&seedB64)
	if errors.Is(err, pgx.ErrNoRows) {
		seed := make([]byte, ed25519.SeedSize)
		if _, err := rand.Read(seed); err != nil {
			return nil, nil, err
		}
		seedB64 = base64.StdEncoding.EncodeToString(seed)
		if _, err := s.Pool.Exec(ctx,
			`INSERT INTO meta(key,value) VALUES('evidence_seed',$1)
			 ON CONFLICT (key) DO NOTHING`, seedB64); err != nil {
			return nil, nil, err
		}
	} else if err != nil {
		return nil, nil, err
	}
	seed, err := base64.StdEncoding.DecodeString(seedB64)
	if err != nil {
		return nil, nil, err
	}
	priv := ed25519.NewKeyFromSeed(seed)
	pub, ok := priv.Public().(ed25519.PublicKey)
	if !ok {
		return nil, nil, fmt.Errorf("unexpected public key type")
	}
	return priv, pub, nil
}

// PublicKeyJSON exposes the verify key for API clients / auditors.
func (s *Store) PublicKeyJSON(ctx context.Context) (json.RawMessage, error) {
	_, pub, err := s.EvidenceKey(ctx)
	if err != nil {
		return nil, err
	}
	return json.Marshal(map[string]string{
		"alg":       "EdDSA",
		"curve":     "Ed25519",
		"publicKey": base64.StdEncoding.EncodeToString(pub),
	})
}
