package integration

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync"
	"testing"
	"time"

	"sircc/internal/service"
)

func tickScheduler(t *testing.T, svc *service.Service) {
	t.Helper()
	service.NewScheduler(svc, time.Minute, 100).TickOnce(context.Background())
}

type actionItemResult struct {
	ID      string `json:"id"`
	Version int32  `json:"version"`
	Status  string `json:"status"`
}

func createActionItem(t *testing.T, h http.Handler, incidentID, owner, dueRFC3339 string) actionItemResult {
	t.Helper()
	body := `{"title":"fix it","owner_id":"` + owner + `","due_at":"` + dueRFC3339 + `"}`
	w := doJSON(t, h, http.MethodPost, "/v1/incidents/"+incidentID+"/action-items",
		uAnalyst, reqID(), body)
	if w.Code != http.StatusOK {
		t.Fatalf("create action item: %d %s", w.Code, w.Body.String())
	}
	var ai actionItemResult
	decodeBody(t, w, &ai)
	return ai
}

func notificationCount(t *testing.T, itemID string) int {
	t.Helper()
	var n int
	if err := pool.QueryRow(context.Background(),
		`SELECT count(*) FROM notifications WHERE action_item_id=$1`, itemID).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func dispatchCount(t *testing.T, itemID string) int {
	t.Helper()
	var n int
	if err := pool.QueryRow(context.Background(),
		`SELECT count(*) FROM reminder_dispatches WHERE action_item_id=$1`, itemID).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

// TestReminderFiresOnce: a due item notifies once; repeated ticks don't
// duplicate it.
func TestReminderFiresOnce(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	svc := newService(t)
	h := newServer(t, svc)

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "due item")
	past := time.Now().Add(-time.Hour).UTC().Format(time.RFC3339)
	ai := createActionItem(t, h, inc.ID, uResponder, past)

	tickScheduler(t, svc)
	if got := notificationCount(t, ai.ID); got != 1 {
		t.Fatalf("after first tick want 1 notification, got %d", got)
	}
	if got := dispatchCount(t, ai.ID); got != 1 {
		t.Fatalf("want 1 dispatch row, got %d", got)
	}

	// Second and third tick must not create duplicates.
	tickScheduler(t, svc)
	tickScheduler(t, svc)
	if got := notificationCount(t, ai.ID); got != 1 {
		t.Fatalf("same due version reminded %d times, want 1", got)
	}
}

// TestFutureItemNotReminded: not yet due items are ignored.
func TestFutureItemNotReminded(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	svc := newService(t)
	h := newServer(t, svc)

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "future item")
	future := time.Now().Add(24 * time.Hour).UTC().Format(time.RFC3339)
	ai := createActionItem(t, h, inc.ID, uResponder, future)

	tickScheduler(t, svc)
	if got := notificationCount(t, ai.ID); got != 0 {
		t.Fatalf("future item produced %d early reminders", got)
	}
}

// TestRescheduleSuppressesStaleReminder: an item past-due is moved to the
// future; the old schedule version must never fire. Moving it back (new
// version) fires exactly once for the new due version.
func TestRescheduleSuppressesStaleReminder(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	svc := newService(t)
	h := newServer(t, svc)

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "reschedule")
	ai := createActionItem(t, h, inc.ID, uResponder,
		time.Now().Add(-time.Hour).UTC().Format(time.RFC3339))

	// Reschedule v1 -> v2 to the future before the scheduler runs.
	future := time.Now().Add(48 * time.Hour).UTC().Format(time.RFC3339)
	w := reschedule(t, h, ai.ID, 1, future)
	if w.Code != http.StatusOK {
		t.Fatalf("reschedule to future: %d %s", w.Code, w.Body.String())
	}

	tickScheduler(t, svc)
	if got := notificationCount(t, ai.ID); got != 0 {
		t.Fatalf("old schedule fired a stale reminder (%d notifications)", got)
	}

	// Reschedule v2 -> v3 back into the past; only v3 should remind.
	past := time.Now().Add(-time.Minute).UTC().Format(time.RFC3339)
	w = reschedule(t, h, ai.ID, 2, past)
	if w.Code != http.StatusOK {
		t.Fatalf("reschedule to past: %d %s", w.Code, w.Body.String())
	}
	tickScheduler(t, svc)
	tickScheduler(t, svc)
	if got := notificationCount(t, ai.ID); got != 1 {
		t.Fatalf("new due version want 1 reminder, got %d", got)
	}
	// The notification must reference version 3, not the stale v1.
	var version int32
	if err := pool.QueryRow(context.Background(),
		`SELECT version FROM notifications WHERE action_item_id=$1`, ai.ID).Scan(&version); err != nil {
		t.Fatal(err)
	}
	if version != 3 {
		t.Fatalf("reminder fired for stale version %d, want 3", version)
	}
}

// TestRescheduleRace: two concurrent reschedules carrying the same expected
// version — one wins, one is rejected with version_conflict; version bumps
// exactly once and only one audit row is written.
func TestRescheduleRace(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	svc := newService(t)
	h := newServer(t, svc)

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "reschedule race")
	future := time.Now().Add(24 * time.Hour).UTC().Format(time.RFC3339)
	ai := createActionItem(t, h, inc.ID, uResponder, future)

	newDue := time.Now().Add(72 * time.Hour).UTC().Format(time.RFC3339)
	var wg sync.WaitGroup
	var mu sync.Mutex
	ok, conflict := 0, 0
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			w := reschedule(t, h, ai.ID, 1, newDue)
			mu.Lock()
			defer mu.Unlock()
			switch w.Code {
			case http.StatusOK:
				ok++
			case http.StatusConflict:
				conflict++
			default:
				t.Errorf("unexpected reschedule status %d %s", w.Code, w.Body.String())
			}
		}()
	}
	wg.Wait()
	if ok != 1 || conflict != 1 {
		t.Fatalf("reschedule race: want 1 ok + 1 conflict, got %d/%d", ok, conflict)
	}

	var version int32
	if err := pool.QueryRow(context.Background(),
		`SELECT version FROM action_items WHERE id=$1`, ai.ID).Scan(&version); err != nil {
		t.Fatal(err)
	}
	if version != 2 {
		t.Fatalf("version should bump once to 2, got %d", version)
	}
	var audits int
	if err := pool.QueryRow(context.Background(),
		`SELECT count(*) FROM action_item_reschedules WHERE action_item_id=$1`, ai.ID).
		Scan(&audits); err != nil {
		t.Fatal(err)
	}
	if audits != 1 {
		t.Fatalf("want 1 reschedule audit row, got %d", audits)
	}
}

// TestRestartCatchUp: an item became due while the scheduler was not running.
// A fresh scheduler (as after a restart) delivers the missed reminder on its
// first pass.
func TestRestartCatchUp(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	svc := newService(t) // scheduler never Run
	h := newServer(t, svc)

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "downtime")
	past := time.Now().Add(-3 * time.Hour).UTC().Format(time.RFC3339)
	ai := createActionItem(t, h, inc.ID, uResponder, past)

	if got := notificationCount(t, ai.ID); got != 0 {
		t.Fatalf("precondition: %d notifications before any tick", got)
	}

	// Simulate process restart: brand-new scheduler instance, immediate tick.
	fresh := service.NewScheduler(svc, time.Hour, 100)
	fresh.TickOnce(context.Background())

	if got := notificationCount(t, ai.ID); got != 1 {
		t.Fatalf("restart did not catch up missed reminder: got %d", got)
	}
}

// TestRescheduleRejectedOnStaleVersion: a reschedule using an old expected
// version after the item already moved is refused.
func TestRescheduleRejectedOnStaleVersion(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "stale resched")
	future := time.Now().Add(24 * time.Hour).UTC().Format(time.RFC3339)
	ai := createActionItem(t, h, inc.ID, uResponder, future)

	later := time.Now().Add(48 * time.Hour).UTC().Format(time.RFC3339)
	if w := reschedule(t, h, ai.ID, 1, later); w.Code != http.StatusOK {
		t.Fatalf("first reschedule: %d %s", w.Code, w.Body.String())
	}
	// Same expected_version=1 is now stale.
	w := reschedule(t, h, ai.ID, 1, later)
	if w.Code != http.StatusConflict {
		t.Fatalf("stale reschedule want 409, got %d %s", w.Code, w.Body.String())
	}
	assertCode(t, w, "version_conflict")
}

func reschedule(t *testing.T, h http.Handler, itemID string, version int32, dueRFC3339 string) *httptest.ResponseRecorder {
	t.Helper()
	body := `{"new_due_at":"` + dueRFC3339 + `","expected_version":` + strconv.FormatInt(int64(version), 10) + `}`
	return doJSON(t, h, http.MethodPost, "/v1/action-items/"+itemID+"/reschedule",
		uAnalyst, reqID(), body)
}
