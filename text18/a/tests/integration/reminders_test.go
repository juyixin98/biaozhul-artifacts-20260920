package integration

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"

	"sircc/internal/clock"
	"sircc/internal/service"
	"sircc/internal/store"
)

func mustActionItemID(t *testing.T, res service.Result) uuid.UUID {
	t.Helper()
	v, ok := res.Body.(service.ActionItemView)
	if !ok {
		t.Fatalf("unexpected action item body: %T", res.Body)
	}
	id, err := uuid.Parse(v.ID)
	if err != nil {
		t.Fatalf("parse action item id: %v", err)
	}
	return id
}

func (f *fixture) reminderCount(t *testing.T, itemID uuid.UUID, version int32) int64 {
	t.Helper()
	n, err := store.New(f.pool).CountRemindersForVersion(context.Background(),
		store.CountRemindersForVersionParams{ActionItemID: itemID, DueVersion: version})
	if err != nil {
		t.Fatalf("count reminders: %v", err)
	}
	return n
}

func (f *fixture) createOverdueItem(t *testing.T) uuid.UUID {
	t.Helper()
	id := f.createP2Incident(t)
	res, err := f.svc.CreateActionItem(context.Background(), f.analyst1(), id, service.CreateActionItemInput{
		Description: "overdue task",
		OwnerUserID: f.responder1().ID,
		DueAt:       f.clk.Now().Add(-1 * time.Hour), // already due
	})
	if err != nil {
		t.Fatalf("create action item: %v", err)
	}
	return mustActionItemID(t, res)
}

// A due item is reminded once; repeated sweeps must not duplicate the
// reminder for the same due version.
func TestReminder_SameVersionRemindedOnce(t *testing.T) {
	f := newFixture(t)
	itemID := f.createOverdueItem(t)

	n1, err := f.svc.SweepDueOnce(context.Background(), 100)
	if err != nil {
		t.Fatalf("sweep 1: %v", err)
	}
	if n1 != 1 {
		t.Fatalf("first sweep should persist 1 reminder, got %d", n1)
	}
	for i := 0; i < 3; i++ {
		n, err := f.svc.SweepDueOnce(context.Background(), 100)
		if err != nil {
			t.Fatalf("sweep %d: %v", i+2, err)
		}
		if n != 0 {
			t.Fatalf("repeat sweep %d created %d duplicate reminders", i+2, n)
		}
	}
	if c := f.reminderCount(t, itemID, 1); c != 1 {
		t.Fatalf("want exactly 1 reminder for v1, got %d", c)
	}
}

// After rescheduling into the future, the old (already due) schedule must
// not produce a stale reminder. When the new deadline passes, exactly one v2
// reminder is persisted.
func TestReminder_RescheduleSuppressesStaleThenRemindsNew(t *testing.T) {
	f := newFixture(t)
	incidentID := f.createP2Incident(t)
	res, err := f.svc.CreateActionItem(context.Background(), f.analyst1(), incidentID, service.CreateActionItemInput{
		Description: "moving deadline",
		OwnerUserID: f.responder1().ID,
		DueAt:       f.clk.Now().Add(-1 * time.Hour), // v1 already due
	})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	itemID := mustActionItemID(t, res)

	if _, err := f.svc.RescheduleActionItem(context.Background(), f.analyst1(),
		incidentID, itemID, service.RescheduleInput{DueAt: f.clk.Now().Add(2 * time.Hour)}); err != nil {
		t.Fatalf("reschedule: %v", err)
	}

	n, err := f.svc.SweepDueOnce(context.Background(), 100)
	if err != nil {
		t.Fatalf("sweep: %v", err)
	}
	if n != 0 {
		t.Fatalf("rescheduled item must not fire an old-schedule reminder, got %d", n)
	}
	if c := f.reminderCount(t, itemID, 1); c != 0 {
		t.Fatalf("stale v1 reminder persisted: %d", c)
	}

	f.clk.Advance(3 * time.Hour)
	n, err = f.svc.SweepDueOnce(context.Background(), 100)
	if err != nil {
		t.Fatalf("sweep after new deadline: %v", err)
	}
	if n != 1 {
		t.Fatalf("want 1 v2 reminder, got %d", n)
	}
	if c := f.reminderCount(t, itemID, 2); c != 1 {
		t.Fatalf("want 1 v2 reminder, got %d", c)
	}
	if c := f.reminderCount(t, itemID, 1); c != 0 {
		t.Fatalf("v1 must never be reminded, got %d", c)
	}
}

// Concurrent sweeps racing a reschedule: the old due version can never be
// reminded after it was superseded, and no version gets more than one
// reminder.
func TestReminder_ConcurrentSweepAndReschedule(t *testing.T) {
	f := newFixture(t)
	incidentID := f.createP2Incident(t)
	res, err := f.svc.CreateActionItem(context.Background(), f.analyst1(), incidentID, service.CreateActionItemInput{
		Description: "racy deadline",
		OwnerUserID: f.responder1().ID,
		DueAt:       f.clk.Now().Add(-1 * time.Minute), // v1 due now
	})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	itemID := mustActionItemID(t, res)

	const farFuture = 100 * 365 * 24 * time.Hour
	var wg sync.WaitGroup
	var sweeps int64
	var errs int64
	start := make(chan struct{})

	// One rescheduler pushes v2 far into the future.
	wg.Add(1)
	go func() {
		defer wg.Done()
		<-start
		if _, err := f.svc.RescheduleActionItem(context.Background(), f.analyst1(),
			incidentID, itemID, service.RescheduleInput{DueAt: f.clk.Now().Add(farFuture)}); err != nil {
			t.Errorf("reschedule: %v", err)
		}
	}()
	// Many sweeps race it. A sweep that loses a row-lock deadlock reports an
	// error and creates nothing; the persistent invariants must still hold.
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			n, err := f.svc.SweepDueOnce(context.Background(), 100)
			if err != nil {
				atomic.AddInt64(&errs, 1)
				return
			}
			atomic.AddInt64(&sweeps, int64(n))
		}()
	}
	close(start)
	wg.Wait()

	v1 := f.reminderCount(t, itemID, 1)
	v2 := f.reminderCount(t, itemID, 2)
	if v1 > 1 {
		t.Fatalf("old version reminded %d times", v1)
	}
	if v2 != 0 {
		t.Fatalf("future v2 must not be reminded yet, got %d", v2)
	}
	if v1 != sweeps {
		t.Fatalf("created reminder count mismatch: rows=%d sweepCreates=%d errors=%d", v1, sweeps, errs)
	}
}

// Restart catch-up: with no process sweeping while time passes, a freshly
// constructed service (simulating a restart) persists the missed reminder
// on its first sweep.
func TestReminder_RestartCatchUp(t *testing.T) {
	f := newFixture(t)
	incidentID := f.createP2Incident(t)
	res, err := f.svc.CreateActionItem(context.Background(), f.analyst1(), incidentID, service.CreateActionItemInput{
		Description: "came due while down",
		OwnerUserID: f.responder1().ID,
		DueAt:       f.clk.Now().Add(30 * time.Minute),
	})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	itemID := mustActionItemID(t, res)

	// Simulate the service being stopped: advance the clock with no sweeps.
	f.clk.Advance(2 * time.Hour)

	// A brand-new service instance over the same database is the restart.
	restarted := service.New(f.pool, &clock.Mock{T: f.clk.Now()})
	n, err := restarted.SweepDueOnce(context.Background(), 100)
	if err != nil {
		t.Fatalf("catch-up sweep: %v", err)
	}
	if n != 1 {
		t.Fatalf("restart must catch up the missed reminder, got %d", n)
	}
	if c := f.reminderCount(t, itemID, 1); c != 1 {
		t.Fatalf("want 1 durable reminder, got %d", c)
	}
}

// Done and canceled items are not swept.
func TestReminder_InactiveItemsNotSwept(t *testing.T) {
	f := newFixture(t)
	incidentID := f.createP2Incident(t)
	res, err := f.svc.CreateActionItem(context.Background(), f.analyst1(), incidentID, service.CreateActionItemInput{
		Description: "will be canceled",
		OwnerUserID: f.responder1().ID,
		DueAt:       f.clk.Now().Add(-time.Minute),
	})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	itemID := mustActionItemID(t, res)
	if _, err := f.svc.SetActionItemStatus(context.Background(), f.analyst1(),
		incidentID, itemID, "canceled"); err != nil {
		t.Fatalf("cancel: %v", err)
	}
	n, err := f.svc.SweepDueOnce(context.Background(), 100)
	if err != nil {
		t.Fatalf("sweep: %v", err)
	}
	if n != 0 {
		t.Fatalf("canceled item must not be reminded, got %d", n)
	}
}

// Remind v1 once, reschedule to a new deadline, then let that pass: v2 is
// reminded exactly once and v1 keeps its single reminder. Repeated sweeps at
// each version never duplicate (duplicate scheduling).
func TestReminder_RescheduleAfterRemindedThenOnceForNewVersion(t *testing.T) {
	f := newFixture(t)
	incidentID := f.createP2Incident(t)
	res, err := f.svc.CreateActionItem(context.Background(), f.analyst1(), incidentID, service.CreateActionItemInput{
		Description: "two schedules",
		OwnerUserID: f.responder1().ID,
		DueAt:       f.clk.Now().Add(-time.Minute), // v1 due
	})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	itemID := mustActionItemID(t, res)

	if n, err := f.svc.SweepDueOnce(context.Background(), 100); err != nil || n != 1 {
		t.Fatalf("v1 sweep: n=%d err=%v", n, err)
	}
	if n, _ := f.svc.SweepDueOnce(context.Background(), 100); n != 0 {
		t.Fatalf("duplicate v1 reminder: %d", n)
	}

	if _, err := f.svc.RescheduleActionItem(context.Background(), f.analyst1(),
		incidentID, itemID, service.RescheduleInput{DueAt: f.clk.Now().Add(time.Hour)}); err != nil {
		t.Fatalf("reschedule: %v", err)
	}
	f.clk.Advance(2 * time.Hour)

	if n, err := f.svc.SweepDueOnce(context.Background(), 100); err != nil || n != 1 {
		t.Fatalf("v2 sweep: n=%d err=%v", n, err)
	}
	for i := 0; i < 3; i++ {
		if n, _ := f.svc.SweepDueOnce(context.Background(), 100); n != 0 {
			t.Fatalf("duplicate v2 reminder on sweep %d: %d", i+1, n)
		}
	}
	if c := f.reminderCount(t, itemID, 1); c != 1 {
		t.Fatalf("v1 reminder count = %d, want 1", c)
	}
	if c := f.reminderCount(t, itemID, 2); c != 1 {
		t.Fatalf("v2 reminder count = %d, want 1", c)
	}
}
