package service_test

import (
	"context"
	"errors"
	"sync"
	"testing"

	"signalboard/internal/service"
	"signalboard/internal/testsupport"
)

// Concurrent publish with the same expected version: exactly one succeeds.
func TestPublishConcurrentSameExpectedVersion(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	storeID, dishIDs := e.SeedStore(t, 3, "UTC")
	items := make([]service.DraftItemInput, len(dishIDs))
	for i, id := range dishIDs {
		items[i] = service.DraftItemInput{DishID: id}
	}
	if _, err := e.Svc.ReplaceDraft(ctx, storeID, items); err != nil {
		t.Fatal(err)
	}

	const n = 12
	var wg sync.WaitGroup
	var mu sync.Mutex
	success, precondition, other := 0, 0, 0
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func() {
			defer wg.Done()
			_, err := e.Svc.Publish(ctx, storeID, 0, "race", nil)
			mu.Lock()
			defer mu.Unlock()
			switch {
			case err == nil:
				success++
			case errors.Is(err, service.ErrPrecondition):
				precondition++
			default:
				other++
				t.Errorf("unexpected error: %v", err)
			}
		}()
	}
	wg.Wait()

	if success != 1 {
		t.Fatalf("expected exactly 1 successful publish, got %d (precondition=%d, other=%d)",
			success, precondition, other)
	}
	if precondition != n-1 {
		t.Fatalf("expected %d precondition failures, got %d", n-1, precondition)
	}

	latest, err := e.Svc.GetPublishedMenu(ctx, storeID, 0)
	if err != nil {
		t.Fatal(err)
	}
	if latest.Version != 1 {
		t.Fatalf("expected current published version 1, got %d", latest.Version)
	}
	if len(latest.Items) != len(dishIDs) {
		t.Fatalf("snapshot has %d items, want %d", len(latest.Items), len(dishIDs))
	}
}

// A stale publisher after a committed publish must fail; the fresh expected
// version must succeed.
func TestPublishExpectedVersionAdvances(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	storeID, dishIDs := e.SeedStore(t, 2, "UTC")

	v1 := e.DraftAndPublish(t, storeID, dishIDs, 0)
	if v1.Version != 1 {
		t.Fatalf("first publish version = %d, want 1", v1.Version)
	}

	// Stale expected version.
	if _, err := e.Svc.Publish(ctx, storeID, 0, "stale", nil); !errors.Is(err, service.ErrPrecondition) {
		t.Fatalf("stale publish: want ErrPrecondition, got %v", err)
	}

	// Draft edits don't affect live until republish.
	if _, err := e.Svc.ReplaceDraft(ctx, storeID,
		[]service.DraftItemInput{{DishID: dishIDs[0]}}); err != nil {
		t.Fatal(err)
	}
	live, err := e.Svc.GetPublishedMenu(ctx, storeID, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(live.Items) != len(dishIDs) {
		t.Fatalf("draft edit leaked into live menu: %d items, want %d",
			len(live.Items), len(dishIDs))
	}

	// Correct expected version publishes the new draft.
	v2, err := e.Svc.Publish(ctx, storeID, 1, "fresh", nil)
	if err != nil {
		t.Fatalf("fresh publish: %v", err)
	}
	if v2.Version != 2 || len(v2.Items) != 1 {
		t.Fatalf("unexpected v2: version=%d items=%d", v2.Version, len(v2.Items))
	}

	// Immutable v1 is still retrievable with its original two items.
	old, err := e.Svc.GetPublishedMenu(ctx, storeID, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(old.Items) != len(dishIDs) {
		t.Fatalf("immutable v1 mutated: %d items", len(old.Items))
	}
}
