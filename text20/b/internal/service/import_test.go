package service_test

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"signalboard/internal/service"
	"signalboard/internal/testsupport"
)

// A batch with one bad item rolls back completely: prior items must not exist.
func TestBatchImportRollbackOnError(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	storeID, dishIDs := e.SeedStore(t, 5, "UTC")

	// First establish a valid draft that must remain intact after a failed import.
	good := []service.DraftItemInput{{DishID: dishIDs[0]}, {DishID: dishIDs[1]}}
	if _, err := e.Svc.ReplaceDraft(ctx, storeID, good); err != nil {
		t.Fatal(err)
	}

	// Batch where a later item references a nonexistent dish.
	bad := []service.DraftItemInput{
		{DishID: dishIDs[0]},
		{DishID: dishIDs[1]},
		{DishID: dishIDs[2]},
		{DishID: 9_999_999},
	}
	_, err := e.Svc.ReplaceDraft(ctx, storeID, bad)
	if !errors.Is(err, service.ErrValidation) {
		t.Fatalf("want ErrValidation, got %v", err)
	}

	d, err := e.Svc.GetDraft(ctx, storeID)
	if err != nil {
		t.Fatal(err)
	}
	if len(d.Items) != len(good) {
		t.Fatalf("batch was not atomic: draft now has %d items, want original %d",
			len(d.Items), len(good))
	}
	for i, it := range d.Items {
		if it.DishID != good[i].DishID {
			t.Fatalf("draft content changed at position %d", i)
		}
	}
}

func TestBatchImportMax500(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	storeID, _ := e.SeedStore(t, 1, "UTC")

	over := make([]service.DraftItemInput, service.MaxBatchItems+1)
	for i := range over {
		over[i] = service.DraftItemInput{DishID: int64(5000 + i)}
	}

	_, err := e.Svc.ReplaceDraft(ctx, storeID, over)
	if err == nil || !errors.Is(err, service.ErrValidation) ||
		!strings.Contains(err.Error(), "500") {
		t.Fatalf("want size validation mentioning 500, got %v", err)
	}
}

// A 500-item batch of valid dishes succeeds (boundary is inclusive).
func TestBatchImportExactly500Accepted(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	// 500 dishes is a lot to create individually but keeps the test honest.
	storeID, _ := e.SeedStore(t, 0, "UTC")
	items := make([]service.DraftItemInput, service.MaxBatchItems)
	for i := range items {
		d, err := e.Svc.CreateDish(ctx, storeID, fmt.Sprintf("dish-%03d", i), int64(i+1))
		if err != nil {
			t.Fatalf("create dish %d: %v", i, err)
		}
		items[i] = service.DraftItemInput{DishID: d.ID}
	}
	draft, err := e.Svc.ReplaceDraft(ctx, storeID, items)
	if err != nil {
		t.Fatalf("500-item batch should succeed: %v", err)
	}
	if len(draft.Items) != service.MaxBatchItems {
		t.Fatalf("draft has %d items, want %d", len(draft.Items), service.MaxBatchItems)
	}
}

// Duplicate dish lines inside one batch are rejected.
func TestBatchImportRejectsDuplicateDish(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	storeID, dishIDs := e.SeedStore(t, 2, "UTC")

	_, err := e.Svc.ReplaceDraft(ctx, storeID, []service.DraftItemInput{
		{DishID: dishIDs[0]},
		{DishID: dishIDs[0]},
	})
	if !errors.Is(err, service.ErrValidation) {
		t.Fatalf("want ErrValidation for duplicate dish, got %v", err)
	}
}
