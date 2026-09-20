package api

import (
	"context"
	"fmt"
	"net/http"
	"sync"
	"testing"
	"time"
)

// pinClock fixes the server's notion of now for the duration of the test.
func pinClock(t *testing.T, fixed time.Time) {
	t.Helper()
	prev := nowFn
	nowFn = func() time.Time { return fixed }
	t.Cleanup(func() { nowFn = prev })
}

func menuItemBySku(items []any, sku string) map[string]any {
	for _, it := range items {
		m := it.(map[string]any)
		if m["sku"] == sku {
			return m
		}
	}
	return nil
}

// TestTemporaryPrice_HalfOpenBoundaries verifies left-closed/right-open
// semantics: the temporary price applies at start_at and at points strictly
// inside, but NOT at end_at. The switch happens purely as the clock crosses the
// boundary — no republish.
func TestTemporaryPrice_HalfOpenBoundaries(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")
	e.addDish(storeID, "B", "Burger", 1000, nil)
	token := e.provisionScreen(storeID, "s1")

	start := "2026-01-15T09:30:00Z"
	end := "2026-01-15T10:30:00Z"
	body := fmt.Sprintf(`{"expected_version":0,"temporary_prices":[
		{"sku":"B","price":700,"start_at":%q,"end_at":%q}
	]}`, start, end)
	if st, data := e.publish(storeID, body); st != http.StatusCreated {
		t.Fatalf("publish: %d %v", st, data)
	}

	cases := []struct {
		name      string
		now       string
		wantPrice int64
		wantTemp  bool
	}{
		{"before window", "2026-01-15T09:00:00Z", 1000, false},
		{"at start (inclusive)", "2026-01-15T09:30:00Z", 700, true},
		{"inside", "2026-01-15T10:00:00Z", 700, true},
		{"at end (exclusive)", "2026-01-15T10:30:00Z", 1000, false},
		{"after window", "2026-01-15T11:00:00Z", 1000, false},
	}
	prevEtags := make(map[string]string)
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fixed, _ := time.Parse(time.RFC3339, tc.now)
			pinClock(t, fixed)
			st, hdr, menu := e.screen(token, http.MethodGet, "/v1/screen/menu", nil)
			if st != http.StatusOK {
				t.Fatalf("menu: %d", st)
			}
			item := menuItemBySku(menu["items"].([]any), "B")
			if item == nil {
				t.Fatalf("item B missing")
			}
			if got := int64(item["display_price"].(float64)); got != tc.wantPrice {
				t.Fatalf("display_price = %d, want %d", got, tc.wantPrice)
			}
			if got := item["on_temporary_price"].(bool); got != tc.wantTemp {
				t.Fatalf("on_temporary_price = %v, want %v", got, tc.wantTemp)
			}
			etag := hdr.Get("ETag")
			// Before and at/after the window must differ from the in-window
			// ETag, proving the crossing invalidates the cache without republish.
			if prev, ok := prevEtags["B"]; ok && tc.wantTemp == false {
				// Transitioning out: current ETag must not equal in-window ETag.
				if etag == prev {
					t.Fatalf("ETag unchanged when crossing temp-price boundary")
				}
			}
			if tc.wantTemp {
				prevEtags["B"] = etag
			}
		})
	}
}

// TestTemporaryPrice_UTCOffsetBucketing uses a non-UTC store to prove offsets
// are honored: a window given with a +08:00 offset matches the same UTC
// instant.
func TestTemporaryPrice_UTCOffsetBucketing(t *testing.T) {
	e := newTestEnv(t)
	// Shanghai is UTC+8 with no DST.
	storeID := e.createStoreViaAPI("Asia/Shanghai")
	e.addDish(storeID, "B", "Burger", 1000, nil)
	token := e.provisionScreen(storeID, "s1")

	// 18:00-19:00 Shanghai == 10:00-11:00 UTC.
	body := `{"expected_version":0,"temporary_prices":[
		{"sku":"B","price":700,"start_at":"2026-01-15T18:00:00+08:00","end_at":"2026-01-15T19:00:00+08:00"}
	]}`
	if st, data := e.publish(storeID, body); st != http.StatusCreated {
		t.Fatalf("publish: %d %v", st, data)
	}

	pinClock(t, time.Date(2026, 1, 15, 10, 30, 0, 0, time.UTC))
	st, _, menu := e.screen(token, http.MethodGet, "/v1/screen/menu", nil)
	if st != http.StatusOK {
		t.Fatalf("menu: %d", st)
	}
	item := menuItemBySku(menu["items"].([]any), "B")
	if int64(item["display_price"].(float64)) != 700 {
		t.Fatalf("offset window did not apply; item=%v", item)
	}
	if menu["local_day"] != "2026-01-15" {
		t.Fatalf("local_day = %v, want 2026-01-15", menu["local_day"])
	}
}

// TestTemporaryPrice_RejectsOverlapInRequest ensures overlapping windows are
// refused before publication, while adjacent (touching) windows are allowed.
func TestTemporaryPrice_RejectsOverlapInRequest(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")
	e.addDish(storeID, "B", "Burger", 1000, nil)

	overlap := `{"expected_version":0,"temporary_prices":[
		{"sku":"B","price":700,"start_at":"2026-01-15T10:00:00Z","end_at":"2026-01-15T12:00:00Z"},
		{"sku":"B","price":600,"start_at":"2026-01-15T11:00:00Z","end_at":"2026-01-15T13:00:00Z"}
	]}`
	if st, _ := e.publish(storeID, overlap); st != http.StatusBadRequest {
		t.Fatalf("overlap: expected 400, got %d", st)
	}

	adjacent := `{"expected_version":0,"temporary_prices":[
		{"sku":"B","price":700,"start_at":"2026-01-15T10:00:00Z","end_at":"2026-01-15T11:00:00Z"},
		{"sku":"B","price":600,"start_at":"2026-01-15T11:00:00Z","end_at":"2026-01-15T12:00:00Z"}
	]}`
	if st, data := e.publish(storeID, adjacent); st != http.StatusCreated {
		t.Fatalf("adjacent windows should be allowed (half-open), got %d %v", st, data)
	}
}

// TestTemporaryPrice_OverlapConstraint_ConcurrentWrites fires many concurrent
// inserts of OVERLAPPING windows directly at the database for the same version
// and SKU. The GiST exclusion constraint must guarantee at most one commits —
// concurrency cannot bypass overlap validation.
func TestTemporaryPrice_OverlapConstraint_ConcurrentWrites(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")

	// Create a version row directly to target the constraint.
	var versionID int64
	err := e.pool.QueryRow(context.Background(),
		`INSERT INTO menu_versions (store_id, version, note) VALUES ($1, 1, 'constraint-test') RETURNING id`,
		storeID).Scan(&versionID)
	if err != nil {
		t.Fatalf("insert version: %v", err)
	}

	const n = 12
	var wg sync.WaitGroup
	var successes int64
	var mu sync.Mutex
	wg.Add(n)
	base := time.Date(2026, 3, 1, 0, 0, 0, 0, time.UTC)
	for i := 0; i < n; i++ {
		i := i
		go func() {
			defer wg.Done()
			// Every window spans the same instant base+1h, so all overlap.
			start := base.Add(time.Duration(i) * time.Minute)
			end := start.Add(3 * time.Hour)
			_, err := e.pool.Exec(context.Background(),
				`INSERT INTO temporary_prices (store_id, version_id, sku, price, start_at, end_at)
				 VALUES ($1,$2,'B',100,$3,$4)`,
				storeID, versionID, start, end)
			if err == nil {
				mu.Lock()
				successes++
				mu.Unlock()
			}
		}()
	}
	wg.Wait()

	if successes != 1 {
		t.Fatalf("exclusion constraint violated: %d overlapping windows committed, want exactly 1", successes)
	}
}
