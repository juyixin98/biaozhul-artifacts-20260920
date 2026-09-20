package service_test

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"signalboard/internal/service"
	"signalboard/internal/testsupport"
)

func ingest(storeID int64, id string, dish int64, qty int32, when time.Time) service.SalesEventInput {
	return service.SalesEventInput{
		EventID: id, DishID: dish, Quantity: qty, OccurredAt: when,
	}
}

// Same id twice counts once; same id with different content is a conflict.
func TestSalesIdempotentAndConflict(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	storeID, dishIDs := e.SeedStore(t, 1, "UTC")
	if err := e.Svc.SetThreshold(ctx, storeID, dishIDs[0], 100); err != nil {
		t.Fatal(err)
	}
	when := at(10_000)

	r1, err := e.Svc.IngestSalesEvent(ctx, storeID, ingest(storeID, "evt-1", dishIDs[0], 3, when))
	if err != nil {
		t.Fatal(err)
	}
	if r1.DayTotal != 3 || r1.Duplicate {
		t.Fatalf("first apply: %+v", r1)
	}

	r2, err := e.Svc.IngestSalesEvent(ctx, storeID, ingest(storeID, "evt-1", dishIDs[0], 3, when))
	if err != nil {
		t.Fatal(err)
	}
	if !r2.Duplicate || r2.DayTotal != 3 {
		t.Fatalf("identical replay must not recount: %+v", r2)
	}

	// Same id, different quantity -> conflict.
	_, err = e.Svc.IngestSalesEvent(ctx, storeID, ingest(storeID, "evt-1", dishIDs[0], 4, when))
	if !errors.Is(err, service.ErrConflict) {
		t.Fatalf("different payload same id: want ErrConflict, got %v", err)
	}

	// Same id, different dish -> conflict.
	otherDish, err := e.Svc.CreateDish(ctx, storeID, "extra", 50)
	if err != nil {
		t.Fatal(err)
	}
	_, err = e.Svc.IngestSalesEvent(ctx, storeID, ingest(storeID, "evt-1", otherDish.ID, 3, when))
	if !errors.Is(err, service.ErrConflict) {
		t.Fatalf("different dish same id: want ErrConflict, got %v", err)
	}
}

// N concurrent submissions of the SAME event id count the quantity exactly
// once (no double count, no loss).
func TestSalesConcurrentSameEventID(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	storeID, dishIDs := e.SeedStore(t, 1, "UTC")
	if err := e.Svc.SetThreshold(ctx, storeID, dishIDs[0], 1000); err != nil {
		t.Fatal(err)
	}

	const n = 20
	var wg sync.WaitGroup
	errs := make([]error, n)
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func(i int) {
			defer wg.Done()
			_, errs[i] = e.Svc.IngestSalesEvent(ctx, storeID,
				ingest(storeID, "only", dishIDs[0], 5, at(1000)))
		}(i)
	}
	wg.Wait()

	for _, err := range errs {
		if err != nil {
			t.Fatalf("identical concurrent sends must not error: %v", err)
		}
	}

	day, err := e.Svc.SalesTodayAt(ctx, storeID, at(1000).Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	total := int64(0)
	for _, s := range day.Stats {
		total += s.Quantity
	}
	if total != 5 {
		t.Fatalf("concurrent identical events counted %d, want exactly 5", total)
	}
}

// N concurrent DISTINCT events accumulate without loss, and crossing the
// threshold flips sold_out.
func TestSalesConcurrentDistinctThreshold(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	storeID, dishIDs := e.SeedStore(t, 1, "UTC")
	const threshold = 50
	if err := e.Svc.SetThreshold(ctx, storeID, dishIDs[0], threshold); err != nil {
		t.Fatal(err)
	}
	e.DraftAndPublish(t, storeID, dishIDs, 0)

	const n = 20 // 20 * qty 3 = 60 -> crosses 50
	var wg sync.WaitGroup
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func(i int) {
			defer wg.Done()
			id := fmt.Sprintf("evt-%d", i)
			if _, err := e.Svc.IngestSalesEvent(ctx, storeID,
				ingest(storeID, id, dishIDs[0], 3, at(1000+int64(i)))); err != nil {
				t.Errorf("ingest %s: %v", id, err)
			}
		}(i)
	}
	wg.Wait()

	day, err := e.Svc.SalesTodayAt(ctx, storeID, at(2000))
	if err != nil {
		t.Fatal(err)
	}
	if len(day.Stats) != 1 {
		t.Fatalf("stats = %d rows, want 1", len(day.Stats))
	}
	if day.Stats[0].Quantity != 60 {
		t.Fatalf("quantity = %d, want 60 (lost or doubled updates)", day.Stats[0].Quantity)
	}
	if !day.Stats[0].SoldOut {
		t.Fatalf("dish should be sold out after crossing threshold %d", threshold)
	}

	menu, err := e.Svc.GetScreenMenu(ctx, storeID, at(2000))
	if err != nil {
		t.Fatal(err)
	}
	if !menu.Lines[0].SoldOut {
		t.Fatal("screen menu must reflect sold_out")
	}
}

// Exactly-at-threshold sells out; below stays available.
func TestSelloutThresholdBoundary(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	storeID, dishIDs := e.SeedStore(t, 1, "UTC")
	if err := e.Svc.SetThreshold(ctx, storeID, dishIDs[0], 5); err != nil {
		t.Fatal(err)
	}
	day := at(86400)
	r, err := e.Svc.IngestSalesEvent(ctx, storeID,
		ingest(storeID, "a", dishIDs[0], 5, day))
	if err != nil {
		t.Fatal(err)
	}
	if !r.SoldOut || r.DayTotal != 5 {
		t.Fatalf("at-threshold event should flip sold_out: %+v", r)
	}
}

// Sellout is per store-local day: sold out on day D, available again on D+1.
func TestSelloutRecoversNextDay(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	storeID, dishIDs := e.SeedStore(t, 1, "UTC")
	if err := e.Svc.SetThreshold(ctx, storeID, dishIDs[0], 5); err != nil {
		t.Fatal(err)
	}
	e.DraftAndPublish(t, storeID, dishIDs, 0)
	day0 := at(24 * 3600)
	for i := 0; i < 5; i++ {
		if _, err := e.Svc.IngestSalesEvent(ctx, storeID, ingest(storeID,
			fmt.Sprintf("a-%d", i), dishIDs[0], 1, day0.Add(time.Duration(i)*time.Second))); err != nil {
			t.Fatal(err)
		}
	}
	menu0, err := e.Svc.GetScreenMenu(ctx, storeID, day0.Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if !menu0.Lines[0].SoldOut {
		t.Fatal("want sold out on day 0")
	}
	menu1, err := e.Svc.GetScreenMenu(ctx, storeID, day0.Add(25*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if menu1.Lines[0].SoldOut {
		t.Fatal("want availability restored on the next store-local day")
	}
}

// A late event is attributed to the day it actually occurred, not arrival day.
func TestLateEventAttributedToOccurrenceDay(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	storeID, dishIDs := e.SeedStore(t, 1, "UTC")
	if err := e.Svc.SetThreshold(ctx, storeID, dishIDs[0], 10); err != nil {
		t.Fatal(err)
	}
	e.DraftAndPublish(t, storeID, dishIDs, 0)
	occurred := at(100 * 24 * 3600)
	res, err := e.Svc.IngestSalesEvent(ctx, storeID,
		ingest(storeID, "late", dishIDs[0], 2, occurred))
	if err != nil {
		t.Fatal(err)
	}
	wantDay := at(100 * 24 * 3600).Truncate(24 * time.Hour)
	if !res.SalesDay.Equal(wantDay) {
		t.Fatalf("sales_day = %v, want occurrence day %v", res.SalesDay, wantDay)
	}
	// Two days later, that dish is still not sold out today.
	menu, err := e.Svc.GetScreenMenu(ctx, storeID, occurred.Add(48*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if menu.Lines[0].SoldOut {
		t.Fatal("late event must not sell out the arrival day")
	}
}

// Events around a zone midnight bucket to the store's local date, not UTC.
func TestSalesTimezoneDayBucketing(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	storeID, dishIDs := e.SeedStore(t, 1, "America/New_York")
	loc, _ := time.LoadLocation("America/New_York")

	// Local 2024-01-02 23:00 == 2024-01-03 04:00 UTC
	localNight := time.Date(2024, 1, 2, 23, 0, 0, 0, loc)
	res, err := e.Svc.IngestSalesEvent(ctx, storeID,
		ingest(storeID, "nyc-1", dishIDs[0], 1, localNight))
	if err != nil {
		t.Fatal(err)
	}
	if y, m, d := res.SalesDay.Date(); d != 2 || m != time.January || y != 2024 {
		t.Fatalf("bucketed to %04d-%02d-%02d, want local 2024-01-02", y, m, d)
	}

	// One hour later crosses local midnight -> a new bucket.
	localNext := localNight.Add(time.Hour) // local 2024-01-03 00:00
	res2, err := e.Svc.IngestSalesEvent(ctx, storeID,
		ingest(storeID, "nyc-2", dishIDs[0], 1, localNext))
	if err != nil {
		t.Fatal(err)
	}
	if res2.SalesDay.Equal(res.SalesDay) {
		t.Fatalf("midnight crossing must create a new day bucket: %v == %v",
			res2.SalesDay, res.SalesDay)
	}
}
