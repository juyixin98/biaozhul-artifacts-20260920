package api

import (
	"fmt"
	"net/http"
	"sync"
	"testing"
)

// TestConcurrentPublish_OnlyOneSucceeds: two publishers that both believe the
// store is at version 0 race to publish. The row lock on stores serializes
// them; exactly one must be created and the other must get a 409.
func TestConcurrentPublish_OnlyOneSucceeds(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")
	e.addDish(storeID, "A", "Apple", 100, nil)

	const n = 8
	var wg sync.WaitGroup
	statuses := make([]int, n)
	var created int64
	var mu sync.Mutex

	wg.Add(n)
	for i := 0; i < n; i++ {
		i := i
		go func() {
			defer wg.Done()
			body := `{"expected_version":0,"note":"race"}`
			st, data, err := e.adminSafe(http.MethodPost,
				fmt.Sprintf("/v1/admin/stores/%d/publish", storeID), body)
			if err != nil {
				t.Errorf("goroutine %d request error: %v", i, err)
				return
			}
			statuses[i] = st
			if st == http.StatusCreated {
				mu.Lock()
				created++
				mu.Unlock()
			} else if st != http.StatusConflict {
				t.Errorf("goroutine %d unexpected status %d body %v", i, st, data)
			}
		}()
	}
	wg.Wait()

	if created != 1 {
		t.Fatalf("expected exactly 1 successful publish, got %d (statuses=%v)", created, statuses)
	}

	// Store must now be exactly at version 1; the next publish must expect 1.
	st, data := e.admin(http.MethodGet, fmt.Sprintf("/v1/admin/stores/%d", storeID), "")
	if st != http.StatusOK {
		t.Fatalf("get store: %d", st)
	}
	if got := int64(data["menu_version"].(float64)); got != 1 {
		t.Fatalf("expected menu_version 1, got %d", got)
	}

	// A stale expected_version is rejected even without concurrency.
	st, _ = e.publish(storeID, `{"expected_version":0,"note":"stale"}`)
	if st != http.StatusConflict {
		t.Fatalf("expected 409 for stale version, got %d", st)
	}
	// The correct expected version succeeds and yields version 2.
	st, data = e.publish(storeID, `{"expected_version":1,"note":"second"}`)
	if st != http.StatusCreated {
		t.Fatalf("expected 201, got %d %v", st, data)
	}
	if got := int64(data["version"].(float64)); got != 2 {
		t.Fatalf("expected version 2, got %v", data["version"])
	}
}

// TestPublishRequiresExpectedVersion verifies optimistic concurrency is
// mandatory.
func TestPublishRequiresExpectedVersion(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")
	st, _ := e.publish(storeID, `{"note":"no version"}`)
	if st != http.StatusBadRequest {
		t.Fatalf("expected 400 without expected_version, got %d", st)
	}
}

// TestPublishedVersionsAreImmutableAndIsolated: editing the draft after
// publishing must not change the published version, and a new publish captures
// the new draft.
func TestPublishedVersionsAreImmutableAndIsolated(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")
	e.addDish(storeID, "BURGER", "Burger", 1000, nil)

	st, _ := e.publish(storeID, `{"expected_version":0}`)
	if st != http.StatusCreated {
		t.Fatalf("publish v1: %d", st)
	}

	// Edit the draft price (draft change must not touch live v1).
	dishID := e.dishIDBySku(storeID, "BURGER")
	st, _ = e.admin(http.MethodPut,
		fmt.Sprintf("/v1/admin/stores/%d/dishes/%d", storeID, dishID),
		`{"sku":"BURGER","name":"Burger","base_price":1200,"active":true}`)
	if st != http.StatusOK {
		t.Fatalf("update dish: %d", st)
	}

	// v1 must still carry the old 1000-cent price.
	st, v1 := e.admin(http.MethodGet,
		fmt.Sprintf("/v1/admin/stores/%d/versions/1", storeID), "")
	if st != http.StatusOK {
		t.Fatalf("get v1: %d", st)
	}
	items := v1["items"].([]any)
	if len(items) != 1 {
		t.Fatalf("expected 1 item, got %d", len(items))
	}
	if price := int64(items[0].(map[string]any)["base_price"].(float64)); price != 1000 {
		t.Fatalf("immutable v1 price changed to %d", price)
	}

	// Publish v2; it must snapshot the new price.
	st, _ = e.publish(storeID, `{"expected_version":1}`)
	if st != http.StatusCreated {
		t.Fatalf("publish v2: %d", st)
	}
	st, v2 := e.admin(http.MethodGet,
		fmt.Sprintf("/v1/admin/stores/%d/versions/2", storeID), "")
	items = v2["items"].([]any)
	if price := int64(items[0].(map[string]any)["base_price"].(float64)); price != 1200 {
		t.Fatalf("v2 expected 1200, got %d", price)
	}
}
