package service

import (
	"context"

	"github.com/google/uuid"
	"github.com/jmoiron/sqlx"

	"domainengine/internal/domainname"
)

func normalizeName(raw string) (string, error) {
	name, err := domainname.Normalize(raw)
	if err != nil {
		return "", ErrValidation("invalid domain name: %v", err)
	}
	return name, nil
}

// InitiateTransfer starts a transfer-in: the requester presents the 16-char
// auth code, the transfer price is frozen from their reseller's credits, and
// the domain moves to `transferring`. Billing points: freeze now, capture on
// completion, release on cancel/reject/timeout.
func (s *Service) InitiateTransfer(ctx context.Context, user *User, rawName, authCode, idemKey string) (int, any, error) {
	resellerID, err := resellerOf(user)
	if err != nil {
		return 0, nil, err
	}
	name, err := normalizeName(rawName)
	if err != nil {
		return 0, nil, err
	}
	now := s.clock.Now()
	price, err := s.priceAt(ctx, domainname.TLD(name), ActionTransfer, now)
	if err != nil {
		return 0, nil, err
	}

	return s.idempotent(ctx, idemKey, user.ID, "transfer_init", func(ctx context.Context, tx *sqlx.Tx) (int, any, error) {
		var dom struct {
			ID          string `db:"id"`
			OwnerID     string `db:"owner_id"`
			Status      string `db:"status"`
			AuthCodeEnc []byte `db:"auth_code_enc"`
		}
		err := tx.GetContext(ctx, &dom,
			`SELECT id, owner_id, status, auth_code_enc FROM domains WHERE name = $1 FOR UPDATE`, name)
		if err != nil {
			return 0, nil, ErrNotFound
		}
		if dom.OwnerID == user.ID {
			return 0, nil, ErrValidation("you already own this domain")
		}
		if dom.Status != StatusRegistered {
			return 0, nil, ErrInvalidState("domain in status %q cannot be transferred", dom.Status)
		}
		if !s.verifyAuthCode(dom.AuthCodeEnc, authCode) {
			return 0, nil, ErrInvalidAuthCode
		}
		if err := ensureFunds(ctx, tx, resellerID, price); err != nil {
			return 0, nil, err
		}

		tr := Transfer{
			ID:               uuid.NewString(),
			DomainID:         dom.ID,
			DomainName:       name,
			FromOwnerID:      dom.OwnerID,
			ToOwnerID:        user.ID,
			State:            TransferPending,
			PriceCents:       price, // fixed at acceptance; later price changes do not apply
			HoldID:           uuid.NewString(),
			ApprovalDeadline: now.AddDate(0, 0, s.cfg.TransferApprovalTimeoutDays),
		}
		if err := freeze(ctx, tx, tr.HoldID, resellerID, price, idemKey+":hold:"+tr.ID, &tr.ID); err != nil {
			return 0, nil, err
		}
		_, err = tx.ExecContext(ctx, `
			INSERT INTO transfers (id, domain_id, from_owner_id, to_owner_id, state, price_cents, hold_id,
			                       idempotency_key, approval_deadline)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
			tr.ID, tr.DomainID, tr.FromOwnerID, tr.ToOwnerID, tr.State, tr.PriceCents, tr.HoldID,
			user.ID+":"+idemKey, tr.ApprovalDeadline)
		if err != nil {
			return 0, nil, err
		}
		if _, err := tx.ExecContext(ctx,
			`UPDATE domains SET status = $1, version = version + 1, updated_at = $2 WHERE id = $3`,
			StatusTransferring, now, dom.ID); err != nil {
			return 0, nil, err
		}
		if err := s.addEvent(ctx, tx, &dom.ID, name, "transfer_initiated", map[string]any{
			"transfer_id": tr.ID, "from_owner_id": tr.FromOwnerID, "to_owner_id": tr.ToOwnerID,
			"price_cents": price,
		}); err != nil {
			return 0, nil, err
		}
		return 201, tr, nil
	})
}

// ApproveTransfer (current owner or admin) moves a pending transfer to
// `approved`; the maintenance job completes it after the simulated 5-day
// registry wait.
func (s *Service) ApproveTransfer(ctx context.Context, user *User, transferID string) (int, any, error) {
	now := s.clock.Now()
	tx, err := s.db.BeginTxx(ctx, nil)
	if err != nil {
		return 0, nil, err
	}
	defer tx.Rollback()

	tr, err := lockTransfer(ctx, tx, transferID)
	if err != nil {
		return 0, nil, err
	}
	if user.ID != tr.FromOwnerID && user.Role != RoleAdmin {
		return 0, nil, ErrForbidden
	}
	if tr.State != TransferPending {
		return 0, nil, ErrInvalidState("transfer in state %q cannot be approved", tr.State)
	}
	completesAt := now.AddDate(0, 0, s.cfg.TransferWaitDays)
	if _, err := tx.ExecContext(ctx,
		`UPDATE transfers SET state = $1, completes_at = $2, updated_at = $3 WHERE id = $4`,
		TransferApproved, completesAt, now, tr.ID); err != nil {
		return 0, nil, err
	}
	if err := s.addEvent(ctx, tx, &tr.DomainID, tr.DomainName, "transfer_approved", map[string]any{
		"transfer_id": tr.ID, "completes_at": completesAt,
	}); err != nil {
		return 0, nil, err
	}
	if err := tx.Commit(); err != nil {
		return 0, nil, err
	}
	tr.State = TransferApproved
	tr.CompletesAt = &completesAt
	return 200, tr, nil
}

// CancelTransfer aborts a pending transfer: by the requester ("cancel"), the
// current owner ("reject") or an admin. The frozen credits are released and
// the domain returns to `registered`.
func (s *Service) CancelTransfer(ctx context.Context, user *User, transferID, reason string) (int, any, error) {
	tx, err := s.db.BeginTxx(ctx, nil)
	if err != nil {
		return 0, nil, err
	}
	defer tx.Rollback()

	tr, err := lockTransfer(ctx, tx, transferID)
	if err != nil {
		return 0, nil, err
	}
	allowed := user.Role == RoleAdmin ||
		(reason == "cancel" && user.ID == tr.ToOwnerID) ||
		(reason == "reject" && user.ID == tr.FromOwnerID)
	if !allowed {
		return 0, nil, ErrForbidden
	}
	if tr.State != TransferPending {
		return 0, nil, ErrInvalidState("transfer in state %q cannot be cancelled", tr.State)
	}
	if err := s.cancelTransferTx(ctx, tx, tr, reason+"ed by "+user.ID); err != nil {
		return 0, nil, err
	}
	if err := tx.Commit(); err != nil {
		return 0, nil, err
	}
	tr.State = TransferCancelled
	return 200, tr, nil
}

// cancelTransferTx performs the shared cancel path: state -> cancelled,
// hold released, domain back to registered. All steps are conditional so
// the job can safely retry them.
func (s *Service) cancelTransferTx(ctx context.Context, tx *sqlx.Tx, tr *Transfer, reason string) error {
	res, err := tx.ExecContext(ctx,
		`UPDATE transfers SET state = $1, cancel_reason = $2, updated_at = now()
		 WHERE id = $3 AND state IN ($4, $5)`,
		TransferCancelled, reason, tr.ID, TransferPending, TransferApproved)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return nil // already finished concurrently
	}
	if err := releaseHold(ctx, tx, tr.HoldID); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx,
		`UPDATE domains SET status = $1, version = version + 1, updated_at = now()
		 WHERE id = $2 AND status = $3`, StatusRegistered, tr.DomainID, StatusTransferring); err != nil {
		return err
	}
	return s.addEvent(ctx, tx, &tr.DomainID, tr.DomainName, "transfer_cancelled", map[string]any{
		"transfer_id": tr.ID, "reason": reason,
	})
}

func lockTransfer(ctx context.Context, tx *sqlx.Tx, id string) (*Transfer, error) {
	var tr Transfer
	err := tx.GetContext(ctx, &tr, `
		SELECT t.id, t.domain_id, d.name AS domain_name, t.from_owner_id, t.to_owner_id, t.state,
		       t.price_cents, t.hold_id, t.approval_deadline, t.completes_at, t.cancel_reason, t.created_at
		FROM transfers t JOIN domains d ON d.id = t.domain_id
		WHERE t.id = $1 FOR UPDATE OF t`, id)
	if err != nil {
		return nil, ErrNotFound
	}
	return &tr, nil
}
