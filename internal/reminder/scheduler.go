// Package reminder delivers due-date reminders for action items. Delivery is
// persistent (a reminders row), idempotent (unique action_item_id+due_version)
// and safe across restarts: every scan re-reads overdue items from the
// database, so reminders missed while the service was down are sent on the
// next run.
package reminder

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	"sircc/internal/store"
)

type Scheduler struct {
	pool     *pgxpool.Pool
	q        *store.Queries
	interval time.Duration
	now      func() time.Time
}

func NewScheduler(pool *pgxpool.Pool, interval time.Duration) *Scheduler {
	return &Scheduler{
		pool:     pool,
		q:        store.New(pool),
		interval: interval,
		now:      time.Now,
	}
}

// Run starts the periodic scan. It fires once immediately so reminders that
// came due while the service was stopped are caught up, then ticks until the
// context is cancelled.
func (s *Scheduler) Run(ctx context.Context) {
	if n, err := s.RunOnce(ctx); err != nil {
		log.Printf("reminder scan: %v", err)
	} else if n > 0 {
		log.Printf("reminder scan: sent %d reminder(s)", n)
	}
	ticker := time.NewTicker(s.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if n, err := s.RunOnce(ctx); err != nil {
				log.Printf("reminder scan: %v", err)
			} else if n > 0 {
				log.Printf("reminder scan: sent %d reminder(s)", n)
			}
		}
	}
}

// RunOnce delivers reminders for every action item that is open, past due and
// not yet reminded for its current due_version. It returns the number of
// reminders sent in this pass.
func (s *Scheduler) RunOnce(ctx context.Context) (int, error) {
	due, err := s.q.ListDueActionItems(ctx, pgtype.Timestamptz{Time: s.now(), Valid: true})
	if err != nil {
		return 0, err
	}
	sent := 0
	for _, item := range due {
		ok, err := s.deliver(ctx, item)
		if err != nil {
			return sent, err
		}
		if ok {
			sent++
		}
	}
	return sent, nil
}

// deliver sends one reminder. The action-item row is re-read under a FOR
// UPDATE lock and re-validated, so a reschedule that committed after the scan
// (new due_version or a future due_at) suppresses the stale reminder. The
// persistent reminders row carries a unique (action_item_id, due_version) key,
// so even concurrent schedulers cannot deliver the same version twice.
func (s *Scheduler) deliver(ctx context.Context, scanned store.ActionItem) (bool, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	q := s.q.WithTx(tx)

	item, err := q.GetActionItemForUpdate(ctx, scanned.ID)
	if err != nil {
		return false, err
	}
	if item.Status != "open" ||
		item.DueVersion != scanned.DueVersion ||
		item.RemindedDueVersion >= item.DueVersion ||
		item.DueAt.Time.After(s.now()) {
		// Rescheduled, completed or already reminded since the scan.
		return false, nil
	}

	rows, err := q.InsertReminder(ctx, store.InsertReminderParams{
		ID:           pgtype.UUID{Bytes: uuid.New(), Valid: true},
		ActionItemID: item.ID,
		DueVersion:   item.DueVersion,
	})
	if err != nil {
		return false, err
	}
	if rows == 0 {
		// Another scheduler instance already persisted this version.
		return false, nil
	}
	if err := q.MarkActionItemReminded(ctx, store.MarkActionItemRemindedParams{
		ID:         item.ID,
		DueVersion: item.DueVersion,
	}); err != nil {
		return false, err
	}
	if _, err := q.InsertAuditEvent(ctx, store.InsertAuditEventParams{
		ID:         pgtype.UUID{Bytes: uuid.New(), Valid: true},
		IncidentID: item.IncidentID,
		EventType:  "reminder.sent",
		Payload: []byte(fmt.Sprintf(`{"actionItemId":%q,"dueVersion":%d}`,
			uuid.UUID(item.ID.Bytes).String(), item.DueVersion)),
	}); err != nil {
		return false, err
	}
	if err := tx.Commit(ctx); err != nil {
		return false, err
	}
	return true, nil
}
