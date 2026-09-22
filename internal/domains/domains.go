// Package domains implements domain registration, renewal, restoration, auth
// code rotation and the time-driven expiry sweep.
//
// Concurrency model: every mutating operation runs in one SERIALIZABLE
// transaction (see store.InTx), charges via the ledger inside that same
// transaction, and locks the affected domain row FOR UPDATE. The unique index
// on canonical_name is the final arbiter for "only one registration". Two
// racing registrations therefore end with exactly one owner and one charge;
// renewal racing the expiry sweep ends in one deterministic outcome (whichever
// commits wins, and the loser retries and sees the new state).
package domains

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"domainengine/internal/apierror"
	"domainengine/internal/authcode"
	clk "domainengine/internal/clock"
	"domainengine/internal/codec"
	"domainengine/internal/events"
	"domainengine/internal/ledger"
	"domainengine/internal/models"
	"domainengine/internal/names"
	"domainengine/internal/prices"
	"domainengine/internal/store"

	"github.com/jmoiron/sqlx"
)

// Timeline are the lifecycle durations (injected; tests shrink them).
type Timeline struct {
	ExpiredGrace  time.Duration // registered -> expired
	RedeemPeriod  time.Duration // expired -> redeemable (the 30-day redemption grace)
	PendingDelete time.Duration // redeemable -> pending_delete
}

type Service struct {
	db     *sqlx.DB
	ledger *ledger.Service
	prices *prices.Service
	codec  *codec.Codec
	tl     Timeline
}

func New(db *sqlx.DB, l *ledger.Service, p *prices.Service, c *codec.Codec, tl Timeline) *Service {
	return &Service{db: db, ledger: l, prices: p, codec: c, tl: tl}
}

// Result is returned by register/renew/restore; AuthCode is populated once,
// only by register and RotateAuthCode.
type Result struct {
	Domain   *models.Domain
	AuthCode string
	ChargeTx *models.BillingTransaction
}

// deadlines computes the four lifecycle instants for an expiry time.
func deadlines(expiresAt time.Time, tl Timeline) (expiredAt, redeemableAt, pendingDeleteAt, purgeAt time.Time) {
	return ComputeDeadlines(expiresAt, tl)
}

// ComputeDeadlines is the exported variant used by the transfer service when a
// completed transfer resets the lifecycle clock.
func ComputeDeadlines(expiresAt time.Time, tl Timeline) (expiredAt, redeemableAt, pendingDeleteAt, purgeAt time.Time) {
	expiredAt = expiresAt.Add(tl.ExpiredGrace)
	redeemableAt = expiredAt.Add(tl.RedeemPeriod)
	pendingDeleteAt = redeemableAt.Add(tl.PendingDelete)
	// Release 5 days after entering pending_delete:
	purgeAt = pendingDeleteAt.Add(5 * 24 * time.Hour)
	return
}

// Register creates a domain and charges the reseller atomically.
func (s *Service) Register(ctx context.Context, clock clk.Clock, req RegisterRequest) (*Result, error) {
	if req.Years < 1 || req.Years > 10 {
		return nil, apierror.ErrInvalidYears
	}
	canon, err := names.Normalize(req.Name)
	if err != nil {
		return nil, apierror.ErrInvalidName
	}
	tld, err := names.TLD(req.Name)
	if err != nil {
		return nil, apierror.ErrInvalidName
	}
	now := clock.Now()

	var res Result
	err = store.InTx(ctx, s.db, func(tx *sqlx.Tx) error {
		// Idempotent replay of a completed registration: return the original
		// domain/charge (the one-time auth code is not revealed again).
		if req.IdempotencyKey != nil {
			if err := ledger.LockIdempotency(ctx, tx, req.ResellerID, models.TxRegister, *req.IdempotencyKey); err != nil {
				return err
			}
		}
		if prior, err := ledger.FindTx(ctx, tx, req.ResellerID, models.TxRegister, req.IdempotencyKey); err != nil {
			return err
		} else if prior != nil && prior.DomainID != nil {
			d, err := domainByID(ctx, tx, *prior.DomainID)
			if err != nil {
				return err
			}
			res = Result{Domain: d, ChargeTx: prior}
			return nil
		}

		// Price is read and charged inside the same tx, so it cannot change
		// between decision and charge.
		price, err := s.prices.CurrentTx(ctx, tx, tld)
		if err != nil {
			return err
		}

		// Fast uniqueness check with a row lock on any existing row.
		var existing models.Domain
		err = tx.GetContext(ctx, &existing,
			`SELECT * FROM domains WHERE canonical_name=$1 FOR UPDATE`, canon)
		switch {
		case err == nil:
			return apierror.ErrDomainTaken
		case !errors.Is(err, sql.ErrNoRows):
			return err
		}

		years := req.Years
		amount := price.RegisterCents * int64(years)
		bt, err := s.ledger.PostTx(ctx, tx, ledger.Post{
			ResellerID:     req.ResellerID,
			Kind:           models.TxRegister,
			AvailableDelta: -amount,
			DomainID:       nil, // domain does not exist yet; linked below
			Years:          &years,
			IdempotencyKey: req.IdempotencyKey,
		})
		if err != nil {
			return err
		}

		code, err := authcode.New()
		if err != nil {
			return err
		}
		cipher, err := s.codec.Encrypt(code)
		if err != nil {
			return err
		}
		expiresAt := now.AddDate(req.Years, 0, 0)
		exp, red, pend, purge := deadlines(expiresAt, s.tl)

		var d models.Domain
		if err := tx.QueryRowxContext(ctx, `
			INSERT INTO domains
			  (name, tld, canonical_name, customer_id, reseller_id, status,
			   expires_at, expired_at, redeemable_at, pending_delete_at, purge_at, auth_cipher)
			VALUES ($1,$2,$3,$4,$5,'registered',$6,$7,$8,$9,$10,$11)
			RETURNING *`,
			canon, tld, canon, req.CustomerID, req.ResellerID,
			expiresAt, exp, red, pend, purge, cipher).StructScan(&d); err != nil {
			if c, ok := store.UniqueConstraint(err); ok && c == "domains_canonical_name_key" {
				return apierror.ErrDomainTaken
			}
			return err
		}
		// Link the charge to the domain (price was already committed as owed).
		if _, err := tx.ExecContext(ctx,
			`UPDATE billing_transactions SET domain_id=$1 WHERE id=$2`, d.ID, bt.ID); err != nil {
			return err
		}
		amt := amount
		if err := events.Insert(ctx, tx, models.DomainEvent{
			DomainID: d.ID, DomainName: canon, EventType: models.EventRegistered,
			ToStatus: strptr(models.StatusRegistered), AmountCents: &amt,
			Detail: events.MustDetail(map[string]any{
				"years": years, "tld": tld,
				"expires_at":           expiresAt,
				"price_cents_per_year": price.RegisterCents,
			}),
		}); err != nil {
			return err
		}
		res = Result{Domain: &d, AuthCode: code, ChargeTx: bt}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &res, nil
}

type RegisterRequest struct {
	Name           string
	CustomerID     int64
	ResellerID     int64
	Years          int
	IdempotencyKey *string
}

// Renew extends registration from the current expiry date (not from "now") and
// charges per-year renew price. Only registered domains can renew.
func (s *Service) Renew(ctx context.Context, clock clk.Clock, req RenewRequest) (*Result, error) {
	if req.Years < 1 || req.Years > 10 {
		return nil, apierror.ErrInvalidYears
	}
	canon, err := names.Normalize(req.Name)
	if err != nil {
		return nil, apierror.ErrInvalidName
	}
	var res Result
	err = store.InTx(ctx, s.db, func(tx *sqlx.Tx) error {
		d, err := lockDomain(ctx, tx, canon)
		if err != nil {
			return err
		}
		if req.ResellerID != d.ResellerID {
			return apierror.ErrForbidden
		}
		if d.Status != models.StatusRegistered {
			return apierror.ErrInvalidState
		}
		if err := ledger.LockIdempotency(ctx, tx, d.ResellerID, models.TxRenew,
			idemKey(req.IdempotencyKey)); err != nil {
			return err
		}
		if prior, err := ledger.FindTx(ctx, tx, d.ResellerID, models.TxRenew, req.IdempotencyKey); err != nil {
			return err
		} else if prior != nil {
			res = Result{Domain: d, ChargeTx: prior}
			return nil
		}
		price, err := s.prices.CurrentTx(ctx, tx, d.TLD)
		if err != nil {
			return err
		}
		years := req.Years
		amount := price.RenewCents * int64(years)
		bt, err := s.ledger.PostTx(ctx, tx, ledger.Post{
			ResellerID:     d.ResellerID,
			Kind:           models.TxRenew,
			AvailableDelta: -amount,
			DomainID:       &d.ID,
			Years:          &years,
			IdempotencyKey: req.IdempotencyKey,
		})
		if err != nil {
			return err
		}
		newExpiry := d.ExpiresAt.AddDate(years, 0, 0)
		exp, red, pend, purge := deadlines(newExpiry, s.tl)
		if err := tx.QueryRowxContext(ctx, `
			UPDATE domains SET status='registered',
				expires_at=$1, expired_at=$2, redeemable_at=$3,
				pending_delete_at=$4, purge_at=$5, updated_at=$6
			WHERE id=$7 RETURNING *`,
			newExpiry, exp, red, pend, purge, clock.Now(), d.ID).StructScan(d); err != nil {
			return err
		}
		amt := amount
		if err := events.Insert(ctx, tx, models.DomainEvent{
			DomainID: d.ID, DomainName: canon, EventType: models.EventRenewed,
			FromStatus:  strptr(models.StatusRegistered),
			ToStatus:    strptr(models.StatusRegistered),
			AmountCents: &amt,
			Detail: events.MustDetail(map[string]any{
				"years": years, "expires_at": newExpiry,
			}),
		}); err != nil {
			return err
		}
		res = Result{Domain: d, ChargeTx: bt}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &res, nil
}

type RenewRequest struct {
	Name           string
	ResellerID     int64
	Years          int
	IdempotencyKey *string
}

// Restore brings an expired or redeemable domain back to registered, charging
// the flat redemption fee plus one renewal year and extending by one year. A
// pending_delete domain can no longer be restored.
func (s *Service) Restore(ctx context.Context, clock clk.Clock, req RestoreRequest) (*Result, error) {
	canon, err := names.Normalize(req.Name)
	if err != nil {
		return nil, apierror.ErrInvalidName
	}
	var res Result
	err = store.InTx(ctx, s.db, func(tx *sqlx.Tx) error {
		d, err := lockDomain(ctx, tx, canon)
		if err != nil {
			return err
		}
		if req.ResellerID != d.ResellerID {
			return apierror.ErrForbidden
		}
		if d.Status != models.StatusExpired && d.Status != models.StatusRedeemable {
			return apierror.ErrInvalidState
		}
		if err := ledger.LockIdempotency(ctx, tx, d.ResellerID, models.TxRestore,
			idemKey(req.IdempotencyKey)); err != nil {
			return err
		}
		if prior, err := ledger.FindTx(ctx, tx, d.ResellerID, models.TxRestore, req.IdempotencyKey); err != nil {
			return err
		} else if prior != nil {
			res = Result{Domain: d, ChargeTx: prior}
			return nil
		}
		price, err := s.prices.CurrentTx(ctx, tx, d.TLD)
		if err != nil {
			return err
		}
		amount := price.RestoreCents + price.RenewCents
		one := 1
		bt, err := s.ledger.PostTx(ctx, tx, ledger.Post{
			ResellerID:     d.ResellerID,
			Kind:           models.TxRestore,
			AvailableDelta: -amount,
			DomainID:       &d.ID,
			Years:          &one,
			IdempotencyKey: req.IdempotencyKey,
		})
		if err != nil {
			return err
		}
		// Restored names get a fresh year from "now" (they were past expiry).
		newExpiry := clock.Now().AddDate(1, 0, 0)
		exp, red, pend, purge := deadlines(newExpiry, s.tl)
		from := d.Status
		if err := tx.QueryRowxContext(ctx, `
			UPDATE domains SET status='registered',
				expires_at=$1, expired_at=$2, redeemable_at=$3,
				pending_delete_at=$4, purge_at=$5, updated_at=$6
			WHERE id=$7 RETURNING *`,
			newExpiry, exp, red, pend, purge, clock.Now(), d.ID).StructScan(d); err != nil {
			return err
		}
		amt := amount
		if err := events.Insert(ctx, tx, models.DomainEvent{
			DomainID: d.ID, DomainName: canon, EventType: models.EventRestored,
			FromStatus: &from, ToStatus: strptr(models.StatusRegistered),
			AmountCents: &amt,
		}); err != nil {
			return err
		}
		res = Result{Domain: d, ChargeTx: bt}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &res, nil
}

type RestoreRequest struct {
	Name           string
	ResellerID     int64
	IdempotencyKey *string
}

// RotateAuthCode issues a new 16-char code; the old one stops working. The new
// code is returned once.
func (s *Service) RotateAuthCode(ctx context.Context, resellerID int64, name string) (string, *models.Domain, error) {
	canon, err := names.Normalize(name)
	if err != nil {
		return "", nil, apierror.ErrInvalidName
	}
	var code string
	var out *models.Domain
	err = store.InTx(ctx, s.db, func(tx *sqlx.Tx) error {
		d, err := lockDomain(ctx, tx, canon)
		if err != nil {
			return err
		}
		if resellerID != d.ResellerID {
			return apierror.ErrForbidden
		}
		code, err = authcode.New()
		if err != nil {
			return err
		}
		cipher, err := s.codec.Encrypt(code)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx,
			`UPDATE domains SET auth_cipher=$1, updated_at=$2 WHERE id=$3`,
			cipher, time.Now(), d.ID); err != nil {
			return err
		}
		d.AuthCipher = cipher
		if err := events.Insert(ctx, tx, models.DomainEvent{
			DomainID: d.ID, DomainName: canon,
			EventType: models.EventAuthRotated,
		}); err != nil {
			return err
		}
		out = d
		return nil
	})
	if err != nil {
		return "", nil, err
	}
	return code, out, nil
}

// CheckAuth decrypts a domain's stored code and compares it in constant time.
func (s *Service) CheckAuth(d *models.Domain, candidate string) (bool, error) {
	plain, err := s.codec.Decrypt(d.AuthCipher)
	if err != nil {
		return false, err
	}
	return codec.ConstantTimeEqual(plain, candidate), nil
}

func lockDomain(ctx context.Context, tx *sqlx.Tx, canon string) (*models.Domain, error) {
	var d models.Domain
	err := tx.GetContext(ctx, &d,
		`SELECT * FROM domains WHERE canonical_name=$1 FOR UPDATE`, canon)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, apierror.ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &d, nil
}

// Get returns a domain by canonical name (no scoping; callers enforce RBAC).
func (s *Service) Get(ctx context.Context, name string) (*models.Domain, error) {
	canon, err := names.Normalize(name)
	if err != nil {
		return nil, apierror.ErrInvalidName
	}
	var d models.Domain
	err = s.db.GetContext(ctx, &d, `SELECT * FROM domains WHERE canonical_name=$1`, canon)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, apierror.ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &d, nil
}

// GetByID returns a domain by id.
func (s *Service) GetByID(ctx context.Context, id int64) (*models.Domain, error) {
	var d models.Domain
	err := s.db.GetContext(ctx, &d, `SELECT * FROM domains WHERE id=$1`, id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, apierror.ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &d, nil
}

// Scope restricts a list query to what a principal may see.
type Scope struct {
	ResellerID int64 // 0 = no reseller restriction (admin)
	CustomerID int64 // 0 = all of the reseller's customers
}

// List returns domains visible to a scope.
func (s *Service) List(ctx context.Context, scope Scope, limit int) ([]models.Domain, error) {
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	q := `SELECT * FROM domains`
	args := []any{}
	switch {
	case scope.CustomerID != 0:
		q += ` WHERE customer_id=$1`
		args = append(args, scope.CustomerID)
	case scope.ResellerID != 0:
		q += ` WHERE reseller_id=$1`
		args = append(args, scope.ResellerID)
	}
	q += ` ORDER BY id DESC LIMIT $` + itoa(len(args)+1)
	args = append(args, limit)
	var out []models.Domain
	err := s.db.SelectContext(ctx, &out, q, args...)
	return out, err
}

// Sweep advances all time-driven status transitions due as of now. It is
// idempotent and restart-safe: each transition is one independent
// transaction, so a crash mid-sweep leaves every row either old or new and a
// re-run completes the rest. SERIALIZABLE + the row lock makes the outcome
// against a concurrent renewal deterministic.
//
// Returns the number of domains transitioned.
func (s *Service) Sweep(ctx context.Context, clock clk.Clock) (int, error) {
	now := clock.Now()
	// Work in small locked batches to keep transactions short.
	const batch = 100
	transitions := 0
	for {
		n, err := s.sweepBatch(ctx, now, batch)
		if err != nil {
			return transitions, err
		}
		transitions += n
		if n < batch {
			break
		}
	}
	return transitions, nil
}

func (s *Service) sweepBatch(ctx context.Context, now time.Time, batch int) (int, error) {
	type due struct {
		ID     int64  `db:"id"`
		Status string `db:"status"`
	}
	var rows []due
	// A domain is due when its status-specific deadline has passed.
	err := s.db.SelectContext(ctx, &rows, `
		SELECT id, status FROM domains
		WHERE status <> 'transferring'
		  AND (
		    (status='registered'      AND expired_at        IS NOT NULL AND expired_at <= $1) OR
		    (status='expired'         AND redeemable_at     IS NOT NULL AND redeemable_at <= $1) OR
		    (status='redeemable'      AND pending_delete_at IS NOT NULL AND pending_delete_at <= $1) OR
		    (status='pending_delete'  AND purge_at          IS NOT NULL AND purge_at <= $1)
		  )
		ORDER BY id
		LIMIT $2`, now, batch)
	if err != nil {
		return 0, err
	}
	for _, r := range rows {
		if err := s.transitionOne(ctx, now, r.ID); err != nil {
			return 0, err
		}
	}
	return len(rows), nil
}

// transitionOne performs exactly one due transition for one domain.
func (s *Service) transitionOne(ctx context.Context, now time.Time, id int64) error {
	return store.InTx(ctx, s.db, func(tx *sqlx.Tx) error {
		var d models.Domain
		if err := tx.GetContext(ctx, &d,
			`SELECT * FROM domains WHERE id=$1 FOR UPDATE`, id); err != nil {
			if errors.Is(err, sql.ErrNoRows) {
				return nil // purged by a previous run
			}
			return err
		}
		switch d.Status {
		case models.StatusRegistered:
			if d.ExpiredAt == nil || now.Before(*d.ExpiredAt) {
				return nil // racing renewal pushed the deadline out
			}
			if _, err := tx.ExecContext(ctx,
				`UPDATE domains SET status='expired', updated_at=$1 WHERE id=$2`,
				now, id); err != nil {
				return err
			}
			return events.Insert(ctx, tx, models.DomainEvent{
				DomainID: id, DomainName: d.CanonicalName, EventType: models.EventExpired,
				FromStatus: strptr(models.StatusRegistered),
				ToStatus:   strptr(models.StatusExpired),
			})
		case models.StatusExpired:
			if d.RedeemableAt == nil || now.Before(*d.RedeemableAt) {
				return nil
			}
			if _, err := tx.ExecContext(ctx,
				`UPDATE domains SET status='redeemable', updated_at=$1 WHERE id=$2`,
				now, id); err != nil {
				return err
			}
			return events.Insert(ctx, tx, models.DomainEvent{
				DomainID: id, DomainName: d.CanonicalName, EventType: models.EventEnteredRedeem,
				FromStatus: strptr(models.StatusExpired),
				ToStatus:   strptr(models.StatusRedeemable),
			})
		case models.StatusRedeemable:
			if d.PendingDeleteAt == nil || now.Before(*d.PendingDeleteAt) {
				return nil
			}
			if _, err := tx.ExecContext(ctx,
				`UPDATE domains SET status='pending_delete', updated_at=$1 WHERE id=$2`,
				now, id); err != nil {
				return err
			}
			return events.Insert(ctx, tx, models.DomainEvent{
				DomainID: id, DomainName: d.CanonicalName, EventType: models.EventEnteredPending,
				FromStatus: strptr(models.StatusRedeemable),
				ToStatus:   strptr(models.StatusPendingDelete),
			})
		case models.StatusPendingDelete:
			if d.PurgeAt == nil || now.Before(*d.PurgeAt) {
				return nil
			}
			// Release the name for re-registration. Ledger/transfers/events
			// rows are retained; the events row notes the purge.
			if _, err := tx.ExecContext(ctx, `DELETE FROM domains WHERE id=$1`, id); err != nil {
				return err
			}
			return events.Insert(ctx, tx, models.DomainEvent{
				DomainID: id, DomainName: d.CanonicalName, EventType: models.EventPurged,
				FromStatus: strptr(models.StatusPendingDelete),
			})
		}
		return nil
	})
}

func strptr(s string) *string { return &s }

func idemKey(k *string) string {
	if k == nil {
		return ""
	}
	return *k
}

func domainByID(ctx context.Context, tx *sqlx.Tx, id int64) (*models.Domain, error) {
	var d models.Domain
	if err := tx.GetContext(ctx, &d, `SELECT * FROM domains WHERE id=$1`, id); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, apierror.ErrNotFound
		}
		return nil, err
	}
	return &d, nil
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [12]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
