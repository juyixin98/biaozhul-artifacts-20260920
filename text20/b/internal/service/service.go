package service

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"signalboard/internal/db"
)

var (
	ErrNotFound        = errors.New("not found")
	ErrConflict        = errors.New("conflict")
	ErrValidation      = errors.New("validation error")
	ErrPrecondition    = errors.New("precondition failed")
	ErrUnauthorized    = errors.New("unauthorized")
	ErrForbidden       = errors.New("forbidden")
	ErrBatchRolledBack = errors.New("batch rolled back")
)

// pgUniqueViolation / exclusion violation share SQLSTATE 23505 / 23P01.
const (
	pgCodeUniqueViolation    = "23505"
	pgCodeExclusionViolation = "23P01"
)

type Service struct {
	pool *pgxpool.Pool
	q    *db.Queries
}

func New(pool *pgxpool.Pool) *Service {
	return &Service{pool: pool, q: db.New(pool)}
}

// tx runs fn inside a transaction with the generated Queries bound to it.
func (s *Service) tx(ctx context.Context, fn func(q *db.Queries) error) error {
	return pgx.BeginTxFunc(ctx, s.pool, pgx.TxOptions{}, func(tx pgx.Tx) error {
		return fn(s.q.WithTx(tx))
	})
}

// pgxRepeatableRead runs fn in a snapshot-isolation transaction. Used for
// reads that must present one consistent state (screen menu).
func pgxRepeatableRead(ctx context.Context, pool *pgxpool.Pool, fn func(q *db.Queries) error) error {
	return pgx.BeginTxFunc(ctx, pool, pgx.TxOptions{IsoLevel: pgx.RepeatableRead},
		func(tx pgx.Tx) error {
			return fn(db.New(tx))
		})
}

func isPgCode(err error, code string) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == code
}
