// Package accounts manages resellers, customers and authentication principals.
//
// API tokens are random 32-byte values shown to the caller exactly once; only
// their SHA-256 hex digest is stored. Authentication is a constant-time hash
// comparison of the presented bearer token.
package accounts

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"

	"domainengine/internal/apierror"
	"domainengine/internal/models"
	"domainengine/internal/store"

	"github.com/jmoiron/sqlx"
)

type Service struct {
	db *sqlx.DB
}

func New(db *sqlx.DB) *Service { return &Service{db: db} }

// DB exposes the connection pool (e.g. for admin ledger operations).
func (s *Service) DB() *sqlx.DB { return s.db }

func hashToken(tok string) string {
	h := sha256.Sum256([]byte(tok))
	return hex.EncodeToString(h[:])
}

// NewToken generates a fresh opaque bearer token (returned once, in plaintext).
func NewToken() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return "dlt_" + hex.EncodeToString(b), nil
}

// CreateReseller creates a reseller with an optional starting credit balance
// and returns its login token (plaintext, one time).
func (s *Service) CreateReseller(ctx context.Context, name string, startingCents int64) (*models.Reseller, string, error) {
	if name == "" || startingCents < 0 {
		return nil, "", apierror.ErrBadRequest
	}
	var r models.Reseller
	token, err := NewToken()
	if err != nil {
		return nil, "", err
	}
	err = store.InTx(ctx, s.db, func(tx *sqlx.Tx) error {
		if err := tx.QueryRowxContext(ctx,
			`INSERT INTO resellers (name, balance_cents) VALUES ($1,$2) RETURNING *`,
			name, startingCents).StructScan(&r); err != nil {
			if store.IsUniqueViolation(err) {
				return apierror.ErrConflict
			}
			return err
		}
		_, err := tx.ExecContext(ctx,
			`INSERT INTO principals (role, reseller_id, username, token_hash)
			 VALUES ('reseller',$1,$2,$3)`,
			r.ID, "reseller:"+name, hashToken(token))
		return err
	})
	if err != nil {
		return nil, "", err
	}
	return &r, token, nil
}

// CreateCustomer creates a customer under a reseller and a customer principal.
func (s *Service) CreateCustomer(ctx context.Context, resellerID int64, name string) (*models.Customer, string, error) {
	if name == "" {
		return nil, "", apierror.ErrBadRequest
	}
	var c models.Customer
	token, err := NewToken()
	if err != nil {
		return nil, "", err
	}
	err = store.InTx(ctx, s.db, func(tx *sqlx.Tx) error {
		if err := tx.QueryRowxContext(ctx,
			`INSERT INTO customers (reseller_id, name) VALUES ($1,$2) RETURNING *`,
			resellerID, name).StructScan(&c); err != nil {
			if store.IsUniqueViolation(err) {
				return apierror.ErrConflict
			}
			return err
		}
		_, err := tx.ExecContext(ctx,
			`INSERT INTO principals (role, reseller_id, customer_id, username, token_hash)
			 VALUES ('customer',$1,$2,$3,$4)`,
			resellerID, c.ID, fmt.Sprintf("customer:%d:%s", resellerID, name), hashToken(token))
		return err
	})
	if err != nil {
		return nil, "", err
	}
	return &c, token, nil
}

// EnsureAdminPrincipal creates the bootstrap admin if no admin exists.
func (s *Service) EnsureAdminPrincipal(ctx context.Context) (string, bool, error) {
	var count int
	if err := s.db.GetContext(ctx, &count,
		`SELECT count(*) FROM principals WHERE role='admin'`); err != nil {
		return "", false, err
	}
	if count > 0 {
		return "", false, nil
	}
	token, err := NewToken()
	if err != nil {
		return "", false, err
	}
	_, err = s.db.ExecContext(ctx,
		`INSERT INTO principals (role, username, token_hash)
		 VALUES ('admin','admin',$1)
		 ON CONFLICT (username) DO NOTHING`, hashToken(token))
	if err != nil {
		return "", false, err
	}
	return token, true, nil
}

// Authenticate resolves a bearer token to a principal.
func (s *Service) Authenticate(ctx context.Context, token string) (*models.Principal, error) {
	if token == "" {
		return nil, apierror.ErrUnauthorized
	}
	want := hashToken(token)
	var p models.Principal
	if err := s.db.GetContext(ctx, &p,
		`SELECT * FROM principals WHERE token_hash=$1`, want); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, apierror.ErrUnauthorized
		}
		return nil, err
	}
	// Constant-time defense in depth (DB lookup is already hash-exact).
	if subtle.ConstantTimeCompare([]byte(p.TokenHash), []byte(want)) != 1 {
		return nil, apierror.ErrUnauthorized
	}
	return &p, nil
}

func (s *Service) GetReseller(ctx context.Context, id int64) (*models.Reseller, error) {
	var r models.Reseller
	err := s.db.GetContext(ctx, &r, `SELECT * FROM resellers WHERE id=$1`, id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, apierror.ErrNotFound
	}
	return &r, err
}

func (s *Service) GetCustomer(ctx context.Context, id int64) (*models.Customer, error) {
	var c models.Customer
	err := s.db.GetContext(ctx, &c, `SELECT * FROM customers WHERE id=$1`, id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, apierror.ErrNotFound
	}
	return &c, err
}

func (s *Service) ListResellers(ctx context.Context) ([]models.Reseller, error) {
	var out []models.Reseller
	err := s.db.SelectContext(ctx, &out, `SELECT * FROM resellers ORDER BY id`)
	return out, err
}

func (s *Service) ListCustomers(ctx context.Context, resellerID int64) ([]models.Customer, error) {
	var out []models.Customer
	err := s.db.SelectContext(ctx, &out,
		`SELECT * FROM customers WHERE reseller_id=$1 ORDER BY id`, resellerID)
	return out, err
}
