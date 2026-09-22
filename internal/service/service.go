package service

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/jmoiron/sqlx"

	"domainengine/internal/clock"
)

// Config holds lifecycle timings. All durations are in days so tests can
// inject a fake clock instead of sleeping.
type Config struct {
	ExpiredGraceDays            int // days a domain stays in `expired` before redemption
	RedemptionDays              int // redemption window (spec: 30)
	PendingDeleteDays           int // days in pending_delete before the name is released
	TransferWaitDays            int // simulated registry wait after approval (spec: 5)
	TransferApprovalTimeoutDays int // how long a transfer waits for approval
}

func (c Config) WithDefaults() Config {
	if c.ExpiredGraceDays <= 0 {
		c.ExpiredGraceDays = 30
	}
	if c.RedemptionDays <= 0 {
		c.RedemptionDays = 30
	}
	if c.PendingDeleteDays <= 0 {
		c.PendingDeleteDays = 5
	}
	if c.TransferWaitDays <= 0 {
		c.TransferWaitDays = 5
	}
	if c.TransferApprovalTimeoutDays <= 0 {
		c.TransferApprovalTimeoutDays = 7
	}
	return c
}

type Service struct {
	db      *sqlx.DB
	clock   clock.Clock
	cfg     Config
	authKey []byte // 32-byte AES key for transfer auth codes
}

func New(db *sqlx.DB, clk clock.Clock, cfg Config, authKey []byte) *Service {
	return &Service{db: db, clock: clk, cfg: cfg.WithDefaults(), authKey: authKey}
}

func (s *Service) Config() Config { return s.cfg }

// idempotent runs fn inside a single transaction together with the
// idempotency record. On success the (status, body) pair is stored against
// the key; a retry with the same key replays the stored response without
// re-executing — so no duplicate charges and no duplicate state changes.
// If fn fails the whole transaction (including the key row) rolls back, so
// failed requests are never recorded and never charge.
//
// Concurrency: the INSERT ... ON CONFLICT DO NOTHING blocks on the unique
// index until a concurrent same-key transaction commits or aborts, so
// exactly one caller executes fn.
func (s *Service) idempotent(ctx context.Context, key, userID, op string, fn func(ctx context.Context, tx *sqlx.Tx) (int, any, error)) (int, any, error) {
	if key == "" {
		return 0, nil, ErrValidation("idempotency_key is required")
	}
	scoped := userID + ":" + key

	var stored struct {
		StatusCode int             `db:"status_code"`
		Response   json.RawMessage `db:"response"`
	}
	err := s.db.GetContext(ctx, &stored,
		`SELECT status_code, response FROM idempotency_keys WHERE key = $1`, scoped)
	if err == nil {
		return stored.StatusCode, stored.Response, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return 0, nil, err
	}

	tx, err := s.db.BeginTxx(ctx, nil)
	if err != nil {
		return 0, nil, err
	}
	defer tx.Rollback()

	res, err := tx.ExecContext(ctx,
		`INSERT INTO idempotency_keys (key, user_id, operation, status_code, response)
		 VALUES ($1, $2, $3, 0, '{}') ON CONFLICT (key) DO NOTHING`, scoped, userID, op)
	if err != nil {
		return 0, nil, err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		// A concurrent request with the same key committed first; replay it.
		tx.Rollback()
		if err := s.db.GetContext(ctx, &stored,
			`SELECT status_code, response FROM idempotency_keys WHERE key = $1`, scoped); err != nil {
			return 0, nil, fmt.Errorf("idempotency replay: %w", err)
		}
		return stored.StatusCode, stored.Response, nil
	}

	status, body, err := fn(ctx, tx)
	if err != nil {
		return 0, nil, err // rollback: nothing stored, nothing charged
	}
	raw, err := json.Marshal(body)
	if err != nil {
		return 0, nil, err
	}
	if _, err := tx.ExecContext(ctx,
		`UPDATE idempotency_keys SET status_code = $1, response = $2 WHERE key = $3`,
		status, raw, scoped); err != nil {
		return 0, nil, err
	}
	if err := tx.Commit(); err != nil {
		return 0, nil, err
	}
	return status, body, nil
}

func (s *Service) addEvent(ctx context.Context, tx *sqlx.Tx, domainID *string, domainName, event string, detail map[string]any) error {
	raw, err := json.Marshal(detail)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx,
		`INSERT INTO domain_events (domain_id, domain_name, event, detail) VALUES ($1, $2, $3, $4)`,
		domainID, domainName, event, raw)
	return err
}
