package service

import (
	"context"
	"time"
)

// MaintenanceReport summarizes one maintenance pass.
type MaintenanceReport struct {
	Expired           int `json:"expired"`
	ToRedemption      int `json:"to_redemption"`
	ToPendingDelete   int `json:"to_pending_delete"`
	Deleted           int `json:"deleted"`
	TransfersComplete int `json:"transfers_completed"`
	TransfersTimedOut int `json:"transfers_timed_out"`
}

// RunMaintenance performs one full lifecycle pass. Every step is a
// conditional UPDATE or an idempotent per-transfer transaction, so the pass
// is safe to re-run after a crash and safe to run concurrently with user
// traffic:
//
//   - expiry vs renewal: the UPDATE takes the same row lock renewal takes;
//     whichever commits first determines the outcome, and renewal is valid
//     from both `registered` and `expired`, so the race has a deterministic,
//     single-charge result;
//   - transfer completion uses deterministic ledger keys, so a job restarted
//     mid-completion never double-captures a hold.
func (s *Service) RunMaintenance(ctx context.Context) (*MaintenanceReport, error) {
	now := s.clock.Now()
	rep := &MaintenanceReport{}

	exec := func(query string, arg time.Time) (int, error) {
		res, err := s.db.ExecContext(ctx, query, arg, now)
		if err != nil {
			return 0, err
		}
		n, _ := res.RowsAffected()
		return int(n), nil
	}

	var err error
	// registered -> expired (domains in `transferring` do not expire; the
	// transfer resolves first, one way or the other)
	if rep.Expired, err = exec(
		`UPDATE domains SET status = 'expired', updated_at = $2
		 WHERE status = 'registered' AND expires_at <= $1`, now); err != nil {
		return nil, err
	}
	// expired -> redemption after the grace window
	if rep.ToRedemption, err = exec(
		`UPDATE domains SET status = 'redemption', updated_at = $2
		 WHERE status = 'expired' AND expires_at <= $1`,
		now.AddDate(0, 0, -s.cfg.ExpiredGraceDays)); err != nil {
		return nil, err
	}
	// redemption -> pending_delete after the 30-day redemption window
	if rep.ToPendingDelete, err = exec(
		`UPDATE domains SET status = 'pending_delete', updated_at = $2
		 WHERE status = 'redemption' AND expires_at <= $1`,
		now.AddDate(0, 0, -(s.cfg.ExpiredGraceDays+s.cfg.RedemptionDays))); err != nil {
		return nil, err
	}
	// pending_delete -> deleted (name becomes available again)
	var doomed []Domain
	if err := s.db.SelectContext(ctx, &doomed,
		`SELECT id, name FROM domains WHERE status = 'pending_delete' AND expires_at <= $1`,
		now.AddDate(0, 0, -(s.cfg.ExpiredGraceDays+s.cfg.RedemptionDays+s.cfg.PendingDeleteDays))); err != nil {
		return nil, err
	}
	for _, d := range doomed {
		res, err := s.db.ExecContext(ctx,
			`DELETE FROM domains WHERE id = $1 AND status = 'pending_delete'`, d.ID)
		if err != nil {
			return nil, err
		}
		if n, _ := res.RowsAffected(); n == 1 {
			rep.Deleted++
			if _, err := s.db.ExecContext(ctx,
				`INSERT INTO domain_events (domain_id, domain_name, event, detail) VALUES ($1, $2, 'deleted', '{}')`,
				d.ID, d.Name); err != nil {
				return nil, err
			}
		}
	}

	// transfers: complete approved ones whose 5-day wait has elapsed
	var due []string
	if err := s.db.SelectContext(ctx, &due,
		`SELECT id FROM transfers WHERE state = 'approved' AND completes_at <= $1`, now); err != nil {
		return nil, err
	}
	for _, id := range due {
		ok, err := s.completeTransfer(ctx, id)
		if err != nil {
			return rep, err
		}
		if ok {
			rep.TransfersComplete++
		}
	}

	// transfers: time out ones never approved
	var stale []string
	if err := s.db.SelectContext(ctx, &stale,
		`SELECT id FROM transfers WHERE state = 'pending_approval' AND approval_deadline <= $1`, now); err != nil {
		return nil, err
	}
	for _, id := range stale {
		ok, err := s.timeoutTransfer(ctx, id)
		if err != nil {
			return rep, err
		}
		if ok {
			rep.TransfersTimedOut++
		}
	}
	return rep, nil
}

// completeTransfer finalizes one approved transfer: capture the frozen
// credits, move ownership, extend expiry by one year. Returns false if
// another worker already processed it.
func (s *Service) completeTransfer(ctx context.Context, id string) (bool, error) {
	tx, err := s.db.BeginTxx(ctx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()

	var tr Transfer
	err = tx.GetContext(ctx, &tr, `
		SELECT t.id, t.domain_id, d.name AS domain_name, t.from_owner_id, t.to_owner_id, t.state,
		       t.price_cents, t.hold_id
		FROM transfers t JOIN domains d ON d.id = t.domain_id
		WHERE t.id = $1 FOR UPDATE OF t`, id)
	if err != nil {
		return false, err
	}
	if tr.State != TransferApproved {
		return false, nil
	}
	if _, err := tx.ExecContext(ctx,
		`UPDATE transfers SET state = $1, updated_at = now() WHERE id = $2`, TransferCompleted, tr.ID); err != nil {
		return false, err
	}
	// capture the hold: conditional update + deterministic ledger key make
	// this exactly-once even if the process died between the two steps.
	toOwner, err := getUser(ctx, tx, tr.ToOwnerID)
	if err != nil {
		return false, err
	}
	resellerID, err := resellerOf(toOwner)
	if err != nil {
		return false, err
	}
	if err := captureHold(ctx, tx, tr.HoldID, resellerID, tr.PriceCents,
		"transfer:"+tr.ID+":capture", "transfer "+tr.DomainName, tr.DomainName, &tr.ID); err != nil {
		return false, err
	}
	now := s.clock.Now()
	if _, err := tx.ExecContext(ctx, `
		UPDATE domains
		SET owner_id = $1, status = $2, expires_at = GREATEST(expires_at, $3) + interval '1 year',
		    version = version + 1, updated_at = $3
		WHERE id = $4`, tr.ToOwnerID, StatusRegistered, now, tr.DomainID); err != nil {
		return false, err
	}
	if err := s.addEvent(ctx, tx, &tr.DomainID, tr.DomainName, "transfer_completed", map[string]any{
		"transfer_id": tr.ID, "from_owner_id": tr.FromOwnerID, "to_owner_id": tr.ToOwnerID,
		"price_cents": tr.PriceCents,
	}); err != nil {
		return false, err
	}
	return true, tx.Commit()
}

// timeoutTransfer cancels a transfer that was never approved in time and
// releases the frozen credits.
func (s *Service) timeoutTransfer(ctx context.Context, id string) (bool, error) {
	tx, err := s.db.BeginTxx(ctx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()

	tr, err := lockTransfer(ctx, tx, id)
	if err != nil {
		return false, err
	}
	if tr.State != TransferPending {
		return false, nil
	}
	if err := s.cancelTransferTx(ctx, tx, tr, "approval timeout"); err != nil {
		return false, err
	}
	return true, tx.Commit()
}
