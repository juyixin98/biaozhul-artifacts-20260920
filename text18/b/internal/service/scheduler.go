package service

import (
	"context"
	"log"
	"time"

	"github.com/google/uuid"

	"sircc/internal/db"
)

// Scheduler turns due action items into persistent notifications.
//
// Correctness properties:
//
//   - Exactly-once per due version: reminder_dispatches (action_item_id,
//     version) is the durable dedupe key; insertion is ON CONFLICT DO NOTHING
//     and only the winning insert creates a notification.
//   - Reschedule safety: due items are selected by their CURRENT version not
//     having a dispatch. Bumping the due date bumps the version, so the old
//     schedule disappears from the candidate set and can never produce a
//     stale reminder.
//   - Restart catch-up: there is no in-memory timer state. On startup the
//     first tick selects every item due at or before now with no dispatch
//     (including items that became due while the service was down), so
//     missed reminders are delivered rather than dropped.
type Scheduler struct {
	svc       *Service
	interval  time.Duration
	batchSize int32
}

func NewScheduler(svc *Service, interval time.Duration, batchSize int32) *Scheduler {
	if batchSize <= 0 {
		batchSize = 100
	}
	return &Scheduler{svc: svc, interval: interval, batchSize: batchSize}
}

// Run blocks until ctx is cancelled, ticking immediately for startup
// catch-up and then on the configured interval.
func (sc *Scheduler) Run(ctx context.Context) {
	log.Printf("reminder scheduler starting (interval=%s)", sc.interval)
	sc.tick(ctx) // startup catch-up
	t := time.NewTicker(sc.interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			log.Printf("reminder scheduler stopping")
			return
		case <-t.C:
			sc.tick(ctx)
		}
	}
}

func (sc *Scheduler) tick(ctx context.Context) {
	due, err := sc.svc.q.DueActionItems(ctx, db.DueActionItemsParams{
		DueAt: tstz(time.Now()),
		Limit: sc.batchSize,
	})
	if err != nil {
		log.Printf("scheduler: query due items: %v", err)
		return
	}
	for _, item := range due {
		if err := sc.dispatch(ctx, item.ID, item.Version); err != nil {
			// One bad item must not abort the rest; it will be retried next
			// tick because no dispatch row will exist.
			log.Printf("scheduler: dispatch %s v%d: %v", item.ID, item.Version, err)
		}
	}
}

// dispatch reserves the (item, version) slot and writes the notification in
// one transaction. The affected-rows result from MarkReminderDispatched is
// the guard that makes concurrent ticks exactly-once.
func (sc *Scheduler) dispatch(ctx context.Context, itemID string, version int32) error {
	tx, err := sc.svc.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	qtx := sc.svc.q.WithTx(tx)

	rows, err := qtx.MarkReminderDispatched(ctx, db.MarkReminderDispatchedParams{
		ActionItemID: itemID,
		Version:      version,
	})
	if err != nil {
		return err
	}
	if rows == 0 {
		// Another tick/process already dispatched this version.
		return nil
	}

	item, err := qtx.GetActionItem(ctx, itemID)
	if err != nil {
		return err
	}
	msg := "Action item due: " + item.Title
	if _, err := qtx.CreateNotification(ctx, db.CreateNotificationParams{
		ID:           uuid.NewString(),
		ActionItemID: item.ID,
		OwnerID:      item.OwnerID,
		Message:      msg,
		DueAt:        item.DueAt,
		Version:      item.Version,
	}); err != nil {
		return err
	}
	if err := qtx.InsertAuditEvent(ctx, db.InsertAuditEventParams{
		ID:         uuid.NewString(),
		IncidentID: textParam(item.IncidentID),
		ActorID:    item.OwnerID,
		Action:     "reminder.dispatched",
		Detail: mustJSON(map[string]any{
			"action_item_id": item.ID,
			"version":        item.Version,
			"due_at":         item.DueAt,
		}),
	}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// TickOnce exposes a single processing pass for tests.
func (sc *Scheduler) TickOnce(ctx context.Context) { sc.tick(ctx) }
