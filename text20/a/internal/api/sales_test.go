package api

import (
	"context"
	"fmt"
	"net/http"
	"sync"
	"testing"
	"time"
)

func (e *testEnv) sendEvent(storeID int64, body string) (int, map[string]any) {
	return e.admin(http.MethodPost,
		fmt.Sprintf("/v1/admin/stores/%d/sales-events", storeID), body)
}

// sendEventSafe is goroutine-safe (never calls t.Fatalf).
func (e *testEnv) sendEventSafe(storeID int64, body string) (int, map[string]any, error) {
	return e.adminSafe(http.MethodPost,
		fmt.Sprintf("/v1/admin/stores/%d/sales-events", storeID), body)
}

// TestSalesEvent_Idempotent counts an identical replay exactly once and marks
// it a duplicate; reusing the ID with different content is a 409.
func TestSalesEvent_Idempotent(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")
	e.addDish(storeID, "B", "Burger", 1000, nil)

	ev := `{"event_id":"EVT-1","sku":"B","qty":2,"occurred_at":"2026-02-01T12:00:00Z"}`
	st, data := e.sendEvent(storeID, ev)
	if st != http.StatusCreated {
		t.Fatalf("first: %d %v", st, data)
	}
	if data["duplicate"] != false {
		t.Fatalf("first event should not be a duplicate")
	}
	if int64(data["qty"].(float64)) != 2 {
		t.Fatalf("qty = %v, want 2", data["qty"])
	}

	// Identical replay: counted once.
	st, data = e.sendEvent(storeID, ev)
	if st != http.StatusOK {
		t.Fatalf("replay: expected 200, got %d %v", st, data)
	}
	if data["duplicate"] != true {
		t.Fatalf("replay must be flagged duplicate")
	}
	if int64(data["qty"].(float64)) != 2 {
		t.Fatalf("replay changed counter to %v, want 2 (double count)", data["qty"])
	}

	// Same ID, different content -> conflict.
	conflict := `{"event_id":"EVT-1","sku":"B","qty":3,"occurred_at":"2026-02-01T12:00:00Z"}`
	st, data = e.sendEvent(storeID, conflict)
	if st != http.StatusConflict {
		t.Fatalf("different qty: expected 409, got %d %v", st, data)
	}

	// Same ID, different sku -> conflict as well.
	conflictSku := `{"event_id":"EVT-1","sku":"OTHER","qty":2,"occurred_at":"2026-02-01T12:00:00Z"}`
	st, _ = e.sendEvent(storeID, conflictSku)
	if st != http.StatusConflict {
		t.Fatalf("different sku: expected 409, got %d", st)
	}
}

// TestSalesEvent_ConcurrentUniqueNoLoss fires distinct event IDs concurrently;
// every increment must land exactly once (no lost updates).
func TestSalesEvent_ConcurrentUniqueNoLoss(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")
	e.addDish(storeID, "B", "Burger", 1000, nil)

	const n = 20
	var wg sync.WaitGroup
	wg.Add(n)
	for i := 0; i < n; i++ {
		i := i
		go func() {
			defer wg.Done()
			body := fmt.Sprintf(`{"event_id":"UNIQ-%d","sku":"B","qty":1,"occurred_at":"2026-02-02T12:00:00Z"}`, i)
			st, _, err := e.sendEventSafe(storeID, body)
			if err != nil {
				t.Errorf("event %d: %v", i, err)
				return
			}
			if st != http.StatusCreated {
				t.Errorf("event %d: status %d, want 201", i, st)
			}
		}()
	}
	wg.Wait()

	var total int64
	err := e.pool.QueryRow(context.Background(),
		`SELECT qty FROM daily_sales WHERE store_id=$1 AND sales_day=$2 AND sku='B'`,
		storeID, time.Date(2026, 2, 2, 0, 0, 0, 0, time.UTC)).Scan(&total)
	if err != nil {
		t.Fatalf("query counter: %v", err)
	}
	if total != n {
		t.Fatalf("lost updates: counter=%d, want %d", total, n)
	}
}

// TestSalesEvent_ConcurrentDuplicateCountsOnce hammers one event ID. Exactly
// one submission counts; the counter is qty (not qty*n).
func TestSalesEvent_ConcurrentDuplicateCountsOnce(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")
	e.addDish(storeID, "B", "Burger", 1000, nil)

	const n = 15
	var wg sync.WaitGroup
	var created, dup, other int64
	var mu sync.Mutex
	wg.Add(n)
	body := `{"event_id":"DUP-EVT","sku":"B","qty":4,"occurred_at":"2026-02-03T08:00:00Z"}`
	for i := 0; i < n; i++ {
		go func() {
			defer wg.Done()
			st, _, err := e.sendEventSafe(storeID, body)
			if err != nil {
				t.Errorf("request: %v", err)
				return
			}
			mu.Lock()
			switch st {
			case http.StatusCreated:
				created++
			case http.StatusOK:
				dup++
			default:
				other++
			}
			mu.Unlock()
		}()
	}
	wg.Wait()

	if created != 1 {
		t.Fatalf("expected exactly 1 created, got %d", created)
	}
	if created+dup != n {
		t.Fatalf("expected %d total 201/200, got created=%d dup=%d other=%d", n, created, dup, other)
	}

	var total int64
	err := e.pool.QueryRow(context.Background(),
		`SELECT qty FROM daily_sales WHERE store_id=$1 AND sales_day=$2 AND sku='B'`,
		storeID, time.Date(2026, 2, 3, 0, 0, 0, 0, time.UTC)).Scan(&total)
	if err != nil {
		t.Fatalf("query counter: %v", err)
	}
	if total != 4 {
		t.Fatalf("duplicate counted: counter=%d, want 4", total)
	}
}

// TestSalesEvent_AutoSoldOutAndNextDayRecovery verifies threshold enforcement
// and that a new local day automatically clears the sold-out state.
func TestSalesEvent_AutoSoldOutAndNextDayRecovery(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")
	limit := int64(3)
	e.addDish(storeID, "B", "Burger", 1000, &limit)
	e.publish(storeID, `{"expected_version":0}`)
	token := e.provisionScreen(storeID, "s")

	// Two sales: under threshold, not sold out.
	e.sendEvent(storeID, `{"event_id":"A1","sku":"B","qty":2,"occurred_at":"2026-02-10T12:00:00Z"}`)
	day1, _ := time.Parse(time.RFC3339, "2026-02-10T12:00:00Z")
	pinClock(t, day1)
	st, _, menu := e.screen(token, http.MethodGet, "/v1/screen/menu", nil)
	if st != 200 {
		t.Fatalf("menu: %d", st)
	}
	if item := menuItemBySku(menu["items"].([]any), "B"); item["sold_out"] != false {
		t.Fatalf("should not be sold out after 2/3")
	}

	// One more reaches threshold -> sold out.
	st, data := e.sendEvent(storeID, `{"event_id":"A2","sku":"B","qty":1,"occurred_at":"2026-02-10T13:00:00Z"}`)
	if st != http.StatusCreated || data["sold_out"] != true {
		t.Fatalf("expected sold_out true, got %d %v", st, data)
	}
	st, _, menu = e.screen(token, http.MethodGet, "/v1/screen/menu", nil)
	if item := menuItemBySku(menu["items"].([]any), "B"); item["sold_out"] != true {
		t.Fatalf("menu should show sold_out")
	}

	// Next local day: automatic recovery, no events on the new day.
	day2, _ := time.Parse(time.RFC3339, "2026-02-11T09:00:00Z")
	pinClock(t, day2)
	st, _, menu = e.screen(token, http.MethodGet, "/v1/screen/menu", nil)
	if st != 200 {
		t.Fatalf("menu day2: %d", st)
	}
	if item := menuItemBySku(menu["items"].([]any), "B"); item["sold_out"] != false {
		t.Fatalf("sold_out should clear on new day")
	}
}

// TestSalesEvent_LateEventAttributesToActualDay sends an event that "happened"
// yesterday but arrives today; it must be bucketed to yesterday and must not
// affect today's availability.
func TestSalesEvent_LateEventAttributesToActualDay(t *testing.T) {
	e := newTestEnv(t)
	// Use a negative-offset timezone to confirm day bucketing is not naive UTC.
	storeID := e.createStoreViaAPI("America/Los_Angeles")
	limit := int64(5)
	e.addDish(storeID, "B", "Burger", 1000, &limit)
	e.publish(storeID, `{"expected_version":0}`)
	token := e.provisionScreen(storeID, "s")

	// "Today" in LA is 2026-02-11. A late event for 2026-02-10 23:00 LA time
	// (= 2026-02-11 07:00 UTC) arrives now. It must count against 2026-02-10.
	today, _ := time.Parse(time.RFC3339, "2026-02-11T18:00:00Z") // 10:00 LA
	pinClock(t, today)

	lateBody := `{"event_id":"LATE-1","sku":"B","qty":5,"occurred_at":"2026-02-11T07:00:00Z"}` // 2026-02-10 23:00 PST
	st, data := e.sendEvent(storeID, lateBody)
	if st != http.StatusCreated {
		t.Fatalf("late event: %d %v", st, data)
	}
	if data["sales_day"] != "2026-02-10" {
		t.Fatalf("late event bucketed to %v, want 2026-02-10", data["sales_day"])
	}

	// It sold out YESTERDAY. Today the item must remain available.
	st, _, menu := e.screen(token, http.MethodGet, "/v1/screen/menu", nil)
	if item := menuItemBySku(menu["items"].([]any), "B"); item["sold_out"] != false {
		t.Fatalf("late yesterday sale must not mark today sold out; item=%v", item)
	}
}
