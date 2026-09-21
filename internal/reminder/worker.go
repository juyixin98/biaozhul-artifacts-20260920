// Package reminder fires persistent, exactly-once-per-due-version reminders
// for overdue action items. It is purely database-driven, so a service
// restart simply picks up whatever is due and not yet reminded.
package reminder

import (
	"context"
	"errors"
	"log"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"sircc/internal/db"
)

type Worker struct {
	Pool *pgxpool.Pool
	// Now is overridable in tests; defaults to time.Now.
	Now func() time.Time
}

func (w *Worker) now() time.Time {
	if w.Now != nil {
		return w.Now()
	}
	return time.Now()
}

// RunOnce processes every currently-due, not-yet-reminded action item and
// returns how many reminders were persisted.
func (w *Worker) RunOnce(ctx context.Context) (int, error) {
	q := db.New(w.Pool)
	items, err := q.ListDueActionItems(ctx, 500)
	if err != nil {
		return 0, err
	}
	sent := 0
	for _, it := range items {
		ok, err := w.processOne(ctx, it.ID)
		if err != nil {
			return sent, err
		}
		if ok {
			sent++
		}
	}
	return sent, nil
}

// processOne locks the action item row and re-validates under the lock, so a
// concurrent reschedule (which bumps due_version) cannot race an outdated
// reminder into the database.
func (w *Worker) processOne(ctx context.Context, id uuid.UUID) (bool, error) {
	tx, err := w.Pool.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	q := db.New(w.Pool).WithTx(tx)

	item, err := q.GetActionItemForUpdate(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if item.Status != "open" || item.DueAt.After(w.now()) {
		return false, nil
	}
	exists, err := q.ReminderExists(ctx, db.ReminderExistsParams{
		ActionItemID: item.ID, DueVersion: item.DueVersion,
	})
	if err != nil {
		return false, err
	}
	if exists {
		return false, nil
	}
	if err := q.InsertReminder(ctx, db.InsertReminderParams{
		ActionItemID: item.ID, DueVersion: item.DueVersion, DueAt: item.DueAt,
	}); err != nil {
		return false, err
	}
	if _, err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
		IncidentID: item.IncidentID,
		Actor:      "system",
		Action:     "reminder_sent",
		Detail:     []byte(`{}`),
	}); err != nil {
		return false, err
	}
	if err := tx.Commit(ctx); err != nil {
		return false, err
	}
	return true, nil
}

// Start runs RunOnce immediately (catching up after a restart) and then on
// every interval until ctx is cancelled.
func (w *Worker) Start(ctx context.Context, interval time.Duration) {
	if n, err := w.RunOnce(ctx); err != nil {
		log.Printf("reminder: initial pass failed: %v", err)
	} else if n > 0 {
		log.Printf("reminder: sent %d overdue reminder(s) on startup", n)
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if _, err := w.RunOnce(ctx); err != nil {
				log.Printf("reminder: pass failed: %v", err)
			}
		}
	}
}
