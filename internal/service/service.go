package service

import (
	"context"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/clearsettle/clearsettle/internal/db"
	"github.com/clearsettle/clearsettle/internal/domain"
	"github.com/clearsettle/clearsettle/internal/money"
)

// ErrUnbalancedEntry is returned if a posting set does not net to zero.
var ErrUnbalancedEntry = errors.New("refusing to post unbalanced ledger entries")

// pgxNoRows is a short alias for the sentinel returned for empty results.
var pgxNoRows = pgx.ErrNoRows

// Service holds the payment engine. All state changes happen inside
// serializable transactions; the ledger, payment state and balances therefore
// always commit together.
type Service struct {
	pool  *pgxpool.Pool
	q     *db.Queries
	clock domain.Clock
	hook  SettleHook

	// cancelFn, when set, is invoked by settlement after committing a payment
	// (tests simulate a process kill this way).
	cancelFn func()
}

// WithSettleHook installs a per-payment settlement callback (tests only).
func (s *Service) WithSettleHook(h SettleHook) *Service {
	s.hook = h
	return s
}

// SetTestCancel installs a context-cancel callback fired from the settle hook
// (tests only).
func (s *Service) SetTestCancel(fn func()) { s.cancelFn = fn }

// ExecRaw executes a statement outside the query abstraction (tests/admin).
func (s *Service) ExecRaw(ctx context.Context, sql string, args ...any) error {
	_, err := s.pool.Exec(ctx, sql, args...)
	return err
}

// QueryRowRaw runs a row-returning statement outside the query abstraction.
func (s *Service) QueryRowRaw(ctx context.Context, sql string, args ...any) pgx.Row {
	return s.pool.QueryRow(ctx, sql, args...)
}

func New(pool *pgxpool.Pool, clock domain.Clock) *Service {
	if clock == nil {
		clock = domain.RealClock{}
	}
	return &Service{pool: pool, q: db.New(pool), clock: clock}
}

// Actor identifies the authenticated caller for scoping and audit.
type Actor struct {
	KeyID      uuid.UUID
	Role       string
	MerchantID *uuid.UUID // nil for admins
}

const maxSerializationRetries = 5

// retryTx runs fn in a serializable transaction, retrying on 40001
// (serialization_failure) or 40P01 (deadlock_detected).
func (s *Service) retryTx(ctx context.Context, fn func(*db.Queries) error) error {
	return s.retryTxRaw(ctx, fn)
}

// retryTxIso runs fn in a transaction at the requested isolation level,
// retrying on 40001 (serialization_failure) or 40P01 (deadlock_detected).
func (s *Service) retryTxIso(ctx context.Context, iso pgx.TxIsoLevel, fn func(*db.Queries) error) error {
	var lastErr error
	for attempt := 0; attempt < maxSerializationRetries; attempt++ {
		err := storeInTxIso(ctx, s.pool, iso, func(tx pgx.Tx) error {
			return fn(s.q.WithTx(tx))
		})
		if err == nil {
			return nil
		}
		lastErr = err
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && (pgErr.Code == "40001" || pgErr.Code == "40P01") {
			continue
		}
		return err
	}
	return fmt.Errorf("transaction conflict after retries: %w", lastErr)
}

// inTx runs fn once in a serializable transaction without retry.
func (s *Service) inTx(ctx context.Context, fn func(*db.Queries) error) error {
	return storeInTxIso(ctx, s.pool, pgx.Serializable, func(tx pgx.Tx) error {
		return fn(s.q.WithTx(tx))
	})
}

// inTxReadCommitted runs fn once at READ COMMITTED (used by the row-locked
// per-payment settlement loop).
func (s *Service) inTxReadCommitted(ctx context.Context, fn func(*db.Queries) error) error {
	return storeInTxIso(ctx, s.pool, pgx.ReadCommitted, func(tx pgx.Tx) error {
		return fn(s.q.WithTx(tx))
	})
}

func storeInTxIso(ctx context.Context, pool *pgxpool.Pool, iso pgx.TxIsoLevel, fn func(pgx.Tx) error) error {
	tx, err := pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: iso})
	if err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback(ctx)
		return err
	}
	return tx.Commit(ctx)
}

func storeInTxSerializable(ctx context.Context, pool *pgxpool.Pool, fn func(pgx.Tx) error) error {
	tx, err := pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback(ctx)
		return err
	}
	return tx.Commit(ctx)
}

// requireMerchant enforces merchant scoping: non-admin callers may only touch
// their own merchant's resources.
func (a Actor) requireMerchant(merchantID uuid.UUID) error {
	if a.Role == "admin" {
		return nil
	}
	if a.MerchantID == nil || *a.MerchantID != merchantID {
		return domain.ErrForbidden
	}
	return nil
}

// audit appends an audit event. Called within a transaction so it commits
// atomically with the action it describes.
func audit(ctx context.Context, q *db.Queries, actor Actor, merchantID *uuid.UUID,
	action, targetType, targetID string, meta []byte) error {
	if len(meta) == 0 {
		meta = []byte("{}")
	}
	return q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
		ActorKeyID: &actor.KeyID,
		ActorRole:  actor.Role,
		MerchantID: merchantID,
		Action:     action,
		TargetType: targetType,
		TargetID:   targetID,
		Metadata:   meta,
	})
}

// merchantFee returns the configured fee schedule, defaulting to 2.9% + 30c.
func merchantFee(m db.Merchant) (int, int64) {
	bps := int(m.FeeBps)
	if bps == 0 {
		bps = money.DefaultFeeBPS
	}
	fixed := m.FeeFixedCents
	return bps, fixed
}
