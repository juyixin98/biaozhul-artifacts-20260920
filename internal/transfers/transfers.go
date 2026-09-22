// Package transfers implements inter-reseller domain transfers.
//
// Lifecycle and billing timeline (all durations are injected):
//
//	request          gaining reseller posts name + 16-char auth code + target
//	                 customer. The transfer fee is frozen from the gaining
//	                 reseller's available credits into the held pool; the
//	                 domain enters "transferring". The frozen amount is
//	                 snapshotted here and is immune to later price changes.
//	approval window  the losing reseller has 5 days to approve or reject.
//	                 - reject  : transfer "rejected", frozen credits released
//	                 - approve : state becomes seller_approved; the registry's
//	                              simulated 5-day transfer wait begins.
//	                 - timeout : transfer "failed", frozen credits released.
//	completion       once the 5-day wait elapses, the worker captures the held
//	                 fee (held -> consumed), extends expiry by one year, moves
//	                 ownership, and the domain returns to "registered".
//	cancel           the gaining reseller may cancel while pending; the freeze
//	                 is released.
//
// Every movement is one SERIALIZABLE transaction that updates domain status,
// transfer state, and ledger balances together, so failure never moves money
// without state or vice versa.
package transfers

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"domainengine/internal/apierror"
	clk "domainengine/internal/clock"
	"domainengine/internal/domains"
	"domainengine/internal/events"
	"domainengine/internal/ledger"
	"domainengine/internal/models"
	"domainengine/internal/prices"
	"domainengine/internal/store"

	"github.com/jmoiron/sqlx"
)

type Config struct {
	ApprovalWindow time.Duration // seller approve/reject window and total TTL
	TransferWait   time.Duration // simulated registry wait after approval (5 days)
	Timeline       domains.Timeline
}

type Service struct {
	db      *sqlx.DB
	domains *domains.Service
	prices  *prices.Service
	ledger  *ledger.Service
	cfg     Config
}

func New(db *sqlx.DB, d *domains.Service, p *prices.Service, l *ledger.Service, cfg Config) *Service {
	return &Service{db: db, domains: d, prices: p, ledger: l, cfg: cfg}
}

// Request starts a transfer.
func (s *Service) Request(ctx context.Context, clock clk.Clock, req RequestInput) (*models.Transfer, *models.BillingTransaction, error) {
	d, err := s.domains.Get(ctx, req.DomainName)
	if err != nil {
		return nil, nil, err
	}
	if req.ToResellerID == d.ResellerID {
		return nil, nil, apierror.ErrSameReseller
	}
	ok, err := s.domains.CheckAuth(d, req.AuthCode)
	if err != nil {
		return nil, nil, err
	}
	if !ok {
		return nil, nil, apierror.ErrBadAuthCode
	}
	now := clock.Now()
	var t models.Transfer
	var bt *models.BillingTransaction
	err = store.InTx(ctx, s.db, func(tx *sqlx.Tx) error {
		// Lock the domain and re-check state under the lock.
		var dd models.Domain
		if err := tx.GetContext(ctx, &dd,
			`SELECT * FROM domains WHERE id=$1 FOR UPDATE`, d.ID); err != nil {
			return err
		}
		if dd.Status != models.StatusRegistered {
			return apierror.ErrInvalidState
		}
		// Re-verify the auth code against the locked row in case it was
		// rotated between the outer read and this transaction.
		if ok, err := s.domains.CheckAuth(&dd, req.AuthCode); err != nil {
			return err
		} else if !ok {
			return apierror.ErrBadAuthCode
		}
		// Snapshot the fee inside the transaction that freezes it, so a price
		// change can never land between decision and charge.
		price, err := s.prices.CurrentTx(ctx, tx, dd.TLD)
		if err != nil {
			return err
		}
		if req.IdempotencyKey != nil {
			if err := ledger.LockIdempotency(ctx, tx, req.ToResellerID,
				models.TxTransferFreeze, *req.IdempotencyKey); err != nil {
				return err
			}
			if prior, err := ledger.FindTx(ctx, tx, req.ToResellerID,
				models.TxTransferFreeze, req.IdempotencyKey); err != nil {
				return err
			} else if prior != nil && prior.TransferID != nil {
				return s.loadExisting(ctx, tx, *prior.TransferID, prior, &t, &bt)
			}
		}

		// Freeze the fee: available -> held.
		post, err := s.ledger.PostTx(ctx, tx, ledger.Post{
			ResellerID:     req.ToResellerID,
			Kind:           models.TxTransferFreeze,
			AvailableDelta: -price.TransferCents,
			HeldDelta:      price.TransferCents,
			DomainID:       &dd.ID,
			IdempotencyKey: req.IdempotencyKey,
		})
		if err != nil {
			return err
		}
		if err := tx.QueryRowxContext(ctx, `
			INSERT INTO transfers
			  (domain_id, domain_name, from_reseller_id, from_customer_id,
			   to_reseller_id, to_customer_id, state, transfer_cents,
			   requested_at, approval_deadline, expires_at, created_tx_id)
			VALUES ($1,$2,$3,$4,$5,$6,'pending_approval',$7,$8,$9,$10,$11)
			RETURNING *`,
			dd.ID, dd.CanonicalName, dd.ResellerID, dd.CustomerID,
			req.ToResellerID, req.ToCustomerID, price.TransferCents,
			now, now.Add(s.cfg.ApprovalWindow), now.Add(s.cfg.ApprovalWindow),
			post.ID).StructScan(&t); err != nil {
			if c, ok := store.UniqueConstraint(err); ok && c == "idx_transfers_one_live" {
				return apierror.ErrTransferLive
			}
			return err
		}
		if _, err := tx.ExecContext(ctx,
			`UPDATE billing_transactions SET transfer_id=$1 WHERE id=$2`, t.ID, post.ID); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx,
			`UPDATE domains SET status='transferring', updated_at=$1 WHERE id=$2`,
			now, dd.ID); err != nil {
			return err
		}
		if err := events.Insert(ctx, tx, models.DomainEvent{
			DomainID: dd.ID, DomainName: dd.CanonicalName,
			EventType:   models.EventTransferRequested,
			FromStatus:  strptr(models.StatusRegistered),
			ToStatus:    strptr(models.StatusTransferring),
			AmountCents: &price.TransferCents,
			Detail: events.MustDetail(map[string]any{
				"transfer_id":      t.ID,
				"from_reseller_id": dd.ResellerID,
				"to_reseller_id":   req.ToResellerID,
			}),
		}); err != nil {
			return err
		}
		bt = post
		return nil
	})
	if err != nil {
		return nil, nil, err
	}
	return &t, bt, nil
}

func (s *Service) loadExisting(ctx context.Context, tx *sqlx.Tx, transferID int64,
	post *models.BillingTransaction, t *models.Transfer, bt **models.BillingTransaction) error {
	if err := tx.GetContext(ctx, t, `SELECT * FROM transfers WHERE id=$1`, transferID); err != nil {
		return err
	}
	*bt = post
	return nil
}

type RequestInput struct {
	DomainName     string
	AuthCode       string
	ToResellerID   int64
	ToCustomerID   int64
	IdempotencyKey *string
}

// Approve records losing-reseller approval and starts the simulated wait.
func (s *Service) Approve(ctx context.Context, clock clk.Clock, transferID, fromResellerID int64) (*models.Transfer, error) {
	now := clock.Now()
	var out models.Transfer
	err := store.InTx(ctx, s.db, func(tx *sqlx.Tx) error {
		t, err := lockTransfer(ctx, tx, transferID)
		if err != nil {
			return err
		}
		if fromResellerID != t.FromResellerID {
			return apierror.ErrForbidden
		}
		if t.State != models.TransferPending {
			return apierror.ErrInvalidState
		}
		if now.After(t.ApprovalDeadline) {
			return apierror.ErrInvalidState // worker will fail it; approve too late
		}
		approveUntil := now.Add(s.cfg.TransferWait)
		if err := tx.QueryRowxContext(ctx, `
			UPDATE transfers SET state='seller_approved', approved_at=$1,
				approve_until=$2, updated_at=$3
			WHERE id=$4 RETURNING *`,
			now, approveUntil, now, transferID).StructScan(&out); err != nil {
			return err
		}
		return events.Insert(ctx, tx, models.DomainEvent{
			DomainID: t.DomainID, DomainName: t.DomainName,
			EventType:  models.EventTransferApproved,
			FromStatus: strptr(models.StatusTransferring),
			ToStatus:   strptr(models.StatusTransferring),
			Detail: events.MustDetail(map[string]any{
				"transfer_id": t.ID, "approve_until": approveUntil,
			}),
		})
	})
	if err != nil {
		return nil, err
	}
	return &out, nil
}

// Reject rejects a pending transfer and releases frozen credits.
func (s *Service) Reject(ctx context.Context, clock clk.Clock, transferID, fromResellerID int64) (*models.Transfer, error) {
	return s.finish(ctx, clock, transferID, actorCheck{
		resellerID: fromResellerID, wantSide: sideFrom,
		allowStates: []string{models.TransferPending},
		newState:    models.TransferRejected,
		eventType:   models.EventTransferRejected,
		release:     true,
	})
}

// Cancel lets the gaining reseller abandon a pending request; freeze released.
func (s *Service) Cancel(ctx context.Context, clock clk.Clock, transferID, toResellerID int64) (*models.Transfer, error) {
	return s.finish(ctx, clock, transferID, actorCheck{
		resellerID: toResellerID, wantSide: sideTo,
		allowStates: []string{models.TransferPending},
		newState:    models.TransferCanceled,
		eventType:   models.EventTransferCanceled,
		release:     true,
	})
}

const (
	sideFrom = 0
	sideTo   = 1
)

type actorCheck struct {
	resellerID  int64
	wantSide    int
	allowStates []string
	newState    string
	eventType   string
	release     bool // release frozen credits
}

func (s *Service) finish(ctx context.Context, clock clk.Clock, transferID int64, a actorCheck) (*models.Transfer, error) {
	now := clock.Now()
	var out models.Transfer
	err := store.InTx(ctx, s.db, func(tx *sqlx.Tx) error {
		t, err := lockTransfer(ctx, tx, transferID)
		if err != nil {
			return err
		}
		owner := t.FromResellerID
		if a.wantSide == sideTo {
			owner = t.ToResellerID
		}
		if a.resellerID != owner {
			return apierror.ErrForbidden
		}
		if !containsState(a.allowStates, t.State) {
			return apierror.ErrInvalidState
		}
		if a.release {
			if _, err := s.ledger.PostTx(ctx, tx, ledger.Post{
				ResellerID:     t.ToResellerID,
				Kind:           models.TxTransferRelease,
				AvailableDelta: t.TransferCents,
				HeldDelta:      -t.TransferCents,
				DomainID:       &t.DomainID,
				TransferID:     &t.ID,
			}); err != nil {
				return err
			}
		}
		if err := tx.QueryRowxContext(ctx, `
			UPDATE transfers SET state=$1, updated_at=$2 WHERE id=$3 RETURNING *`,
			a.newState, now, transferID).StructScan(&out); err != nil {
			return err
		}
		// Domain leaves transferring and follows its original lifecycle clock.
		if _, err := tx.ExecContext(ctx,
			`UPDATE domains SET status='registered', updated_at=$1 WHERE id=$2`,
			now, t.DomainID); err != nil {
			return err
		}
		return events.Insert(ctx, tx, models.DomainEvent{
			DomainID: t.DomainID, DomainName: t.DomainName,
			EventType:   a.eventType,
			FromStatus:  strptr(models.StatusTransferring),
			ToStatus:    strptr(models.StatusRegistered),
			AmountCents: amountIf(t.TransferCents),
		})
	})
	if err != nil {
		return nil, err
	}
	return &out, nil
}

// ProcessDue advances all timed transfer transitions due as of now. It is the
// worker entry point and is safe to run repeatedly or after a restart: every
// transition is its own transaction.
func (s *Service) ProcessDue(ctx context.Context, clock clk.Clock) (int, error) {
	now := clock.Now()
	const batch = 100
	done := 0
	for {
		var ids []int64
		err := s.db.SelectContext(ctx, &ids, `
			SELECT id FROM transfers
			WHERE (state='pending_approval' AND $1 >= approval_deadline)
			   OR (state='seller_approved' AND approve_until IS NOT NULL AND $1 >= approve_until)
			ORDER BY id
			LIMIT $2`, now, batch)
		if err != nil {
			return done, err
		}
		for _, id := range ids {
			var t models.Transfer
			if err := s.db.GetContext(ctx, &t, `SELECT * FROM transfers WHERE id=$1`, id); err != nil {
				return done, err
			}
			switch t.State {
			case models.TransferPending:
				if _, err := s.timeoutFail(ctx, clock, &t, now); err != nil {
					return done, err
				}
			case models.TransferApproved:
				if err := s.complete(ctx, clock, &t); err != nil {
					return done, err
				}
			}
			done++
		}
		if len(ids) < batch {
			break
		}
	}
	return done, nil
}

// timeoutFail marks an unapproved transfer failed and releases the freeze.
func (s *Service) timeoutFail(ctx context.Context, clock clk.Clock, t *models.Transfer, now time.Time) (*models.Transfer, error) {
	err := store.InTx(ctx, s.db, func(tx *sqlx.Tx) error {
		tt, err := lockTransfer(ctx, tx, t.ID)
		if err != nil {
			return err
		}
		if tt.State != models.TransferPending {
			return nil // approve/cancel raced us
		}
		if _, err := s.ledger.PostTx(ctx, tx, ledger.Post{
			ResellerID:     tt.ToResellerID,
			Kind:           models.TxTransferRelease,
			AvailableDelta: tt.TransferCents,
			HeldDelta:      -tt.TransferCents,
			DomainID:       &tt.DomainID,
			TransferID:     &tt.ID,
		}); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx,
			`UPDATE transfers SET state='failed', updated_at=$1 WHERE id=$2`,
			now, tt.ID); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx,
			`UPDATE domains SET status='registered', updated_at=$1 WHERE id=$2`,
			now, tt.DomainID); err != nil {
			return err
		}
		return events.Insert(ctx, tx, models.DomainEvent{
			DomainID: tt.DomainID, DomainName: tt.DomainName,
			EventType:   models.EventTransferFailed,
			FromStatus:  strptr(models.StatusTransferring),
			ToStatus:    strptr(models.StatusRegistered),
			AmountCents: amountIf(tt.TransferCents),
			Detail:      events.MustDetail(map[string]any{"reason": "approval_timeout"}),
		})
	})
	if err != nil {
		return nil, err
	}
	t.State = models.TransferFailed
	return t, nil
}

// complete finishes an approved transfer after the 5-day simulated wait:
// capture held fee, move ownership, extend by one year, recompute deadlines.
func (s *Service) complete(ctx context.Context, clock clk.Clock, t *models.Transfer) error {
	now := clock.Now()
	return store.InTx(ctx, s.db, func(tx *sqlx.Tx) error {
		tt, err := lockTransfer(ctx, tx, t.ID)
		if err != nil {
			return err
		}
		if tt.State != models.TransferApproved {
			return nil // double execution: already completed
		}
		var d models.Domain
		if err := tx.GetContext(ctx, &d,
			`SELECT * FROM domains WHERE id=$1 FOR UPDATE`, tt.DomainID); err != nil {
			return err
		}
		// Capture the frozen fee (held -> consumed). No available movement and
		// no price lookup: the amount was fixed at request time.
		capture, err := s.ledger.PostTx(ctx, tx, ledger.Post{
			ResellerID: tt.ToResellerID,
			Kind:       models.TxTransferCapture,
			HeldDelta:  -tt.TransferCents,
			DomainID:   &tt.DomainID,
			TransferID: &tt.ID,
		})
		if err != nil {
			return err
		}
		// Ownership moves to the gaining reseller/customer.
		newExpiry := d.ExpiresAt.AddDate(1, 0, 0)
		exp, red, pend, purge := s.deadlines(newExpiry)
		if err := tx.QueryRowxContext(ctx, `
			UPDATE domains SET
				status='registered', customer_id=$1, reseller_id=$2,
				expires_at=$3, expired_at=$4, redeemable_at=$5,
				pending_delete_at=$6, purge_at=$7, updated_at=$8
			WHERE id=$9 RETURNING *`,
			tt.ToCustomerID, tt.ToResellerID, newExpiry, exp, red, pend, purge,
			now, d.ID).StructScan(&d); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `
			UPDATE transfers SET state='completed', completed_at=$1,
				captured_tx_id=$2, updated_at=$3 WHERE id=$4`,
			now, capture.ID, now, tt.ID); err != nil {
			return err
		}
		fee := tt.TransferCents
		return events.Insert(ctx, tx, models.DomainEvent{
			DomainID: d.ID, DomainName: d.CanonicalName,
			EventType:   models.EventTransferCompleted,
			FromStatus:  strptr(models.StatusTransferring),
			ToStatus:    strptr(models.StatusRegistered),
			AmountCents: &fee,
			Detail: events.MustDetail(map[string]any{
				"transfer_id":      tt.ID,
				"from_reseller_id": tt.FromResellerID,
				"to_reseller_id":   tt.ToResellerID,
				"to_customer_id":   tt.ToCustomerID,
				"expires_at":       newExpiry,
			}),
		})
	})
}

func (s *Service) deadlines(expiresAt time.Time) (time.Time, time.Time, time.Time, time.Time) {
	return domains.ComputeDeadlines(expiresAt, s.cfg.Timeline)
}

// Get returns a transfer by id.
func (s *Service) Get(ctx context.Context, id int64) (*models.Transfer, error) {
	return getTransfer(ctx, s.db, id)
}

// ListForReseller returns transfers visible to a reseller (as either side).
func (s *Service) ListForReseller(ctx context.Context, resellerID int64, limit int) ([]models.Transfer, error) {
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	var out []models.Transfer
	err := s.db.SelectContext(ctx, &out, `
		SELECT * FROM transfers
		WHERE from_reseller_id=$1 OR to_reseller_id=$1
		ORDER BY id DESC LIMIT $2`, resellerID, limit)
	return out, err
}

// ListForCustomer returns transfers that reference the customer on either side.
func (s *Service) ListForCustomer(ctx context.Context, customerID int64, limit int) ([]models.Transfer, error) {
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	var out []models.Transfer
	err := s.db.SelectContext(ctx, &out, `
		SELECT * FROM transfers
		WHERE from_customer_id=$1 OR to_customer_id=$1
		ORDER BY id DESC LIMIT $2`, customerID, limit)
	return out, err
}

func lockTransfer(ctx context.Context, tx *sqlx.Tx, id int64) (*models.Transfer, error) {
	var t models.Transfer
	err := tx.GetContext(ctx, &t, `SELECT * FROM transfers WHERE id=$1 FOR UPDATE`, id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, apierror.ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &t, nil
}

func getTransfer(ctx context.Context, q sqlx.QueryerContext, id int64) (*models.Transfer, error) {
	var t models.Transfer
	err := sqlx.GetContext(ctx, q, &t, `SELECT * FROM transfers WHERE id=$1`, id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, apierror.ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &t, nil
}

func containsState(haystack []string, needle string) bool {
	for _, h := range haystack {
		if h == needle {
			return true
		}
	}
	return false
}

func amountIf(v int64) *int64 {
	vv := v
	return &vv
}

func strptr(x string) *string { return &x }
